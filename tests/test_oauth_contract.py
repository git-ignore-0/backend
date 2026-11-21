import base64
import hashlib
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.db import connection
from django.db.models.signals import pre_save
from django.test import Client
from django.urls import get_script_prefix, reverse, set_script_prefix
from django.utils import timezone
from oauth2_provider.exceptions import FatalClientError
from oauth2_provider.models import AccessToken, Grant
from oauthlib.oauth2.rfc6749.errors import InvalidRequestFatalError

from identity.models import AccountStatus, User
from identity.navigation import PUBLIC_RETURN_TO_SESSION_KEY
from identity.services import transition_account
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY
from members.models import MemberProfile


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
@pytest.mark.parametrize(
    "application_case", ["registered", "unknown_client", "unregistered_callback"]
)
def test_authorization_error_returns_to_the_application_only_when_it_matches(
    client, active_user, oauth_application, settings, monkeypatch, application_case
):
    origin = "https://public.example.com"
    settings.PUBLIC_SITE_ORIGINS = (origin,)
    callback = f"{origin}/api/auth/callback"
    oauth_application.redirect_uris = callback
    oauth_application.save(update_fields=["redirect_uris"])
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    session = client.session
    session[PUBLIC_RETURN_TO_SESSION_KEY] = f"{origin}/account/vi"
    session.save()

    def reject_request(self, request):
        raise FatalClientError(error=InvalidRequestFatalError())

    monkeypatch.setattr(
        "identity.oauth_views.CoreAuthorizationView.validate_authorization_request", reject_request
    )
    client_id = "unknown" if application_case == "unknown_client" else oauth_application.client_id
    redirect_uri = (
        f"{origin}/other-callback" if application_case == "unregistered_callback" else callback
    )
    response = client.get(
        reverse("authorize"),
        {"client_id": client_id, "redirect_uri": redirect_uri},
    )

    assert response.status_code == 400
    html = response.content.decode()
    expected = f"{origin}/account/vi" if application_case == "registered" else reverse("account")
    assert f'class="auth-button auth-button-primary" href="{expected}"' in html
    assert "Quay lại website" not in html


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
    assert b'name="referral_code"' not in signup.content


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
    assert b'name="referral_code"' not in signup.content


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
@pytest.mark.parametrize(
    ("email", "label"),
    [(None, "+84912345678"), ("member-choice@example.com", "member-choice@example.com")],
    ids=["phone-only", "email-and-phone"],
)
def test_signup_account_choice_displays_preferred_account_label(
    client, oauth_application, email, label
):
    user = User.objects.create_user(
        email=email,
        login_phone_number="0912345678",
        name="Account choice member",
        status=AccountStatus.ACTIVE,
    )
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/oauth/authorize", valid_signup_authorization(oauth_application))

    assert response.status_code == 200
    assert f"Signed in as <strong>{label}</strong>." in response.content.decode()


@pytest.fixture(params=["", "/core"], ids=["root", "mounted"])
def script_mount(request):
    prefix = request.param
    original = get_script_prefix()
    # WSGI sets the reverse() prefix from SCRIPT_NAME.
    set_script_prefix(prefix + "/")
    try:
        yield prefix
    finally:
        set_script_prefix(original)


@pytest.mark.django_db
def test_signup_account_choice_logout_preserves_referred_oauth_signup(
    client, active_user, oauth_application, script_mount, settings
):
    settings.PUBLIC_SITE_ORIGINS = ("https://forum.example.com",)
    client.defaults["SCRIPT_NAME"] = script_mount
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = valid_signup_authorization(
        oauth_application,
        referral_code=active_user.referral_account.code,
        locale="vi",
    )
    choice = client.get("/oauth/authorize", authorization)
    oauth_next = choice.context["oauth_next"]

    response = client.post(
        "/oauth/signup-account/",
        {"action": "logout", "next": oauth_next},
    )

    assert response.status_code == 302
    assert response.url == oauth_next
    assert "_auth_user_id" not in client.session

    signup_redirect = client.get(response.url.removeprefix(script_mount))
    assert signup_redirect.status_code == 302
    assert signup_redirect.url.startswith(f"{reverse('account_signup')}?next=")
    assert client.session[SIGNUP_REFERRAL_SESSION_KEY] == active_user.referral_account.code
    signup = client.get(signup_redirect.url.removeprefix(script_mount))
    field = signup.context["form"].fields["referral_code"]
    assert field.widget.attrs["readonly"] is True
    assert signup.context["form"]["referral_code"].value() == active_user.referral_account.code
    assert client.session[PUBLIC_RETURN_TO_SESSION_KEY] == "https://forum.example.com/"


@pytest.mark.django_db
def test_signup_account_choice_can_continue_with_current_account(
    client, active_user, oauth_application, script_mount
):
    client.defaults["SCRIPT_NAME"] = script_mount
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = valid_signup_authorization(
        oauth_application,
        referral_code=active_user.referral_account.code,
    )
    choice = client.get("/oauth/authorize", authorization)

    response = client.post(
        "/oauth/signup-account/",
        {"action": "continue", "next": choice.context["oauth_next"]},
    )

    assert response.status_code == 302
    parsed = urlparse(response.url)
    assert parsed.path == script_mount + "/oauth/authorize"
    assert parse_qs(parsed.query) == {
        key: [value]
        for key, value in authorization.items()
        if key not in {"screen_hint", "referral_code"}
    }
    assert client.session.get("_auth_user_id") == str(active_user.pk)
    consent = client.get(response.url.removeprefix(script_mount))
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


VERIFIER = "a" * 64


def authorization_request(application):
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
    )
    return {
        "client_id": application.client_id,
        "response_type": "code",
        "redirect_uri": application.redirect_uris,
        "scope": "profile email",
        "state": "test-state",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "ui_locales": "en",
    }


@pytest.mark.django_db
def test_consent_logout_can_sign_back_in_and_resume_oauth(
    active_user, oauth_application, script_mount
):
    browser = Client(enforce_csrf_checks=True, SCRIPT_NAME=script_mount)
    browser.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = authorization_request(oauth_application)
    consent = browser.get("/oauth/authorize", authorization)
    assert consent.status_code == 200
    continuation = consent.wsgi_request.get_full_path()
    logout_path = "/accounts/logout/"
    logout_url = script_mount + logout_path
    assert f'action="{logout_url}"' in consent.content.decode()
    assert browser.post(logout_path, {"next": continuation}).status_code == 403
    assert browser.session[SESSION_KEY] == str(active_user.pk)

    logout_response = browser.post(
        logout_path,
        {"next": continuation, "csrfmiddlewaretoken": consent.context["csrf_token"]},
    )
    assert logout_response.url == continuation
    assert SESSION_KEY not in browser.session

    login_redirect = browser.get(continuation.removeprefix(script_mount))
    login_url = urlparse(login_redirect.url)
    assert login_url.path == script_mount + "/accounts/login/"
    assert parse_qs(login_url.query) == {"next": [continuation]}
    login_path = login_url.path.removeprefix(script_mount)
    login = browser.get(login_redirect.url.removeprefix(script_mount))
    signed_in = browser.post(
        login_path,
        {
            "next": continuation,
            "login": active_user.email,
            "password": "StrongPassword12",
            "csrfmiddlewaretoken": login.context["csrf_token"],
        },
    )
    assert signed_in.url == continuation
    resumed = browser.get(signed_in.url.removeprefix(script_mount))
    assert resumed.status_code == 200
    form = resumed.context["form"]
    for field in (
        "state",
        "client_id",
        "redirect_uri",
        "scope",
        "code_challenge",
        "code_challenge_method",
    ):
        assert form[field].value() == authorization[field]


def authorize(client, authorization_data):
    consent = client.get("/oauth/authorize", authorization_data)
    assert consent.status_code == 200
    assert consent.wsgi_request.LANGUAGE_CODE == "en"
    assert "oauth2_provider/authorize.html" in [template.name for template in consent.templates]

    response = client.post("/oauth/authorize", {**authorization_data, "allow": "Authorize"})
    assert response.status_code == 302
    parsed = urlparse(response.url)
    assert parsed.netloc == "forum.example.com" and parsed.path == "/auth/nfv"
    query = parse_qs(parsed.query)
    assert query["state"] == ["test-state"]
    return query["code"][0]


def exchange(client, application, code, **overrides):
    return client.post(
        "/oauth/token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": application.redirect_uris,
            "client_id": application.client_id,
            "client_secret": "test-forum-secret",
            "code_verifier": VERIFIER,
            **overrides,
        },
    )


def refresh(client, application, token):
    return client.post(
        "/oauth/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": application.client_id,
            "client_secret": "test-forum-secret",
        },
    )


def native_approval(client, application, method):
    data = authorization_request(application)
    if method == "post":
        return client.post("/oauth/authorize", {**data, "allow": "Authorize"})
    if method == "auto":
        data["approval_prompt"] = "auto"
    return client.get("/oauth/authorize", data)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("method", ["post", "auto", "skip"])
def test_native_grant_is_written_inside_a_transaction(
    client, active_user, oauth_application, access_token, method
):
    if method == "skip":
        oauth_application.skip_authorization = True
        oauth_application.save(update_fields=["skip_authorization"])
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    grant_transactions = []

    def record_transaction(sender, **kwargs):
        grant_transactions.append(connection.in_atomic_block)

    pre_save.connect(record_transaction, sender=Grant)
    try:
        response = native_approval(client, oauth_application, method)
    finally:
        pre_save.disconnect(record_transaction, sender=Grant)

    assert response.status_code == 302
    parsed = urlparse(response.url)
    assert parsed.netloc == "forum.example.com" and parsed.path == "/auth/nfv"
    query = parse_qs(parsed.query)
    assert query["state"] == ["test-state"]
    assert Grant.objects.filter(code=query["code"][0], user=active_user).exists()
    assert grant_transactions == [True]


@pytest.mark.django_db(transaction=True)
def test_native_grant_holds_the_user_row_lock_until_written(client, active_user, oauth_application):
    from concurrent.futures import ThreadPoolExecutor

    from django.db import DatabaseError, transaction

    if not connection.features.has_select_for_update_nowait:
        pytest.skip("Requires SELECT FOR UPDATE NOWAIT to verify the User row lock")

    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    lock_available_during_write = []

    def user_lock_available():
        try:
            with transaction.atomic():
                User.objects.select_for_update(nowait=True).get(pk=active_user.pk)
            return True
        except DatabaseError:
            return False
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(user_lock_available).result(timeout=10)

        def check_user_lock(sender, **kwargs):
            lock_available_during_write.append(
                worker.submit(user_lock_available).result(timeout=10)
            )

        pre_save.connect(check_user_lock, sender=Grant)
        try:
            response = native_approval(client, oauth_application, "post")
        finally:
            pre_save.disconnect(check_user_lock, sender=Grant)

        assert worker.submit(user_lock_available).result(timeout=10)

    assert response.status_code == 302
    code = parse_qs(urlparse(response.url).query)["code"][0]
    assert Grant.objects.filter(code=code, user=active_user).exists()
    assert lock_available_during_write == [False]


@pytest.mark.django_db
def test_native_grant_rechecks_the_session_hash_on_the_current_identity(
    client, active_user, oauth_application, monkeypatch
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    original_auth_hash = User.get_session_auth_hash
    authenticated_user = None

    def get_auth_hash(user):
        nonlocal authenticated_user
        if authenticated_user is None:
            authenticated_user = user
        if user is authenticated_user:
            return original_auth_hash(user)
        return "different-current-session-hash"

    monkeypatch.setattr(User, "get_session_auth_hash", get_auth_hash)

    response = native_approval(client, oauth_application, "post")

    assert response.status_code == 302
    assert urlparse(response.url).path == reverse("account_login")
    assert SESSION_KEY not in client.session
    assert not Grant.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_native_consent_decline_preserves_state_without_issuing_grant(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization_data = authorization_request(oauth_application)
    assert client.get("/oauth/authorize", authorization_data).status_code == 200

    response = client.post("/oauth/authorize", authorization_data)

    assert response.status_code == 302
    parsed = urlparse(response.url)
    assert parsed.netloc == "forum.example.com" and parsed.path == "/auth/nfv"
    assert parse_qs(parsed.query) == {"error": ["access_denied"], "state": ["test-state"]}
    assert not Grant.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_pending_identity_cannot_enter_authorization_flow(client, oauth_application):
    user = User.objects.create_user(
        login_phone_number="0912345678", name="Pending OAuth", password="StrongPassword12"
    )
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")
    authorization_data = authorization_request(oauth_application)
    for response in (
        client.get("/oauth/authorize", authorization_data),
        client.post("/oauth/authorize", {**authorization_data, "allow": "Authorize"}),
    ):
        assert response.status_code == 302
        assert response.url == "/account/complete"
    assert not Grant.objects.filter(user=user).exists()
    token = AccessToken.objects.create(
        user=user,
        application=oauth_application,
        token="pending-test-token",
        expires=timezone.now() + timedelta(hours=1),
        scope="profile email content:manage",
    )
    assert client.get("/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {token.token}").status_code == 403
    assert (
        client.get("/api/v1/admin/session", HTTP_AUTHORIZATION=f"Bearer {token.token}").status_code
        == 401
    )


@pytest.mark.django_db
@pytest.mark.parametrize("email_state", ["missing", "unverified", "verified"])
def test_authorization_code_pkce_flow_issues_token_usable_by_me_api(
    client, active_user, oauth_application, script_mount, email_state
):
    client.defaults["SCRIPT_NAME"] = script_mount
    active_user.login_phone_number = "0912345678"
    if email_state == "missing":
        active_user.emailaddress_set.all().delete()
        active_user.email = None
    elif email_state == "unverified":
        active_user.emailaddress_set.update(verified=False)
    active_user.save(update_fields=["email", "login_phone_number"])
    MemberProfile.objects.create(
        user=active_user, phone_number="+84 987 654 321", date_of_birth="1990-06-15"
    )

    authorization_data = authorization_request(oauth_application)
    next_url = script_mount + "/oauth/authorize?" + urlencode(authorization_data)
    signed_in = client.post(
        "/accounts/login/",
        {"login": "0912345678", "password": "StrongPassword12", "next": next_url},
    )
    assert signed_in.status_code == 302 and signed_in.url == next_url
    code = authorize(client, authorization_data)

    if email_state == "verified" and not script_mount:
        for invalid in (
            {"code_verifier": "b" * 64},
            {"redirect_uri": "https://forum.example.com/other"},
            {"client_secret": "wrong-test-secret"},
        ):
            denied = exchange(client, oauth_application, code, **invalid)
            assert denied.status_code == (401 if "client_secret" in invalid else 400)
    token_response = exchange(client, oauth_application, code)
    assert token_response.status_code == 200
    tokens = token_response.json()
    assert set(tokens["scope"].split()) == {"profile", "email"}

    initial_profile = client.get(
        "/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}"
    )
    assert initial_profile.status_code == 200
    renewed = refresh(client, oauth_application, tokens["refresh_token"])
    assert renewed.status_code == 200
    updated_tokens = renewed.json()
    assert updated_tokens["refresh_token"] != tokens["refresh_token"]
    assert updated_tokens["access_token"] != tokens["access_token"]

    profile = client.get(
        "/api/v1/me", HTTP_AUTHORIZATION=f"Bearer {updated_tokens['access_token']}"
    )
    assert profile.status_code == 200
    assert profile.json()["data"] == {
        "sub": str(active_user.pk),
        "name": active_user.name,
        "email": active_user.email,
        "email_verified": email_state == "verified",
        "login_phone_number": "+84912345678",
        "locale": "vi",
        "status": "active",
        "roles": [],
        "phone_number": "+84912345678",
        "date_of_birth": "1990-06-15",
    }
    assert initial_profile.json() == profile.json()
    assert refresh(client, oauth_application, tokens["refresh_token"]).status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize(
    "state", [AccountStatus.SUSPENDED, AccountStatus.DELETION_REQUESTED, AccountStatus.ANONYMIZED]
)
def test_locked_identity_cannot_use_oauth_credentials_issued_while_active(
    client, active_user, oauth_application, state
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization_data = authorization_request(oauth_application)
    token_response = exchange(client, oauth_application, authorize(client, authorization_data))
    assert token_response.status_code == 200
    tokens = token_response.json()
    unused_code = authorize(client, authorization_data)

    transition_account(active_user, state)
    before = set(Grant.objects.filter(user=active_user).values_list("pk", flat=True))
    response = client.post("/oauth/authorize", {**authorization_data, "allow": "Authorize"})
    assert response.status_code in {302, 403}
    assert (
        response.get("Location", "").startswith("/accounts/login/") or response.status_code == 403
    )
    assert set(Grant.objects.filter(user=active_user).values_list("pk", flat=True)) == before
    for denied in (
        exchange(client, oauth_application, unused_code),
        refresh(client, oauth_application, tokens["refresh_token"]),
    ):
        assert denied.status_code == 400 and denied.json()["error"] == "invalid_grant"
    bearer = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
    me = client.get("/api/v1/me", **bearer)
    assert me.status_code == 403 and me.json() == {"code": "account_inactive"}
    assert client.get("/api/v1/admin/session", **bearer).status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["post", "auto", "skip"])
def test_admin_reset_revokes_native_oauth_credentials_for_phone_only_account(
    client, active_user, access_token, oauth_application, method
):
    from django.contrib.auth.models import Group

    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    member = User.objects.create_user(
        login_phone_number="0912345678",
        name="Phone-only OAuth",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    client.force_login(member, backend="identity.backends.CoreAuthenticationBackend")
    authorization_data = authorization_request(oauth_application)
    issued = exchange(client, oauth_application, authorize(client, authorization_data))
    assert issued.status_code == 200
    tokens = issued.json()
    if method == "skip":
        oauth_application.skip_authorization = True
        oauth_application.save(update_fields=["skip_authorization"])
    initial_approval = native_approval(client, oauth_application, method)
    assert initial_approval.status_code == 302
    unused_code = parse_qs(urlparse(initial_approval.url).query)["code"][0]
    member_bearer = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
    assert client.get("/api/v1/me", **member_bearer).status_code == 200

    updated_password = "Reset-member-password-58!"
    reset = client.post(
        f"/api/v1/admin/users/{member.pk}/reset-password",
        {"password1": updated_password, "password2": updated_password},
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {access_token.token}",
    )
    assert reset.status_code == 200
    assert reset.json() == {"data": {"id": str(member.pk), "status": "active"}}
    later_approval = native_approval(client, oauth_application, method)
    assert later_approval.status_code == 302
    assert urlparse(later_approval.url).path == reverse("account_login")
    assert not Grant.objects.filter(user=member).exists()
    me = client.get("/api/v1/me", **member_bearer)
    assert me.status_code == 401 and me.json() == {"code": "invalid_token"}
    for denied in (
        exchange(client, oauth_application, unused_code),
        refresh(client, oauth_application, tokens["refresh_token"]),
    ):
        assert denied.status_code == 400 and denied.json()["error"] == "invalid_grant"
    assert (
        client.get(
            "/api/v1/admin/session", HTTP_AUTHORIZATION=f"Bearer {access_token.token}"
        ).status_code
        == 200
    )
