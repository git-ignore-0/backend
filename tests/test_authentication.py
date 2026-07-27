import pytest
from allauth.account import app_settings
from allauth.account.models import EmailAddress
from django.db import IntegrityError, transaction
from django.utils import timezone

from identity.models import AccountStatus, AuditLog, User
from members.models import MemberProfile


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


@pytest.mark.django_db
def test_account_page_does_not_create_empty_member_profile(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/account")

    assert response.status_code == 200
    assert response.context["profile_form"]["name"].value() == active_user.name
    content = response.content.decode()
    assert 'class="card account-card"' in content
    assert 'id="profile-title"' in content
    assert 'aria-labelledby="security-title"' in content
    assert 'autocomplete="name"' in content
    assert 'autocomplete="address-line1"' in content
    assert "data-province-select" in content
    assert "data-ward-select" in content
    assert 'value="01">Thành phố Hà Nội</option>' in content
    assert 'data-wards-url="/api/v1/administrative-units/wards"' in content
    assert "data-profile-submit disabled" in content
    assert content.count("required") == 6
    assert 'type="date"' in content
    assert f'max="{timezone.localdate().isoformat()}"' in content
    assert 'type="tel"' in content
    assert "data-phone-input" in content
    assert 'src="/static/js/account.js"' in content
    assert "Manage email" not in content
    assert "/accounts/email/" not in content
    assert not MemberProfile.objects.filter(user=active_user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_account_email_management_is_disabled(client, active_user, method):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = getattr(client, method)("/accounts/email/")

    assert response.status_code == 302
    assert response.url == "/account"
    assert app_settings.MAX_EMAIL_ADDRESSES == 1
    assert not EmailAddress.objects.can_add_email(active_user)
    assert EmailAddress.objects.filter(user=active_user).count() == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        EmailAddress.objects.create(
            user=active_user,
            email="secondary@example.com",
            primary=False,
            verified=False,
        )


@pytest.mark.django_db
def test_account_page_updates_shared_identity_and_member_profile(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/account",
        {
            "name": "Nông Dân Mới",
            "phone_number": "+84 912 345 678",
            "date_of_birth": "1990-06-15",
            "province_code": "66",
            "ward_code": "22015",
            "address": "Số 12, ngõ 5",
            "email": "attacker@example.com",
        },
    )

    assert response.status_code == 302
    assert response.url == "/account"
    active_user.refresh_from_db()
    assert active_user.name == "Nông Dân Mới"
    assert active_user.email == "member@example.com"
    profile = MemberProfile.objects.get(user=active_user)
    assert profile.phone_number == "+84 912 345 678"
    assert profile.date_of_birth.isoformat() == "1990-06-15"
    assert profile.province_code == "66"
    assert profile.ward_code == "22015"
    assert profile.address == "Số 12, ngõ 5"


@pytest.mark.django_db
def test_account_page_rejects_invalid_profile_fields(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/account",
        {
            "name": active_user.name,
            "phone_number": "call-me",
            "date_of_birth": (timezone.localdate() + timezone.timedelta(days=1)).isoformat(),
            "province_code": "",
            "ward_code": "",
            "address": "",
        },
    )

    assert response.status_code == 200
    assert set(response.context["profile_form"].errors) == {
        "phone_number",
        "date_of_birth",
        "province_code",
        "ward_code",
        "address",
    }
    assert not MemberProfile.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_account_page_rejects_ward_from_another_province(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/account",
        {
            "name": active_user.name,
            "phone_number": "",
            "date_of_birth": "",
            "province_code": "01",
            "ward_code": "22015",
            "address": "Số 12",
        },
    )

    assert response.status_code == 200
    assert "ward_code" in response.context["profile_form"].errors
    assert not MemberProfile.objects.filter(user=active_user).exists()


def test_administrative_wards_endpoint_returns_localized_wards(client):
    response = client.get(
        "/api/v1/administrative-units/wards",
        {"province": "01"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    assert response.json()["data"][0] == {"code": "00004", "name": "Ba Dinh Ward"}


def test_administrative_wards_endpoint_rejects_unknown_province(client):
    response = client.get(
        "/api/v1/administrative-units/wards",
        {"province": "00"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_province"}
