import pytest
from allauth.account.models import EmailAddress
from django.utils import timezone
from oauth2_provider.models import AccessToken, Application

from identity.models import AccountStatus, User


@pytest.fixture
def active_user(db):
    user = User.objects.create_user(
        email="member@example.com",
        name="Nông Dân Xanh",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


@pytest.fixture
def oauth_application(db, active_user):
    return Application.objects.create(
        name="NFV Forum",
        user=active_user,
        client_id="test-forum-client",
        client_secret="test-forum-secret",
        hash_client_secret=False,
        client_type=Application.CLIENT_CONFIDENTIAL,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris="https://forum.example.com/auth/nfv",
    )


@pytest.fixture
def access_token(db, active_user, oauth_application):
    return AccessToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="valid-test-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )
