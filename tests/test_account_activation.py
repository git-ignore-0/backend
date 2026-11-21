import pytest
from allauth.account.models import EmailAddress
from allauth.account.signals import email_confirmed
from django.contrib.auth.models import Group
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, connections, transaction

from identity import services
from identity.models import AccountStatus, AuditLog, IdentityEvent, User
from identity.services import InvalidAccountTransition, transition_account


def activate(user, source="email_verified", actor=None):
    return services.activate_account(user=user, source=source, actor=actor)


@pytest.fixture
def pending(db):
    return User.objects.create_user(email="pending@example.com", name="Pending")


@pytest.fixture
def operator(db):
    user = User.objects.create_user(email="admin@example.com", name="Admin", status="active")
    user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    return user


@pytest.mark.django_db
def test_pending_state_and_core_access_contract(pending):
    assert pending.status == "pending_activation"
    assert pending.is_active and pending.can_authenticate()
    assert not pending.can_access_core(email_verified=True)


@pytest.mark.django_db
@pytest.mark.parametrize("email", [None, "unverified@example.com"])
def test_active_core_access_does_not_require_verified_email(email):
    user = User.objects.create_user(
        email=email, login_phone_number="0901234567", name="Active", status="active"
    )
    assert user.can_authenticate()
    assert user.can_access_core()
    assert user.can_access_core(email_verified=False)
    assert not user.email_verified


@pytest.mark.django_db
def test_email_activation_is_idempotent_with_atomic_audit_event(pending):
    EmailAddress.objects.create(user=pending, email=pending.email, verified=True)
    first = activate(pending)
    second = activate(pending)
    assert first.status == second.status == "active"
    assert second.is_active
    event = IdentityEvent.objects.get(user=pending, type="user.activated")
    assert event.payload == {"user_id": str(pending.pk), "status": "active"}
    audit = AuditLog.objects.get(user=pending, action="user.activated")
    assert audit.context == {"source": "email_verified"}


@pytest.mark.django_db
def test_activation_cannot_trust_source_or_wrong_email(pending):
    EmailAddress.objects.create(user=pending, email="different@example.com", verified=True)
    with pytest.raises(InvalidAccountTransition):
        activate(pending)
    with pytest.raises(InvalidAccountTransition):
        activate(pending, "unknown")
    assert not IdentityEvent.objects.filter(user=pending).exists()
    assert not AuditLog.objects.filter(user=pending).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("has_email", [False, True])
def test_admin_activation_does_not_verify_email(pending, operator, has_email):
    if has_email:
        address = EmailAddress.objects.create(user=pending, email=pending.email, verified=False)
    else:
        pending.email = None
        pending.login_phone_number = "0901234567"
        pending.save()
    updated = activate(pending, "admin", operator)
    assert updated.status == "active" and updated.can_access_core()
    assert not updated.email_verified
    assert AuditLog.objects.get(user=pending, action="user.activated").context == {
        "source": "admin",
        "actor_user_id": str(operator.pk),
    }
    if has_email:
        address.refresh_from_db()
        assert not address.verified
    else:
        assert not pending.emailaddress_set.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["none", "regular", "staff", "suspended", "unsaved", "stale"])
def test_admin_activation_requires_current_authorized_actor(pending, operator, kind):
    if kind == "none":
        actor = None
    elif kind == "unsaved":
        actor = User(is_staff=True, is_superuser=True, status="active")
    elif kind == "stale":
        actor = operator
        actor.groups.clear()
    else:
        actor = User.objects.create_user(
            email="actor@example.com", name="Actor", status="active", is_staff=kind == "staff"
        )
        if kind == "suspended":
            actor.groups.add(Group.objects.get(name="website_admin"))
            transition_account(actor, AccountStatus.SUSPENDED)
    with pytest.raises(InvalidAccountTransition):
        activate(pending, "admin", actor)
    pending.refresh_from_db()
    assert pending.status == "pending_activation"


@pytest.mark.django_db
def test_superuser_may_activate_without_email_verification(pending):
    operator = User.objects.create_superuser("root@example.com", "secret")
    assert activate(pending, "admin", operator).status == "active"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["suspended", "deletion_requested", "anonymized"])
def test_confirmation_of_stale_pending_instance_does_not_unlock(pending, status):
    address = EmailAddress.objects.create(user=pending, email=pending.email, verified=True)
    transition_account(pending, status)
    email_confirmed.send(sender=EmailAddress, request=None, email_address=address)
    assert activate(pending).status == status
    pending.refresh_from_db()
    assert not pending.can_authenticate() and not pending.can_access_core(email_verified=True)
    assert not IdentityEvent.objects.filter(user=pending, type="user.activated").exists()


@pytest.mark.django_db
def test_confirmation_after_admin_activation_creates_no_extra_audit(pending, operator):
    address = EmailAddress.objects.create(user=pending, email=pending.email, verified=False)
    activate(pending, "admin", operator)
    address.verified = True
    address.save()
    email_confirmed.send(sender=EmailAddress, request=None, email_address=address)
    assert IdentityEvent.objects.filter(user=pending, type="user.activated").count() == 1
    assert AuditLog.objects.filter(user=pending, action="user.activated").count() == 1


@pytest.mark.django_db
def test_activation_rollback_includes_state_event_and_audit(pending, monkeypatch):
    EmailAddress.objects.create(user=pending, email=pending.email, verified=True)

    def fail(**kwargs):
        raise IntegrityError("audit write failed")

    monkeypatch.setattr(AuditLog.objects, "create", fail)
    with pytest.raises(IntegrityError):
        activate(pending)
    pending.refresh_from_db()
    assert pending.status == "pending_activation"
    assert not IdentityEvent.objects.filter(user=pending).exists()


@pytest.mark.django_db
def test_only_explicit_admin_reactivation_can_unlock_suspended(pending, operator):
    EmailAddress.objects.create(user=pending, email=pending.email, verified=True)
    transition_account(pending, "suspended")
    with pytest.raises(InvalidAccountTransition):
        transition_account(pending, "active")
    with pytest.raises(InvalidAccountTransition):
        activate(pending, "admin", operator)
    with pytest.raises(InvalidAccountTransition):
        transition_account(pending, "pending_activation")
    updated = transition_account(pending, "active", source="admin", actor=operator)
    assert updated.status == "active"
    assert AuditLog.objects.get(user=pending, action="user.activated").context["source"] == "admin"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["deletion_requested", "anonymized"])
def test_activation_does_not_restore_deleted_identity(pending, operator, status):
    transition_account(pending, status)
    with pytest.raises(InvalidAccountTransition):
        transition_account(pending, "active", source="admin", actor=operator)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "is_active"),
    [
        ("pending_activation", False),
        ("active", False),
        ("suspended", True),
        ("deletion_requested", True),
        ("anonymized", True),
        ("pending_verification", True),
    ],
)
def test_database_enforces_new_status_active_matrix(pending, status, is_active):
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=pending.pk).update(status=status, is_active=is_active)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Database has no row-level SELECT FOR UPDATE; stale/order tests still run on SQLite",
)
@pytest.mark.parametrize("second_action", ["activate", "suspend"])
def test_concurrent_activation_and_lifecycle_serialize_on_user_row(second_action):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    user = User.objects.create_user(email="race@example.com", name="Race")
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    start = Barrier(3)

    def run(action):
        close_old_connections()
        try:
            start.wait(timeout=10)
            if action == "activate":
                return activate(user).status
            return transition_account(user, "suspended").status
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            first = pool.submit(run, "activate")
            second = pool.submit(run, second_action)
            start.wait(timeout=10)
        first.result(timeout=15)
        second.result(timeout=15)
    user.refresh_from_db()
    events = IdentityEvent.objects.filter(user=user, type="user.activated").count()
    audits = AuditLog.objects.filter(user=user, action="user.activated").count()
    if second_action == "activate":
        assert user.status == "active" and events == audits == 1
    else:
        assert user.status == "suspended" and not user.is_active
        assert events == audits <= 1


@pytest.mark.django_db
def test_legacy_same_active_transition_is_noop_for_phone_only_user(pending, operator):
    pending.email = None
    pending.login_phone_number = "0901234567"
    pending.save()
    activate(pending, "admin", operator)
    assert transition_account(pending, "active").status == "active"
    assert IdentityEvent.objects.filter(user=pending, type="user.activated").count() == 1
    assert AuditLog.objects.filter(user=pending, action="user.activated").count() == 1


@pytest.mark.django_db(transaction=True)
def test_email_verification_requires_exact_address_even_with_accent_insensitive_database(
    monkeypatch,
):
    import sqlite3
    import unicodedata

    user = User.objects.create_user(email="person@café.vn", name="Member")
    EmailAddress.objects.create(user=user, email="person@cafe.vn", verified=True)
    # Patch the wrapper that owns the attribute so monkeypatch can restore it.
    db = connections[DEFAULT_DB_ALIAS]
    if db.vendor == "sqlite":
        # A separate handle models MySQL accent-insensitive LIKE without changing the
        # test connection's built-in LIKE or its schema/data.
        def equivalent(pattern, value):
            def key(text):
                return "".join(
                    char
                    for char in unicodedata.normalize("NFD", text.casefold())
                    if not unicodedata.combining(char)
                )

            return key(pattern) == key(value)

        raw = sqlite3.connect(db.settings_dict["NAME"], uri=True)
        try:
            raw.create_function(
                "like", 3, lambda pattern, value, escape: equivalent(pattern, value)
            )
            with monkeypatch.context() as context:
                context.setattr(db, "connection", raw)
                assert not user.email_verified
        finally:
            raw.close()
    else:
        assert not user.email_verified
