import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import pytest
from allauth.account.models import EmailAddress
from django.conf import settings
from django.urls import reverse

from identity.models import User
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY


def test_pkce_is_required():
    assert settings.OAUTH2_PROVIDER["PKCE_REQUIRED"] is True
    assert settings.OAUTH2_PROVIDER["OAUTH2_VALIDATOR_CLASS"] == (
        "identity.oauth.CoreOAuth2Validator"
    )
    assert settings.OAUTH2_PROVIDER["REFRESH_TOKEN_REUSE_PROTECTION"] is True
    assert "REFRESH_TOKEN_GRACE_PERIOD_SECONDS" not in settings.OAUTH2_PROVIDER
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
            "referral_code": "nfv2345678",
        },
    )

    assert response.status_code == 302
    assert response.url.startswith(f"{reverse('account_signup')}?next=")
    preserved_request = parse_qs(urlparse(response.url).query)["next"][0]
    preserved_query = parse_qs(urlparse(preserved_request).query)
    assert urlparse(preserved_request).path == "/oauth/authorize"
    assert preserved_query["state"] == ["public-state"]
    assert preserved_query["code_challenge"] == ["challenge"]
    assert preserved_query["screen_hint"] == ["signup"]
    assert preserved_query["referral_code"] == ["nfv2345678"]
    assert client.session[SIGNUP_REFERRAL_SESSION_KEY] == "NFV2345678"
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


@pytest.mark.django_db
def test_invalid_authorize_referral_does_not_lock_signup(client):
    session = client.session
    session[SIGNUP_REFERRAL_SESSION_KEY] = "NFV2345678"
    session.save()

    response = client.get(
        "/oauth/authorize",
        {
            "client_id": "public-client",
            "response_type": "code",
            "screen_hint": "signup",
            "referral_code": "NFV23456I8",
        },
    )

    assert response.status_code == 302
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session

    signup = client.get(response.url)
    assert signup.status_code == 200
    assert "readonly" not in signup.context["form"].fields["referral_code"].widget.attrs


@pytest.mark.django_db
def test_signup_authorize_without_referral_clears_stale_signup_referral(client):
    capture_signup_referral(client)

    response = client.get(
        "/oauth/authorize",
        {
            "client_id": "public-client",
            "response_type": "code",
            "screen_hint": "signup",
        },
    )

    assert response.status_code == 302
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session
    signup = client.get(response.url)
    assert signup.context["form"]["referral_code"].value() in (None, "")
    assert "readonly" not in signup.context["form"].fields["referral_code"].widget.attrs


@pytest.mark.django_db
def test_non_signup_authorize_does_not_capture_referral(client):
    response = client.get(
        "/oauth/authorize",
        {
            "client_id": "public-client",
            "response_type": "code",
            "referral_code": "NFV2345678",
        },
    )

    assert response.status_code == 302
    assert response.url.startswith("/accounts/login/")
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_authenticated_authorize_does_not_capture_signup_referral(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(
        "/oauth/authorize",
        {
            "client_id": oauth_application.client_id,
            "response_type": "code",
            "redirect_uri": "https://forum.example.com/auth/nfv",
            "scope": "profile email",
            "state": "signed-in-state",
            "code_challenge": "a" * 43,
            "code_challenge_method": "S256",
            "screen_hint": "signup",
            "referral_code": "NFV2345678",
        },
    )

    assert response.status_code == 200
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session


@pytest.mark.django_db
def test_login_and_signup_templates_preserve_next(client):
    next_url = "/oauth/authorize?client_id=public-client&state=state"

    login = client.get("/login", {"next": next_url})
    assert parse_qs(urlparse(login.context["signup_url"]).query)["next"] == [next_url]

    signup = client.get("/register", {"next": next_url})
    assert signup.context["redirect_field_value"] == next_url
    assert f'value="{next_url.replace("&", "&amp;")}"' in signup.content.decode()


def capture_signup_referral(client):
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
            "referral_code": "nfv2345678",
        },
    )
    assert response.status_code == 302
    assert client.session[SIGNUP_REFERRAL_SESSION_KEY] == "NFV2345678"
    return parse_qs(urlparse(response.url).query)["next"][0]


def valid_signup_authorization(oauth_application, referral_code=None, locale="en"):
    data = {
        "client_id": oauth_application.client_id,
        "response_type": "code",
        "redirect_uri": "https://forum.example.com/auth/nfv",
        "scope": "profile email",
        "state": "signup-state",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "screen_hint": "signup",
        "ui_locales": locale,
    }
    if referral_code:
        data["referral_code"] = referral_code
    return data


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("locale", "heading", "action_label"),
    [
        ("en", "You are already signed in", "Sign out and create another account"),
        ("vi", "Bạn đang đăng nhập", "Đăng xuất và tạo tài khoản khác"),
    ],
)
def test_authenticated_signup_renders_account_choice_instead_of_consent(
    client, active_user, oauth_application, locale, heading, action_label
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = valid_signup_authorization(
        oauth_application,
        referral_code=active_user.referral_account.code,
        locale=locale,
    )

    response = client.get("/oauth/authorize", authorization)
    content = response.content.decode()

    assert response.status_code == 200
    assert "oauth2_provider/signup_account_conflict.html" in [
        template.name for template in response.templates
    ]
    assert response.headers["Cache-Control"] == "no-store"
    assert heading in content
    assert action_label in content
    assert active_user.email in content
    assert "Allow NFV Forum?" not in content
    assert "Cho phép NFV Forum?" not in content
    preserved = parse_qs(urlparse(response.context["oauth_next"]).query)
    assert preserved["state"] == ["signup-state"]
    assert preserved["code_challenge"] == ["a" * 43]
    assert preserved["referral_code"] == [active_user.referral_account.code]
    assert 'name="csrfmiddlewaretoken"' in content


@pytest.mark.django_db
def test_signup_account_choice_logout_preserves_referred_oauth_signup(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = valid_signup_authorization(
        oauth_application,
        referral_code=active_user.referral_account.code,
        locale="vi",
    )
    choice = client.get("/oauth/authorize", authorization)
    oauth_next = choice.context["oauth_next"]

    response = client.post(
        reverse("resolve-signup-account"),
        {"action": "logout", "next": oauth_next},
    )

    assert response.status_code == 302
    assert response.url == oauth_next
    assert "_auth_user_id" not in client.session

    signup_redirect = client.get(response.url)
    assert signup_redirect.status_code == 302
    assert signup_redirect.url.startswith(f"{reverse('account_signup')}?next=")
    assert client.session[SIGNUP_REFERRAL_SESSION_KEY] == active_user.referral_account.code
    signup = client.get(signup_redirect.url)
    field = signup.context["form"].fields["referral_code"]
    assert field.widget.attrs["readonly"] is True
    assert signup.context["form"]["referral_code"].value() == active_user.referral_account.code


@pytest.mark.django_db
def test_signup_account_choice_can_continue_with_current_account(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = valid_signup_authorization(
        oauth_application,
        referral_code=active_user.referral_account.code,
    )
    choice = client.get("/oauth/authorize", authorization)

    response = client.post(
        reverse("resolve-signup-account"),
        {"action": "continue", "next": choice.context["oauth_next"]},
    )

    assert response.status_code == 302
    continued = parse_qs(urlparse(response.url).query)
    assert "screen_hint" not in continued
    assert "referral_code" not in continued
    assert continued["state"] == [authorization["state"]]
    assert continued["code_challenge"] == [authorization["code_challenge"]]
    assert client.session.get("_auth_user_id") == str(active_user.pk)
    consent = client.get(response.url)
    assert "Allow NFV Forum?" in consent.content.decode()


@pytest.mark.django_db
def test_signup_account_choice_rejects_external_continuation_without_logout(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        reverse("resolve-signup-account"),
        {
            "action": "logout",
            "next": "https://attacker.example/oauth/authorize?screen_hint=signup",
        },
    )

    assert response.status_code == 400
    assert client.session.get("_auth_user_id") == str(active_user.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("login_path", ["/login", reverse("account_login")])
def test_successful_core_login_clears_captured_signup_referral(client, active_user, login_path):
    next_url = capture_signup_referral(client)

    response = client.post(
        login_path,
        {
            "login": active_user.email,
            "password": "StrongPassword12",
            "next": next_url,
        },
    )

    assert response.status_code == 302
    assert client.session.get("_auth_user_id") == str(active_user.pk)
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("login_path", ["/login", reverse("account_login")])
def test_failed_core_login_keeps_captured_signup_referral(client, active_user, login_path):
    next_url = capture_signup_referral(client)

    response = client.post(
        login_path,
        {
            "login": active_user.email,
            "password": "wrong-password",
            "next": next_url,
        },
    )

    assert response.status_code == 200
    assert "_auth_user_id" not in client.session
    assert client.session[SIGNUP_REFERRAL_SESSION_KEY] == "NFV2345678"


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
