import pytest

from identity.models import AccountStatus, User


@pytest.mark.django_db
def test_user_manager_requires_at_least_one_identifier():
    with pytest.raises(ValueError, match="Email or login phone number is required"):
        User.objects.create_user(email="", name="Missing Email", password="StrongPassword12")


@pytest.mark.django_db
def test_superuser_manager_sets_required_flags():
    user = User.objects.create_superuser(
        email="ADMIN@example.com",
        name="Core Admin",
        password="StrongPassword12",
    )

    assert user.email == "admin@example.com"
    assert user.status == AccountStatus.ACTIVE
    assert user.is_active is True
    assert user.is_staff is True
    assert user.is_superuser is True


@pytest.mark.django_db
def test_superuser_manager_rejects_missing_privilege_flag():
    with pytest.raises(ValueError, match="must have is_staff"):
        User.objects.create_superuser(
            email="admin@example.com",
            name="Core Admin",
            password="StrongPassword12",
            is_staff=False,
        )


@pytest.mark.django_db
def test_legacy_positional_email_caller_still_works():
    user = User.objects.create_user(" Old@EXAMPLE.COM ", "secret", name="Legacy")
    assert user.email == "old@example.com"
    assert user.check_password("secret")


@pytest.mark.django_db
@pytest.mark.parametrize("email", [None, "", "  "])
def test_superuser_requires_email_even_with_phone(email):
    with pytest.raises(ValueError, match="Email is required"):
        User.objects.create_superuser(
            email=email, password="secret", login_phone_number="0901234567"
        )


@pytest.mark.django_db
@pytest.mark.parametrize("flag", ["is_staff", "is_superuser"])
def test_superuser_rejects_either_missing_privilege(flag):
    with pytest.raises(ValueError):
        User.objects.create_superuser("admin@example.com", "secret", **{flag: False})


@pytest.mark.django_db
@pytest.mark.parametrize("identifier", ["email", "phone"])
def test_manager_rejects_equivalent_duplicate_identifiers(identifier):
    from django.core.exceptions import ValidationError

    User.objects.create_user("first@example.com", name="One", login_phone_number="0901234567")
    fields = (
        {"email": " FIRST@EXAMPLE.COM "}
        if identifier == "email"
        else {"login_phone_number": "+84 (90) 123 4567"}
    )
    with pytest.raises(ValidationError):
        User.objects.create_user(name="Two", **fields)


@pytest.mark.django_db
def test_createsuperuser_cli_still_uses_email(monkeypatch):
    from django.core.management import call_command

    monkeypatch.setenv("DJANGO_SUPERUSER_PASSWORD", "cli-test-password")
    call_command(
        "createsuperuser", email="CLI@example.com", name="CLI", interactive=False, verbosity=0
    )
    user = User.objects.get(email="cli@example.com")
    assert User.USERNAME_FIELD == "email"
    assert user.is_staff and user.is_superuser and user.is_active
    assert user.check_password("cli-test-password")
    assert not user.email_verified


@pytest.mark.django_db
@pytest.mark.parametrize("phone", [None, "", "   "])
def test_email_only_manager_accounts_store_missing_phone_as_null(phone):
    first = User.objects.create_user(
        email=" First@EXAMPLE.COM ", name="First", login_phone_number=phone
    )
    second = User.objects.create_user(
        email="second@example.com", name="Second", login_phone_number=phone
    )
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.email == "first@example.com"
    assert second.email == "second@example.com"
    assert first.login_phone_number is None and second.login_phone_number is None
    assert first.pk != second.pk


@pytest.mark.django_db
@pytest.mark.parametrize("email", [None, "", "   "])
@pytest.mark.parametrize("phone", [None, "", "   "])
def test_manager_rejects_missing_identifiers_after_normalization(email, phone):
    before = User.objects.count()
    with pytest.raises(ValueError, match="^Email or login phone number is required$"):
        User.objects.create_user(email=email, name="Missing", login_phone_number=phone)
    assert User.objects.count() == before
