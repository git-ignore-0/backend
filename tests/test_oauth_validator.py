from types import SimpleNamespace

import pytest
from django.utils import timezone
from oauth2_provider.models import Grant, RefreshToken

from identity.models import AccountStatus
from identity.oauth import CoreOAuth2Validator


@pytest.mark.django_db
def test_active_user_can_use_current_refresh_token(active_user, oauth_application, access_token):
    RefreshToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="current-refresh-token",
        access_token=access_token,
    )
    oauth_request = SimpleNamespace(user=None)

    valid = CoreOAuth2Validator().validate_refresh_token(
        "current-refresh-token",
        oauth_application,
        oauth_request,
    )

    assert valid is True


@pytest.mark.django_db
def test_suspended_user_cannot_exchange_existing_authorization_code(active_user, oauth_application):
    Grant.objects.create(
        user=active_user,
        application=oauth_application,
        code="authorization-code-before-suspension",
        expires=timezone.now() + timezone.timedelta(minutes=1),
        redirect_uri="https://forum.example.com/auth/nfv",
        scope="profile email",
    )
    active_user.status = AccountStatus.SUSPENDED
    active_user.is_active = False
    active_user.save(update_fields=["status", "is_active", "updated_at"])
    oauth_request = SimpleNamespace(user=None, scopes=None)

    valid = CoreOAuth2Validator().validate_code(
        oauth_application.client_id,
        "authorization-code-before-suspension",
        oauth_application,
        oauth_request,
    )

    assert valid is False


@pytest.mark.django_db
def test_suspended_user_cannot_use_existing_refresh_token(
    active_user, oauth_application, access_token
):
    RefreshToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="refresh-token-before-suspension",
        access_token=access_token,
    )
    active_user.status = AccountStatus.SUSPENDED
    active_user.is_active = False
    active_user.save(update_fields=["status", "is_active", "updated_at"])
    oauth_request = SimpleNamespace(user=None)

    valid = CoreOAuth2Validator().validate_refresh_token(
        "refresh-token-before-suspension",
        oauth_application,
        oauth_request,
    )

    assert valid is False


@pytest.mark.django_db
def test_expired_refresh_token_is_rejected(active_user, oauth_application, access_token):
    refresh_token = RefreshToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="expired-refresh-token",
        access_token=access_token,
    )
    RefreshToken.objects.filter(pk=refresh_token.pk).update(
        created=timezone.now() - timezone.timedelta(days=31)
    )
    oauth_request = SimpleNamespace(user=None)

    valid = CoreOAuth2Validator().validate_refresh_token(
        "expired-refresh-token",
        oauth_application,
        oauth_request,
    )

    assert valid is False
