import pytest

from identity.models import AccountStatus, User


@pytest.mark.django_db
def test_user_manager_requires_email():
    with pytest.raises(ValueError, match="Email is required"):
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
