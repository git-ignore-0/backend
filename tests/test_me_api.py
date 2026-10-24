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
            "locale": "vi",
            "status": "active",
            "roles": ["member"],
            "phone_number": "+84 912 345 678",
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
def test_profile_and_email_scopes_are_required(client, active_user, oauth_application):
    token = AccessToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="missing-email-scope",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile",
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
