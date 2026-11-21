import re
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlencode, urlparse

import pytest
from allauth.account.forms import default_token_generator
from django.contrib.auth import SESSION_KEY
from django.core.cache import cache
from django.test import Client, override_settings

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
    assert parse_qs(urlparse(response.context["password_reset_url"]).query) == {
        "return_to": ["https://preview.naturalfarmingvietnam.com/vi"]
    }
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
@pytest.mark.parametrize(
    ("language", "success_message"),
    [("vi", "Đổi mật khẩu thành công."), ("en", "Password successfully changed.")],
)
def test_password_change_returns_to_account(
    client, active_user, settings, language, success_message
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/accounts/password/change/",
        {
            "oldpassword": "StrongPassword12",
            "password1": "NewStrongPassword34",
            "password2": "NewStrongPassword34",
        },
        follow=True,
    )

    assert response.status_code == 200
    assert response.redirect_chain == [("/account", 302)]
    text = response.content.decode()
    toast = text.split('<div class="auth-toast-stack">', 1)[1]
    assert toast.count('role="status"') == toast.count('x-init="show()"') == 1
    assert success_message in toast and text.count(success_message) == 1
    assert success_message not in client.get("/account").content.decode()
    active_user.refresh_from_db()
    assert active_user.check_password("NewStrongPassword34")


@pytest.fixture
def isolated_reset_rate_limits():
    cache.clear()
    yield
    cache.clear()


@pytest.mark.usefixtures("isolated_reset_rate_limits")
@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("origin", "new_browser", "login_url"),
    [
        (
            "application",
            False,
            "https://preview.naturalfarmingvietnam.com/api/auth/login?locale=vi&returnTo=%2Fplants%2Fvi",
        ),
        (
            "oauth",
            False,
            "https://preview.naturalfarmingvietnam.com/api/auth/login?locale=vi&returnTo=%2F",
        ),
        ("application", True, "/accounts/login/"),
        ("auth", False, "/accounts/login/"),
        ("signed-in", False, "/accounts/login/"),
        ("untrusted", False, "/accounts/login/"),
    ],
)
def test_password_reset_completion_uses_only_its_own_login_origin(
    client, active_user, mailoutbox, origin, new_browser, login_url
):
    client.get(
        "/accounts/login/",
        {
            "next": "/oauth/authorize?state=stale",
            "return_to": "https://naturalfarmingvietnam.com/old",
        },
    )
    request_url = "/accounts/password/reset/"
    if origin == "application":
        login = client.get(
            "/accounts/login/",
            {"return_to": "https://preview.naturalfarmingvietnam.com/plants/vi"},
        )
        request_url = login.context["password_reset_url"]
    elif origin == "oauth":
        next_url = "/oauth/authorize?" + urlencode(
            {
                "state": "this-attempt",
                "redirect_uri": "https://preview.naturalfarmingvietnam.com/api/auth/callback",
            }
        )
        login = client.get("/accounts/login/", {"next": next_url})
        request_url = login.context["password_reset_url"]
    elif origin == "untrusted":
        request_url += "?" + urlencode(
            {
                "return_to": "https://attacker.example/account",
                "next": "/oauth/authorize?state=stale",
            }
        )
    elif origin == "signed-in":
        client.force_login(active_user)
    client.get(request_url)
    requested = client.post(request_url, {"email": active_user.email})
    assert requested.url == "/accounts/password/reset/done/"
    link = re.search(r"https?://[^\s]+/accounts/password/reset/key/[^\s]+", mailoutbox[0].body)
    assert link is not None
    browser = Client() if new_browser else client
    opened = browser.get(link[0])
    assert opened.status_code == 302
    response = browser.post(
        opened.url,
        {"password1": "NewStrongPassword34", "password2": "NewStrongPassword34"},
        follow=True,
    )

    assert response.status_code == 200
    assert len(response.redirect_chain) == 1
    done_url, status_code = response.redirect_chain[0]
    assert status_code == 302
    assert urlparse(done_url).path == "/accounts/password/reset/key/done/"
    if login_url.startswith("https://"):
        assert set(parse_qs(urlparse(done_url).query)) == {"flow"}
    else:
        assert not urlparse(done_url).query
    content = response.content.decode()
    escaped_url = login_url.replace("&", "&amp;")
    login_link = f'class="auth-button auth-button-primary" href="{escaped_url}"'
    assert login_link in content
    reloaded = browser.get(done_url)
    assert reloaded.status_code == 200
    assert login_link in reloaded.content.decode()
    if login_url.startswith("https://"):
        core_login = 'class="auth-button auth-button-primary" href="/accounts/login/"'
        assert core_login in Client().get(done_url).content.decode()
        invalid_flow = urlparse(done_url).path + "?flow=unknown"
        assert core_login in browser.get(invalid_flow).content.decode()
    assert "https://attacker.example" not in content
    assert SESSION_KEY not in browser.session
    assert LOGIN_NEXT_SESSION_KEY not in browser.session
    assert PUBLIC_RETURN_TO_SESSION_KEY not in browser.session


@pytest.mark.usefixtures("isolated_reset_rate_limits")
@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_later_reset_request_does_not_replace_the_origin_of_an_earlier_email(
    client, active_user, mailoutbox
):
    for path in ["/first", "/second"]:
        cache.clear()
        return_to = "https://preview.naturalfarmingvietnam.com" + path
        url = "/accounts/password/reset/?" + urlencode({"return_to": return_to})
        assert client.post(url, {"email": active_user.email}).status_code == 302

    pattern = r"https?://[^\s]+/accounts/password/reset/key/[^\s]+"
    first, second = [re.search(pattern, message.body)[0] for message in mailoutbox]
    assert first != second
    opened = client.get(first)
    response = client.post(
        opened.url,
        {"password1": "NewStrongPassword34", "password2": "NewStrongPassword34"},
        follow=True,
    )
    assert response.context["password_reset_login_url"] == (
        "https://preview.naturalfarmingvietnam.com/api/auth/login?locale=vi&returnTo=%2Ffirst"
    )


@pytest.mark.usefixtures("isolated_reset_rate_limits")
@override_settings(PUBLIC_SITE_ORIGINS=PUBLIC_ORIGINS)
@pytest.mark.django_db
def test_two_open_password_reset_forms_keep_their_own_flow(
    client, active_user, mailoutbox, monkeypatch
):
    now = default_token_generator._now()
    monkeypatch.setattr(default_token_generator, "_now", lambda: now)
    pattern = r"https?://[^\s]+/accounts/password/reset/key/[^\s]+"
    # Send the emails a second apart so each link contains a different token.
    links = []
    for path in ["/first", "/second"]:
        cache.clear()
        return_to = "https://preview.naturalfarmingvietnam.com" + path
        url = "/accounts/password/reset/?" + urlencode({"return_to": return_to})
        assert client.post(url, {"email": active_user.email}).status_code == 302
        match = re.search(pattern, mailoutbox[-1].body)
        assert match is not None
        links.append(match[0])
        now += timedelta(seconds=1)
    assert urlparse(links[0]).path != urlparse(links[1]).path

    forms = []
    for link in links:
        opened = client.get(link)
        assert opened.status_code == 302
        assert urlparse(opened.url).path.endswith("-set-password/")
        form = client.get(opened.url)
        assert form.status_code == 200 and b'name="password1"' in form.content
        forms.append(opened.url)
    assert forms[0] != forms[1]

    password = "NewStrongPassword34"
    completed = client.post(forms[0], {"password1": password, "password2": password}, follow=True)
    assert completed.status_code == 200
    assert completed.context["password_reset_login_url"] == (
        "https://preview.naturalfarmingvietnam.com/api/auth/login?locale=vi&returnTo=%2Ffirst"
    )
    assert (
        'href="https://preview.naturalfarmingvietnam.com/api/auth/login'
        '?locale=vi&amp;returnTo=%2Ffirst"' in completed.content.decode()
    )

    another_password = "AnotherStrongPassword56"
    stale = client.post(forms[1], {"password1": another_password, "password2": another_password})
    assert stale.status_code == 200 and stale.context["token_fail"]
    active_user.refresh_from_db()
    assert active_user.check_password(password)


@pytest.mark.parametrize("value", ["/\\attacker.example", "/account\\..\\login"])
def test_local_next_rejects_browser_backslash_normalization(value):
    assert safe_local_next(value) is None


@pytest.mark.django_db
def test_signup_login_complete_verification_resumes_original_oauth_without_loop(
    client, oauth_application, mailoutbox
):
    from urllib.parse import urlencode

    from allauth.account.models import EmailConfirmationHMAC
    from django.core.cache import cache

    from identity.models import User

    cache.clear()
    query = {
        "client_id": oauth_application.client_id,
        "response_type": "code",
        "redirect_uri": "https://forum.example.com/auth/nfv",
        "scope": "profile email",
        "state": "signup-to-consent",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "screen_hint": "signup",
        "ui_locales": "en",
    }
    continuation = "/oauth/authorize?" + urlencode(query)
    signup = client.post(
        "/register",
        {
            "phone": "0901234567",
            "name": "New Member",
            "email": "new-flow@example.com",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
            "next": continuation,
        },
    )
    assert signup.status_code == 302 and "_auth_user_id" not in client.session
    signed_in = client.post(
        signup.url, {"login": "0901234567", "password": "StrongPassword12", "next": continuation}
    )
    assert signed_in.url == "/account/complete"
    assert client.get(signed_in.url).status_code == 200
    assert client.session[LOGIN_NEXT_SESSION_KEY] == continuation
    user = User.objects.get(email="new-flow@example.com")
    address = user.emailaddress_set.get()
    confirmed = client.post(f"/accounts/confirm-email/{EmailConfirmationHMAC(address).key}/")
    assert confirmed.status_code == 302
    resumed = parse_qs(urlparse(confirmed.url).query)
    assert resumed["state"] == ["signup-to-consent"] and resumed["code_challenge"] == ["a" * 43]
    assert "screen_hint" not in resumed and "referral_code" not in resumed
    consent = client.get(confirmed.url)
    assert consent.status_code == 200
    assert "oauth2_provider/authorize.html" in [template.name for template in consent.templates]
    assert LOGIN_NEXT_SESSION_KEY not in client.session


@pytest.mark.parametrize(
    "value", ["/\t///attacker.example", "/\t//attacker.example", "/\x00/account", "/account\x1f"]
)
def test_local_next_rejects_control_characters_before_url_normalization(value):
    assert safe_local_next(value) is None


@pytest.fixture(params=["", "/core"], ids=["root", "script-prefix"])
def script_mount(request):
    from django.urls import get_script_prefix, set_script_prefix

    previous = get_script_prefix()
    # WSGI sets the URL resolver prefix from SCRIPT_NAME; the test client does not.
    set_script_prefix(request.param + "/")
    try:
        yield request.param
    finally:
        set_script_prefix(previous)


@pytest.mark.django_db
@pytest.mark.parametrize("overwrite_next", [False, True], ids=["original-next", "overwritten-next"])
def test_verification_resumes_oauth_at_script_mount(
    client, oauth_application, script_mount, overwrite_next
):
    from urllib.parse import urlencode

    from allauth.account.models import EmailAddress, EmailConfirmationHMAC

    from identity.models import User

    user = User.objects.create_user(
        email="mounted-flow@example.com",
        password="StrongPassword12",
        name="Mounted signup",
        login_phone_number="0901234567",
    )
    address = EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")
    oauth_query = {
        "response_type": "code",
        "client_id": oauth_application.client_id,
        "redirect_uri": "https://forum.example.com/auth/nfv",
        "scope": "profile email",
        "state": "keep+state/&=value",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "screen_hint": "signup",
        "referral_code": "REF12345",
        "ui_locales": "en",
    }
    continuation_path = "/oauth/authorize?" + urlencode(oauth_query)
    restricted = client.get(continuation_path, SCRIPT_NAME=script_mount)
    assert restricted.status_code == 302
    assert restricted.url == script_mount + "/account/complete"
    assert client.session[LOGIN_NEXT_SESSION_KEY] == script_mount + continuation_path

    if overwrite_next:
        other_oauth = (
            script_mount
            + "/oauth/authorize?"
            + urlencode(
                {
                    **oauth_query,
                    "state": "different-flow",
                    "redirect_uri": "https://forum.example.com/other-callback",
                    "scope": "profile",
                    "code_challenge": "b" * 43,
                }
            )
        )
        completion = client.get(
            "/account/complete", {"next": other_oauth}, SCRIPT_NAME=script_mount
        )
        assert completion.status_code == 200
        assert client.session[LOGIN_NEXT_SESSION_KEY] == other_oauth

    confirmed = client.post(
        f"/accounts/confirm-email/{EmailConfirmationHMAC(address).key}/",
        SCRIPT_NAME=script_mount,
    )
    assert confirmed.status_code == 302
    destination = urlparse(confirmed.url)
    assert destination.path == script_mount + "/oauth/authorize"
    assert parse_qs(destination.query) == {
        key: [value]
        for key, value in oauth_query.items()
        if key not in {"screen_hint", "referral_code"}
    }
    assert LOGIN_NEXT_SESSION_KEY not in client.session
    user.refresh_from_db()
    assert user.status == "active" and user.email_verified
    consent = client.get("/oauth/authorize?" + destination.query, SCRIPT_NAME=script_mount)
    assert consent.status_code == 200
    assert "oauth2_provider/authorize.html" in [template.name for template in consent.templates]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target", ["/login", "/register", "/accounts/login", "/accounts/signup", "/account/complete"]
)
@pytest.mark.parametrize("trailing_slash", ["", "/"])
def test_account_destination_excludes_auth_loops_at_script_mount(
    rf, active_user, script_mount, target, trailing_slash
):
    from identity.navigation import account_destination

    request = rf.get("/")
    request.user = active_user
    request.session = {
        LOGIN_NEXT_SESSION_KEY: script_mount + target + trailing_slash + "?next=/account"
    }
    assert account_destination(request) == script_mount + "/account"
    assert LOGIN_NEXT_SESSION_KEY not in request.session


@pytest.mark.django_db
@pytest.mark.parametrize(
    "target", ["https://attacker.example/core/account", "//attacker.example/account", "/\\evil"]
)
def test_account_destination_rejects_unsafe_next_at_script_mount(
    rf, active_user, script_mount, target
):
    from identity.navigation import account_destination

    request = rf.get("/")
    request.user = active_user
    request.session = {LOGIN_NEXT_SESSION_KEY: target}
    assert account_destination(request) == script_mount + "/account"
    assert LOGIN_NEXT_SESSION_KEY not in request.session
