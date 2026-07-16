import pytest
from allauth.account.models import EmailAddress

from identity.models import AccountStatus, AuditLog, User


@pytest.mark.django_db
def test_active_user_can_log_in(client, active_user):
    response = client.post(
        "/login",
        {"login": active_user.email, "password": "StrongPassword12"},
    )

    assert response.status_code == 302
    assert response.url == "/account"
    assert client.session.get("_auth_user_id") == str(active_user.pk)
    assert AuditLog.objects.filter(user=active_user, action="auth.login").exists()


@pytest.mark.django_db
def test_suspended_user_cannot_log_in(client):
    user = User.objects.create_user(
        email="suspended@example.com",
        name="Suspended User",
        password="StrongPassword12",
        status=AccountStatus.SUSPENDED,
        is_active=False,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)

    response = client.post(
        "/login",
        {"login": user.email, "password": "StrongPassword12"},
    )

    assert response.status_code == 302
    assert response.url == "/accounts/inactive/"
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_pending_user_is_sent_to_email_verification(client):
    user = User.objects.create_user(
        email="pending@example.com",
        name="Pending User",
        password="StrongPassword12",
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/account")

    assert response.status_code == 302
    assert response.url == "/accounts/confirm-email/"
