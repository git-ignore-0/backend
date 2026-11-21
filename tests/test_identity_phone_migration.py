from uuid import uuid4

import pytest
from django.contrib.auth.hashers import make_password
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.models import QuerySet
from django.test.utils import CaptureQueriesContext

OLD = ("identity", "0003_single_email_per_user")
NEW = ("identity", "0004_phone_identity_activation")
BACKFILL = ("identity", "0005_backfill_legacy_login_phones")


@pytest.fixture
def legacy_schema(transactional_db):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate([OLD])
    # Keep all unrelated apps at their existing schema; only identity is downgraded.
    old_state = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True)
    try:
        yield old_state.apps
    finally:
        MigrationExecutor(connection).migrate(latest)


def snapshots(apps, labels):
    return {
        label: list(apps.get_model(*label.split(".")).objects.order_by("pk").values())
        for label in labels
    }


@pytest.mark.django_db(transaction=True)
def test_legacy_phone_backfill_preserves_data_and_enables_phone_login(client, capsys):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate([NEW])
    apps = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
    User = apps.get_model("identity", "User")
    Profile = apps.get_model("members", "MemberProfile")
    password = make_password("MigratePhonePassword34")
    rows = (
        # Status, legacy contact, previous login phone, expected login phone.
        ("active", "(091) 234-5678", None, "+84912345678"),
        ("suspended", "84 (28) 1234.5678", None, "+842812345678"),
        ("active", "0901234567", None, None),
        ("active", "+84 (90) 123-4567", None, None),
        ("active", "+84 931 234 567", None, None),
        ("active", "0941234567", "+84931234567", "+84931234567"),
        ("active", "0971234567", "+84891234567", "+84891234567"),
        ("active", "+84 971 234 567", None, None),
        ("active", "0912345678 ext 42", None, None),
        ("active", "", None, None),
        ("active", None, None, None),
        ("anonymized", "0981234567", None, None),
        ("anonymized", "0961234567", None, None),
        ("active", "+84 961 234 567", None, None),
    )
    users = []
    for index, (status, legacy, login_phone, expected) in enumerate(rows):
        user = User.objects.create(
            email=f"legacy-{index}@example.com",
            name=f"Legacy {index}",
            status=status,
            is_active=status == "active",
            password=password,
            login_phone_number=login_phone,
        )
        if legacy is not None:
            Profile.objects.create(user_id=user.pk, phone_number=legacy, address="Historic address")
        users.append((user.pk, expected))
    before = {row["id"]: row for row in User.objects.values()}
    profiles_before = snapshots(apps, ["members.MemberProfile"])
    capsys.readouterr()
    try:
        MigrationExecutor(connection).migrate([BACKFILL])
        output = capsys.readouterr().out
        assert "migrated=2" in output
        assert "conflicts=5" in output and "conflicting_numbers=4" in output
        assert "+849" not in output and "@example.com" not in output
        assert {row["id"]: row for row in User.objects.values()} == {
            pk: {**before[pk], "login_phone_number": expected} for pk, expected in users
        }
        assert snapshots(apps, ["members.MemberProfile"]) == profiles_before
        # The live login view uses the current User schema.
        MigrationExecutor(connection).migrate(latest)
        response = client.post(
            "/login", {"login": "0912345678", "password": "MigratePhonePassword34"}
        )
        assert response.status_code == 302 and response.url == "/account"
        assert client.session["_auth_user_id"] == str(users[0][0])
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_legacy_phone_backfill_rechecks_status_when_updating(legacy_schema, monkeypatch):
    state = MigrationExecutor(connection).migrate([NEW])
    User = state.apps.get_model("identity", "User")
    Profile = state.apps.get_model("members", "MemberProfile")
    user = User.objects.create(
        email="legacy@example.com", name="Legacy", status="active", is_active=True
    )
    profile = Profile.objects.create(user_id=user.pk, phone_number="0912345678")
    update = QuerySet.update

    def anonymize_before_phone_write(queryset, **changes):
        if changes.get("login_phone_number") == "+84912345678":
            update(User.objects.filter(pk=user.pk), status="anonymized", is_active=False)
        return update(queryset, **changes)

    with monkeypatch.context() as migration_patch:
        migration_patch.setattr(QuerySet, "update", anonymize_before_phone_write)
        MigrationExecutor(connection).migrate([BACKFILL])

    user.refresh_from_db()
    profile.refresh_from_db()
    assert user.status == "anonymized" and not user.is_active
    assert user.login_phone_number is None
    assert profile.phone_number == "0912345678"


@pytest.mark.django_db(transaction=True)
def test_upgrade_from_0003_preserves_identity_and_related_data(legacy_schema):
    apps = legacy_schema
    User = apps.get_model("identity", "User")
    users = []
    for index, status in enumerate(
        ["pending_verification", "active", "suspended", "deletion_requested", "anonymized"]
    ):
        users.append(
            User.objects.create(
                email=f" Person{index}@EXAMPLE.COM ",
                name=f"Person {index}",
                status=status,
                is_active=index < 2,
                password="pbkdf2_sha256$legacy$preserved",
                is_staff=index == 1,
                is_superuser=index == 1,
            )
        )
        apps.get_model("account", "EmailAddress").objects.create(
            user_id=users[-1].pk,
            email=f" Person{index}@EXAMPLE.COM ",
            primary=True,
            verified=index % 2 == 0,
        )
    member, referee = users[:2]
    group = apps.get_model("auth", "Group").objects.create(name="legacy-role")
    permission = apps.get_model("auth", "Permission").objects.first()
    member.groups.add(group)
    member.user_permissions.add(permission)
    apps.get_model("members", "MemberProfile").objects.create(
        user_id=member.pk, phone_number="0901234567"
    )
    apps.get_model("referrals", "ReferralAccount").objects.create(user_id=member.pk, code="LEGACY1")
    referral = apps.get_model("referrals", "Referral").objects.create(
        referrer_id=member.pk, referee_id=referee.pk
    )
    apps.get_model("referrals", "PointTransaction").objects.create(
        user_id=member.pk,
        actor_id=referee.pk,
        direction="credit",
        amount=12,
        template="manual_adjustment",
        message="Legacy",
        related_referral_id=referral.pk,
    )
    apps.get_model("memberships", "CSACustomer").objects.create(
        user_id=member.pk, name="Legacy", phone_e164="+84901234567"
    )
    apps.get_model("commerce", "StoreOrder").objects.create(
        buyer_user_id=member.pk,
        reference_code="OLD-1",
        create_idempotency_key=uuid4(),
        request_fingerprint="legacy",
        locale="vi",
        recipient_name_snapshot="Legacy",
        phone_e164_snapshot="+84901234567",
        province_code_snapshot="66",
        ward_code_snapshot="22015",
        address_snapshot="Legacy",
        subtotal_vnd=100,
        shipping_fee_vnd=0,
        total_vnd=100,
    )
    # Outbox holds Core UUID used by forum; never rewrite historical payloads.
    apps.get_model("identity", "IdentityEvent").objects.create(
        user_id=member.pk,
        type="user.suspended",
        payload={"user_id": str(member.pk), "status": "suspended"},
    )
    labels = [
        "members.MemberProfile",
        "referrals.ReferralAccount",
        "referrals.Referral",
        "referrals.PointTransaction",
        "memberships.CSACustomer",
        "commerce.StoreOrder",
        "identity.IdentityEvent",
    ]
    before = snapshots(apps, labels)
    user_before = {u.pk: User.objects.values().get(pk=u.pk) for u in users}
    addresses_before = list(
        apps.get_model("account", "EmailAddress").objects.order_by("pk").values()
    )
    MigrationExecutor(connection).migrate([NEW])
    new_apps = (
        MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
    )
    assert snapshots(new_apps, labels) == before
    for index, user in enumerate(users):
        updated = new_apps.get_model("identity", "User").objects.get(pk=user.pk)
        expected = user_before[user.pk]
        for field, value in expected.items():
            if field == "status" and index == 0:
                value = "pending_activation"
            elif field == "email":
                value = f"person{index}@example.com"
            assert getattr(updated, field) == value
        assert updated.login_phone_number is None
    assert list(
        new_apps.get_model("identity", "User")
        .objects.get(pk=member.pk)
        .groups.values_list("pk", flat=True)
    ) == [group.pk]
    assert list(
        new_apps.get_model("identity", "User")
        .objects.get(pk=member.pk)
        .user_permissions.values_list("pk", flat=True)
    ) == [permission.pk]
    addresses_after = list(
        new_apps.get_model("account", "EmailAddress").objects.order_by("pk").values()
    )
    for before_address, after_address in zip(addresses_before, addresses_after, strict=True):
        assert after_address == {**before_address, "email": before_address["email"].strip().lower()}


@pytest.mark.django_db(transaction=True)
def test_normalized_email_conflict_stops_before_any_data_changes(legacy_schema):
    User = legacy_schema.get_model("identity", "User")
    first = User.objects.create(email="duplicate@example.com", name="One")
    second = User.objects.create(email=" DUPLICATE@example.com ", name="Two")
    before = list(User.objects.order_by("pk").values())
    try:
        with pytest.raises(RuntimeError) as error:
            MigrationExecutor(connection).migrate([NEW])
        assert str(first.pk) in str(error.value) and str(second.pk) in str(error.value)
        assert "duplicate@example.com" not in str(error.value).lower()
        assert list(User.objects.order_by("pk").values()) == before
    finally:
        # Repair only the synthetic fixture so schema cleanup can proceed.
        User.objects.filter(pk=second.pk).update(email="resolved@example.com")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("affected_model", ["User", "EmailAddress"])
def test_normalized_email_length_preflight_stops_before_ddl(legacy_schema, affected_model):
    User = legacy_schema.get_model("identity", "User")
    EmailAddress = legacy_schema.get_model("account", "EmailAddress")
    email = "member@" + ".".join(["İ" * 30] * 5) + ".vn"
    model = User if affected_model == "User" else EmailAddress
    max_length = model._meta.get_field("email").max_length
    assert len(email) <= max_length < len(email.lower())
    user = User.objects.create(
        name="Legacy", email=email if affected_model == "User" else "member@example.com"
    )
    address = EmailAddress.objects.create(
        user_id=user.pk, email=email if affected_model == "EmailAddress" else "member@example.com"
    )
    executor = MigrationExecutor(connection)
    try:
        with CaptureQueriesContext(connection) as queries, pytest.raises(RuntimeError) as error:
            executor.migrate([NEW])
        assert str(error.value) == (
            f"Normalized email exceeds the field length for Core UUIDs: {user.pk}"
        )
        assert not any(
            query["sql"].lstrip().upper().startswith(("ALTER ", "CREATE ", "DROP "))
            for query in queries
        )
        assert User.objects.values_list("email", flat=True).get(pk=user.pk) == user.email
        stored_address = EmailAddress.objects.get(pk=address.pk)
        assert stored_address.email == address.email
    finally:
        model.objects.filter(pk=user.pk if affected_model == "User" else address.pk).update(
            email="member@example.com"
        )


@pytest.mark.django_db(transaction=True)
def test_reverse_refuses_to_drop_phone_identity(legacy_schema):
    from django.db.migrations.exceptions import IrreversibleError

    MigrationExecutor(connection).migrate([NEW])
    apps = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
    user = apps.get_model("identity", "User").objects.create(
        email=None, login_phone_number="+84901234567", name="Phone", status="pending_activation"
    )
    try:
        with pytest.raises(IrreversibleError):
            MigrationExecutor(connection).migrate([OLD])
        user.refresh_from_db()
        assert user.email is None and user.login_phone_number == "+84901234567"
    finally:
        # Only test fixture data; allow fixture cleanup to return schema to latest.
        user.delete()


@pytest.mark.django_db(transaction=True)
def test_blank_legacy_email_addresses_do_not_collide_or_gain_verification(legacy_schema):
    User = legacy_schema.get_model("identity", "User")
    EmailAddress = legacy_schema.get_model("account", "EmailAddress")
    rows = []
    for index, blank in enumerate([" ", "  "]):
        user = User.objects.create(email=blank, name=f"Legacy {index}")
        address = EmailAddress.objects.create(user_id=user.pk, email=blank, verified=True)
        rows.append((user.pk, address.pk, blank))
    MigrationExecutor(connection).migrate([NEW])
    apps = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
    for user_id, address_id, blank in rows:
        assert apps.get_model("identity", "User").objects.get(pk=user_id).email is None
        address = apps.get_model("account", "EmailAddress").objects.get(pk=address_id)
        assert address.email == blank and address.verified


@pytest.mark.django_db(transaction=True)
def test_email_preflight_respects_database_collation_before_ddl(legacy_schema, monkeypatch):
    import unicodedata
    from importlib import import_module

    if connection.vendor != "sqlite":
        pytest.skip("Portable collation simulation uses SQLite; actual MySQL metadata needs MySQL")
    migration = import_module("identity.migrations.0004_phone_identity_activation")

    def key(text):
        return "".join(
            char
            for char in unicodedata.normalize("NFD", text.casefold())
            if not unicodedata.combining(char)
        )

    def compare(left, right):
        return (key(left) > key(right)) - (key(left) < key(right))

    connection.connection.create_collation("be01_accent_insensitive", compare)
    # Replace only backend metadata discovery; grouping/comparison still executes in DB.
    monkeypatch.setattr(
        migration,
        "_email_collations",
        lambda editor: {
            "identity_user": "be01_accent_insensitive",
            "account_emailaddress": "be01_accent_insensitive",
        },
        raising=False,
    )
    User = legacy_schema.get_model("identity", "User")
    first = User.objects.create(email="person@café.vn", name="One")
    second = User.objects.create(email=" person@cafe.vn", name="Two")
    try:
        with pytest.raises(RuntimeError) as error:
            MigrationExecutor(connection).migrate([NEW])
        assert str(first.pk) in str(error.value) and str(second.pk) in str(error.value)
        assert "person@" not in str(error.value)
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, "identity_user")
        assert "login_phone_number" not in {column.name for column in columns}
    finally:
        User.objects.filter(pk=second.pk).update(email="resolved@example.com")
        connection.connection.create_collation("be01_accent_insensitive", None)
