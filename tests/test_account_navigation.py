from urllib.parse import quote

import pytest
from django.test import override_settings

from identity.navigation import (
    LOGIN_NEXT_SESSION_KEY,
    PUBLIC_RETURN_TO_SESSION_KEY,
    public_return_from_oauth_next,
    safe_local_next,
    safe_oauth_signup_next,
    safe_public_return_to,
)

PUBLIC_ORIGINS = (
    "https://naturalfarmingvietnam.com",
    "https://preview.naturalfarmingvietnam.com",
)


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.parametrize(
    "value",
    [
        "https://attacker.example/account",
        "https://preview.naturalfarmingvietnam.com.attacker.example/account",
        "javascript:alert(1)",
        "//preview.naturalfarmingvietnam.com/account",
        "https://user:password@preview.naturalfarmingvietnam.com/account",
        "https://preview.naturalfarmingvietnam.com/account\r\nLocation: https://attacker.example",
    ],
)
def test_public_return_url_rejects_untrusted_targets(value):
    assert safe_public_return_to(value) is None


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
def test_public_return_url_accepts_an_allowlisted_path_query_and_fragment():
    assert (
        safe_public_return_to("https://preview.naturalfarmingvietnam.com/plants/vi?tab=inputs#imo")
        == "https://preview.naturalfarmingvietnam.com/plants/vi?tab=inputs#imo"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://attacker.example",
        "//attacker.example",
        "login",
        "/login\r\nLocation: https://attacker.example",
    ],
)
def test_login_next_rejects_external_or_malformed_targets(value):
    assert safe_local_next(value) is None


def test_signup_oauth_next_accepts_only_local_signup_authorization():
    valid = (
        "/oauth/authorize?client_id=public&screen_hint=signup&state=state&code_challenge=challenge"
    )

    assert safe_oauth_signup_next(valid) == valid
    assert safe_oauth_signup_next("/oauth/authorize?client_id=public") is None
    external = "https://attacker.example/oauth/authorize?screen_hint=signup"
    assert safe_oauth_signup_next(external) is None


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
def test_oauth_next_selects_the_matching_public_origin():
    next_url = (
        "/oauth/authorize?response_type=code"
        "&redirect_uri=https%3A%2F%2Fpreview.naturalfarmingvietnam.com"
        "%2Fapi%2Fauth%2Fcallback"
    )

    assert public_return_from_oauth_next(next_url) == ("https://preview.naturalfarmingvietnam.com/")


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
def test_oauth_next_rejects_an_untrusted_callback_origin():
    next_url = (
        "/oauth/authorize?redirect_uri=https%3A%2F%2Fattacker.example%2Fapi%2Fauth%2Fcallback"
    )

    assert public_return_from_oauth_next(next_url) is None


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_account_remembers_validated_public_return_url(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(
        "/account",
        {"return_to": ("https://preview.naturalfarmingvietnam.com/plants/vi?tab=inputs")},
    )

    assert response.status_code == 200
    assert (
        client.session[PUBLIC_RETURN_TO_SESSION_KEY]
        == "https://preview.naturalfarmingvietnam.com/plants/vi?tab=inputs"
    )
    assert response.context["public_site_url"] == (
        "https://preview.naturalfarmingvietnam.com/plants/vi?tab=inputs"
    )
    assert (
        'href="https://preview.naturalfarmingvietnam.com/plants/vi?tab=inputs"'
        in response.content.decode()
    )


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_account_ignores_untrusted_return_url(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(
        "/account",
        {"return_to": "https://attacker.example/steal-session"},
    )

    assert PUBLIC_RETURN_TO_SESSION_KEY not in client.session
    assert response.context["public_site_url"] == "https://naturalfarmingvietnam.com/"
    assert 'href="https://naturalfarmingvietnam.com/"' in response.content.decode()


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_password_reset_and_login_links_preserve_safe_oauth_continuation(client):
    next_url = (
        "/oauth/authorize?response_type=code&client_id=public"
        "&redirect_uri=https%3A%2F%2Fpreview.naturalfarmingvietnam.com"
        "%2Fapi%2Fauth%2Fcallback"
    )

    response = client.get(
        "/accounts/login/",
        {
            "next": next_url,
            "return_to": "https://preview.naturalfarmingvietnam.com/vi",
        },
    )

    assert client.session[LOGIN_NEXT_SESSION_KEY] == next_url
    assert response.context["core_login_url"] == (
        f"/accounts/login/?next={quote(next_url, safe='')}"
    )
    assert response.context["password_reset_url"] == "/accounts/password/reset/"
    assert response.context["public_site_url"] == ("https://preview.naturalfarmingvietnam.com/vi")


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_oauth_login_uses_the_callback_public_origin(client):
    next_url = (
        "/oauth/authorize?response_type=code"
        "&redirect_uri=https%3A%2F%2Fpreview.naturalfarmingvietnam.com"
        "%2Fapi%2Fauth%2Fcallback"
    )

    response = client.get("/accounts/login/", {"next": next_url})

    assert response.context["public_site_url"] == ("https://preview.naturalfarmingvietnam.com/")
    assert (
        client.session[PUBLIC_RETURN_TO_SESSION_KEY] == "https://preview.naturalfarmingvietnam.com/"
    )


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_password_reset_does_not_resume_a_stale_oauth_request(client):
    next_url = (
        "/oauth/authorize?response_type=code&state=short-lived"
        "&redirect_uri=https%3A%2F%2Fpreview.naturalfarmingvietnam.com"
        "%2Fapi%2Fauth%2Fcallback"
    )
    client.get("/accounts/login/", {"next": next_url})

    response = client.post(
        "/accounts/password/reset/",
        {"email": "unknown@example.com"},
    )

    assert response.status_code == 302
    assert response.url == "/accounts/password/reset/done/"


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_authenticated_password_reset_returns_to_account(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/accounts/password/reset/")
    content = response.content.decode()

    assert 'href="/account"' in content
    assert "Quay lại tài khoản" in content


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_password_change_returns_to_account(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/accounts/password/change/",
        {
            "oldpassword": "StrongPassword12",
            "password1": "NewStrongPassword34",
            "password2": "NewStrongPassword34",
        },
    )

    assert response.status_code == 302
    assert response.url == "/account"
    active_user.refresh_from_db()
    assert active_user.check_password("NewStrongPassword34")


@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_password_reset_completion_starts_a_fresh_public_login(client):
    client.get(
        "/accounts/password/reset/",
        {"return_to": "https://preview.naturalfarmingvietnam.com/plants/vi"},
    )

    response = client.get("/accounts/password/reset/key/done/")

    assert response.context["public_login_url"] == (
        "https://preview.naturalfarmingvietnam.com/api/auth/login?locale=vi&returnTo=%2Fplants%2Fvi"
    )
    content = response.content.decode()
    assert response.context["public_login_url"].replace("&", "&amp;") in content
    assert "https://attacker.example" not in content
