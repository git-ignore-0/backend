import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.urls import reverse

from identity.models import User


def test_pkce_is_required():
    assert settings.OAUTH2_PROVIDER["PKCE_REQUIRED"] is True
    assert settings.OAUTH2_PROVIDER["OAUTH2_VALIDATOR_CLASS"] == (
        "identity.oauth.CoreOAuth2Validator"
    )
    assert settings.OAUTH2_PROVIDER["REFRESH_TOKEN_REUSE_PROTECTION"] is True
    assert settings.OAUTH2_PROVIDER["ALLOWED_REDIRECT_URI_SCHEMES"] == ["http", "https"]


@pytest.mark.django_db
def test_compatibility_oauth_endpoints_do_not_redirect_for_trailing_slash(client):
    without_slash = client.post("/oauth/token", {})
    with_slash = client.post("/oauth/token/", {})

    assert without_slash.status_code == 400
    assert with_slash.status_code == 400


@pytest.mark.django_db
def test_authorize_endpoint_requires_login(client):
    response = client.get("/oauth/authorize?client_id=missing&response_type=code")
    assert response.status_code == 302
    assert response.url.startswith("/accounts/login/")


@pytest.mark.django_db
def test_authorize_screen_hint_opens_signup_and_preserves_oauth_request(client):
    response = client.get(
        "/oauth/authorize",
        {
            "client_id": "public-client",
            "response_type": "code",
            "redirect_uri": "http://localhost:3000/api/auth/callback",
            "scope": "profile email",
            "state": "public-state",
            "code_challenge": "challenge",
            "code_challenge_method": "S256",
            "screen_hint": "signup",
            "ui_locales": "en",
        },
    )

    assert response.status_code == 302
    assert response.url.startswith(f"{reverse('account_signup')}?next=")
    assert "screen_hint%3Dsignup" in response.url
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


def test_login_and_signup_templates_preserve_next(client):
    next_url = "/oauth/authorize?client_id=public-client&state=state"

    login = client.get("/login", {"next": next_url})
    assert parse_qs(urlparse(login.context["signup_url"]).query)["next"] == [next_url]

    signup = client.get("/register", {"next": next_url})
    assert signup.context["redirect_field_value"] == next_url
    assert f'value="{next_url.replace("&", "&amp;")}"' in signup.content.decode()


@pytest.mark.django_db
def test_pending_identity_cannot_enter_authorization_flow(client):
    user = User.objects.create_user(
        email="pending-oauth@example.com",
        name="Pending OAuth",
        password="StrongPassword12",
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/oauth/authorize")

    assert response.status_code == 302
    assert response.url == "/accounts/confirm-email/"


@pytest.mark.django_db
def test_authorization_code_pkce_flow_issues_token_usable_by_me_api(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    authorization_data = {
        "client_id": oauth_application.client_id,
        "response_type": "code",
        "redirect_uri": "https://forum.example.com/auth/nfv",
        "scope": "profile email",
        "state": "test-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }

    consent = client.get("/oauth/authorize", authorization_data)
    assert consent.status_code == 200

    authorized = client.post(
        "/oauth/authorize",
        {**authorization_data, "allow": "Authorize"},
    )
    assert authorized.status_code == 302
    query = parse_qs(urlparse(authorized.url).query)
    assert query["state"] == ["test-state"]

    token_response = client.post(
        "/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": authorization_data["redirect_uri"],
            "client_id": oauth_application.client_id,
            "client_secret": "test-forum-secret",
            "code_verifier": verifier,
        },
    )
    assert token_response.status_code == 200
    token_payload = token_response.json()
    assert set(token_payload["scope"].split()) == {"profile", "email"}

    profile = client.get(
        "/api/v1/me",
        HTTP_AUTHORIZATION=f"Bearer {token_payload['access_token']}",
    )
    assert profile.status_code == 200
    assert profile.json()["data"]["sub"] == str(active_user.pk)
