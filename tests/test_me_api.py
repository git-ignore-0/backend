import pytest
from django.contrib.auth.models import Group
from django.utils import timezone
from oauth2_provider.models import AccessToken

from identity.models import AccountStatus
from members.models import MemberProfile


@pytest.mark.django_db
def test_profile_scope_returns_stable_core_identity(client, active_user, access_token):
    group = Group.objects.create(name="member")
    active_user.groups.add(group)
    MemberProfile.objects.create(
        user=active_user,
        phone_number="+84 912 345 678",
        date_of_birth="1990-06-15",
    )

    response = client.get(
        "/api/v1/me",
        HTTP_AUTHORIZATION=f"Bearer {access_token.token}",
    )

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    assert response.json() == {
        "data": {
            "sub": str(active_user.pk),
            "name": active_user.name,
            "email": active_user.email,
            "email_verified": True,
            "login_phone_number": None,
            "locale": "vi",
            "status": "active",
            "roles": ["member"],
            "phone_number": "",
            "date_of_birth": "1990-06-15",
        }
    }


@pytest.mark.django_db
def test_profile_scope_returns_empty_optional_profile_without_creating_row(
    client, active_user, access_token
):
    assert not MemberProfile.objects.filter(user=active_user).exists()

    response = client.get(
        "/api/v1/me",
        HTTP_AUTHORIZATION=f"Bearer {access_token.token}",
    )

    assert response.status_code == 200
    assert response.json()["data"]["phone_number"] == ""
    assert response.json()["data"]["date_of_birth"] is None
    assert not MemberProfile.objects.filter(user=active_user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("legacy_phone", [None, "0987654321"])
def test_me_prefers_login_phone_with_or_without_legacy_profile(
    client, active_user, access_token, legacy_phone
):
    active_user.login_phone_number = "0912345678"
    active_user.save(update_fields=["login_phone_number"])
    if legacy_phone is not None:
        MemberProfile.objects.create(user=active_user, phone_number=legacy_phone)

    response = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["phone_number"] == data["login_phone_number"] == "+84912345678"
    if legacy_phone is None:
        assert not MemberProfile.objects.filter(user=active_user).exists()
    else:
        assert active_user.member_profile.phone_number == legacy_phone


@pytest.mark.django_db
def test_suspended_identity_is_rejected_with_valid_existing_token(
    client, active_user, access_token
):
    active_user.status = AccountStatus.SUSPENDED
    active_user.is_active = False
    active_user.save(update_fields=["status", "is_active", "updated_at"])

    response = client.get(
        "/api/v1/me",
        HTTP_AUTHORIZATION=f"Bearer {access_token.token}",
    )

    assert response.status_code == 403
    assert response.json() == {"code": "account_inactive"}
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["profile", "email"])
def test_profile_and_email_scopes_are_required(client, active_user, oauth_application, scope):
    token = AccessToken.objects.create(
        user=active_user,
        application=oauth_application,
        token=f"only-{scope}-scope",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope=scope,
    )

    response = client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {token.token}")

    assert response.status_code == 403
    assert response.json() == {"code": "insufficient_scope"}
    assert 'error="insufficient_scope"' in response["WWW-Authenticate"]


@pytest.mark.django_db
def test_invalid_token_is_rejected(client):
    response = client.get("/api/v1/me", HTTP_AUTHORIZATION="Bearer unknown")
    assert response.status_code == 401
    assert response.json() == {"code": "invalid_token"}
    assert response["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert response["Cache-Control"] == "no-store"


@pytest.mark.django_db
def test_django_session_does_not_replace_bearer(client, active_user):
    for authenticated in (False, True):
        if authenticated:
            client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
        result = client.get("/api/v1/me")
        assert result.status_code == 401
        assert result.json() == {"code": "invalid_token"}
