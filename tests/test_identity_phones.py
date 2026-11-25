import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from config.phones import format_vietnam_phone
from identity.models import User


def normalize(value):
    from identity.phones import normalize_login_phone

    return normalize_login_phone(value)


@pytest.mark.parametrize(
    "value",
    ["0901234567", "84901234567", "+84901234567", " (090) 123-45.67 ", "+84 (90) 123 4567"],
)
def test_equivalent_phone_presentations(value):
    assert normalize(value) == "+84901234567"


def test_vietnam_landline():
    assert normalize("024 1234 5678") == "+842412345678"


@pytest.mark.parametrize(
    ("stored", "display"),
    [
        ("+84981234567", "0981234567"),
        ("+842412345678", "02412345678"),
        ("0981234567", "0981234567"),
        ("legacy contact", "legacy contact"),
        (None, None),
        ("", ""),
    ],
)
def test_format_phone_for_display_preserves_missing_and_legacy_values(stored, display):
    assert format_vietnam_phone(stored) == display


@pytest.mark.parametrize(
    "value",
    [
        "",
        "12345",
        "+1901234567",
        "090123456",
        "090123456789",
        "0601234567",
        "090a1234567",
        "090/1234567",
        "090_1234567",
        "０９０１２３４５６７",
        "090\n1234567",
        "++84901234567",
        "+840901234567",
        None,
        901234567,
    ],
)
def test_invalid_phone_is_rejected(value):
    with pytest.raises(ValidationError):
        normalize(value)


@pytest.mark.django_db
def test_phone_only_accounts_hash_password_and_store_null_email():
    first = User.objects.create_user(login_phone_number="0901234567", name="One", password="secret")
    second = User.objects.create_user(email="  ", login_phone_number="0912345678", name="Two")
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.email is None and second.email is None
    assert first.login_phone_number == "+84901234567"
    assert first.check_password("secret")
    assert not first.emailaddress_set.exists()
    assert str(first) == "+84901234567"
    assert first.email_verified is False
    unidentified = User(email=None)
    assert str(unidentified) == str(unidentified.pk)


@pytest.mark.django_db
def test_model_save_and_clean_normalize_identifiers_without_forms():
    user = User(email="  Mixed@EXAMPLE.COM ", login_phone_number="090 123 4567", name="Member")
    user.full_clean(exclude={"password"})
    user.save()
    user.refresh_from_db()
    assert user.email == "mixed@example.com"
    assert user.login_phone_number == "+84901234567"
    user.email = " "
    user.login_phone_number = "091-234-5678"
    user.save(update_fields=["email", "login_phone_number"])
    user.refresh_from_db()
    assert user.email is None
    assert user.login_phone_number == "+84912345678"
    user.login_phone_number = "invalid"
    with pytest.raises(ValidationError):
        user.save()


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["phone", "email"])
def test_database_rejects_duplicate_normalized_identifiers_without_full_clean(kind):
    User.objects.create(email=" Dupe@EXAMPLE.COM ", login_phone_number="0901234567", name="One")
    duplicate = (
        User(email="other@example.com", login_phone_number="+84 (90) 1234567", name="Two")
        if kind == "phone"
        else User(email="DUPE@example.com", name="Two")
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate.save()


@pytest.mark.django_db
def test_email_verified_requires_current_matching_address():
    from allauth.account.models import EmailAddress

    user = User.objects.create_user(email="first@example.com", name="Member")
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    assert user.email_verified
    user.email = "new@example.com"
    user.save()
    assert not user.email_verified
    user.email = None
    user.save()
    assert not user.email_verified


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "sqlite", reason="SQLite schema race")
def test_simultaneous_equivalent_phones_hit_database_unique_constraint(tmp_path):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from uuid import uuid4

    # Copy only the actual migrated User DDL, not any local/member data.
    with connection.cursor() as cursor:
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='identity_user'")
        ddl = cursor.fetchone()[0]
    database = tmp_path / "phone-race.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute(ddl)
    start = Barrier(2)

    def insert(value):
        phone = normalize(value)
        with sqlite3.connect(database, timeout=10) as db:
            start.wait(timeout=5)
            try:
                db.execute(
                    "INSERT INTO identity_user "
                    "(id, email, login_phone_number, password, is_superuser, name, status, locale, "
                    "is_staff, is_active, date_joined, updated_at) "
                    "VALUES (?, NULL, ?, '', 0, 'Race', 'pending_activation', 'vi', 0, 1, ?, ?)",
                    (uuid4().hex, phone, "2026-11-20 00:00:00", "2026-11-20 00:00:00"),
                )
                return "created"
            except sqlite3.IntegrityError:
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(insert, ["0901234567", "+84 (90) 1234567"]))
    assert sorted(results) == ["created", "duplicate"]
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT login_phone_number FROM identity_user").fetchall() == [
            ("+84901234567",)
        ]


@pytest.mark.django_db
def test_manager_and_model_save_use_same_unicode_email_normalization():
    User.objects.create(email=" Member@ｅxample.com ", name="Direct")
    with pytest.raises(ValidationError):
        User.objects.create_user(email=" Member@ｅxample.com ", name="Manager")
    assert list(User.objects.values_list("email", flat=True)) == ["member@ｅxample.com"]


@pytest.mark.django_db
@pytest.mark.parametrize("phone", [None, "", "   "])
@pytest.mark.parametrize("write_path", ["objects_create", "full_clean_save", "save"])
def test_optional_phone_model_writes_store_null(phone, write_path):
    fields = {"email": "legacy@example.com", "name": "Legacy", "login_phone_number": phone}
    if write_path == "objects_create":
        user = User.objects.create(**fields)
    else:
        user = User(**fields)
        user.set_unusable_password()
        if write_path == "full_clean_save":
            user.full_clean()
        user.save()
    user.refresh_from_db()
    assert user.email == "legacy@example.com"
    assert user.login_phone_number is None


@pytest.mark.django_db
@pytest.mark.parametrize("phone", [None, "", "   "])
def test_partial_save_clears_optional_phone_to_null(phone):
    user = User.objects.create_user(
        email="legacy@example.com", name="Legacy", login_phone_number="0901234567"
    )
    user.login_phone_number = phone
    user.save(update_fields=["login_phone_number"])
    user.refresh_from_db()
    assert user.login_phone_number is None
    assert user.email == "legacy@example.com"


@pytest.mark.django_db
@pytest.mark.parametrize("phone", ["abc", "09012", "( )-.", " \t ", "\u00a0"])
@pytest.mark.parametrize("write_path", ["manager", "model_save"])
def test_optional_phone_does_not_hide_invalid_nonempty_values(phone, write_path):
    create = User.objects.create_user if write_path == "manager" else User.objects.create
    before = User.objects.count()
    with pytest.raises(ValidationError):
        create(email="legacy@example.com", name="Legacy", login_phone_number=phone)
    assert User.objects.count() == before


def test_standalone_normalizer_still_rejects_ascii_spaces():
    with pytest.raises(ValidationError):
        normalize("   ")


@pytest.mark.django_db
def test_model_optional_phone_does_not_require_identifier_for_anonymized_user():
    user = User.objects.create(
        email=None,
        login_phone_number="   ",
        name="Anonymized",
        status="anonymized",
        is_active=False,
    )
    user.refresh_from_db()
    assert user.email is None and user.login_phone_number is None
    assert user.status == "anonymized" and not user.is_active
