import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account import signals
from allauth.core import context
from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import Group
from django.contrib.auth.password_validation import password_validators_help_text_html
from django.core.exceptions import PermissionDenied
from django.test import Client
from django.urls import get_script_prefix, reverse, set_script_prefix
from django.utils import timezone, translation

from identity.admin_services import change_account_state, reset_account_password
from identity.forms import CoreChangePasswordForm, CoreSetPasswordForm
from identity.models import AccountStatus, User
from identity.services import transition_account
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY
from members.models import MemberProfile
from members.services import update_member_profile


class Controls(HTMLParser):
    """Inspect rendered attributes without replacing Django's widget rendering."""

    def __init__(self, response):
        super().__init__()
        self.elements = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def get(self, tag, **expected):
        return next(
            attrs
            for name, attrs in self.elements
            if name == tag
            and all(key in attrs and attrs[key] == value for key, value in expected.items())
        )

    def field(self, name, input_type, *, required=True):
        attrs = self.get("input", name=name, type=input_type)
        assert ("required" in attrs) is required
        assert attrs["id"]
        self.get("label", **{"for": attrs["id"]})
        return attrs


def assert_close_in_topbar(html):
    before_main, _separator, main = html.partition('<main class="auth-stage">')
    topbar = re.search(r'<div class="auth-topbar">(.*?)</div>', before_main, re.S)
    assert topbar
    assert topbar[1].index("auth-close") < topbar[1].index("language-switcher")
    assert "auth-close" not in main


@pytest.mark.django_db
@pytest.mark.parametrize("prefix", ["", "/core"])
@pytest.mark.parametrize(("initial_locale", "locale"), [("vi", "en"), ("en", "vi")])
def test_invited_signup_keeps_readonly_referral_after_language_switch(
    request, active_user, oauth_application, prefix, initial_locale, locale
):
    previous_prefix = get_script_prefix()
    request.addfinalizer(lambda: set_script_prefix(previous_prefix))
    set_script_prefix(prefix + "/")
    client = Client(enforce_csrf_checks=True, SCRIPT_NAME=prefix)
    code = active_user.referral_account.code
    invitation = client.get(
        "/oauth/authorize",
        {
            "client_id": oauth_application.client_id,
            "redirect_uri": oauth_application.redirect_uris,
            "response_type": "code",
            "scope": "profile email",
            "state": "invited-signup",
            "screen_hint": "signup",
            "referral_code": code,
            "ui_locales": initial_locale,
        },
    )
    assert invitation.status_code == 302
    signup = client.get(invitation.url.removeprefix(prefix))
    csrf = Controls(signup).get("input", name="csrfmiddlewaretoken")["value"]
    oauth_next = parse_qs(urlsplit(invitation.url).query)["next"][0]
    form_error = client.post(
        reverse("account_signup").removeprefix(prefix),
        {"next": oauth_next, "name": "Invited member", "csrfmiddlewaretoken": csrf},
    )
    assert form_error.status_code == 200
    assert "phone" in form_error.context["form"].errors
    controls = Controls(form_error)
    assert "readonly" in controls.get("input", name="referral_code", value=code)
    language_action = controls.get("form", **{"class": "language-switcher"})["action"]
    assert language_action == f"{prefix}/language/"
    language_next = controls.get("input", name="next", type="hidden")["value"]
    switched = client.post(
        language_action.removeprefix(prefix),
        {"language": locale, "next": language_next, "csrfmiddlewaretoken": csrf},
    )
    assert switched.status_code == 302
    assert urlsplit(switched.url).path == reverse("account_signup")
    assert parse_qs(urlsplit(switched.url).query)["next"] == [oauth_next]
    localized = client.get(switched.url.removeprefix(prefix))
    assert localized.status_code == 200
    Controls(localized).get("html", lang=locale)
    assert "readonly" in Controls(localized).get("input", name="referral_code", value=code)
    assert client.session[SIGNUP_REFERRAL_SESSION_KEY] == code

    ordinary = client.get(reverse("account_signup").removeprefix(prefix))
    assert ordinary.status_code == 200
    assert b'name="referral_code"' not in ordinary.content
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "fields"),
    [
        (
            "/login",
            [
                ("login", "text", True),
                ("password", "password", True),
                ("remember", "checkbox", False),
            ],
        ),
        (
            "/register",
            [
                ("phone", "tel", True),
                ("email", "email", False),
                ("password1", "password", True),
                ("password2", "password", True),
            ],
        ),
        ("/accounts/password/reset/", [("email", "email", True)]),
        (
            "/accounts/password/change/",
            [
                ("oldpassword", "password", True),
                ("password1", "password", True),
                ("password2", "password", True),
            ],
        ),
    ],
)
def test_auth_templates_keep_native_input_types_and_labels(client, active_user, path, fields):
    if path.endswith("/change/"):
        client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    response = client.get(path)
    assert response.status_code == 200
    controls = Controls(response)
    controls.get("link", rel="stylesheet", href="/static/css/account-ui.css")
    controls.get("input", name="csrfmiddlewaretoken", type="hidden")
    for name, input_type, required in fields:
        attrs = controls.field(name, input_type, required=required)
        if input_type == "password":
            assert "value" not in attrs
    if path == "/login":
        controls.get("input", name="login", autocomplete="username")


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["vi", "en"])
def test_password_change_render_and_field_errors_in_both_languages(active_user, language):
    client = Client(enforce_csrf_checks=True)
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    signup_response = client.get(reverse("account_signup"))
    assert signup_response.status_code == 200
    signup = Controls(signup_response)
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    path = reverse("account_change_password")
    initial = client.get(path)
    csrf = Controls(initial).get("input", name="csrfmiddlewaretoken")["value"]
    data = {"oldpassword": "WrongPassword", "password1": "TooShort7", "password2": "TooShort7"}
    assert client.post(path, data).status_code == 403
    invalid = client.post(path, {**data, "csrfmiddlewaretoken": csrf})
    assert set(invalid.context["form"].errors) == {"oldpassword", "password1"}

    if language == "vi":
        title = "Đổi mật khẩu"
        labels = ("Mật khẩu hiện tại", "Mật khẩu mới", "Nhập lại mật khẩu mới")
        hints = ("Mật khẩu", "Nhập lại mật khẩu")
        close_label, forgot_label = "Quay lại tài khoản", "Quên mật khẩu?"
        current_password_error = "Vui lòng nhập mật khẩu hiện tại."
    else:
        title = "Change password"
        labels = ("Current Password", "New Password", "New Password (again)")
        hints = ("Password", "Re-enter your password")
        close_label, forgot_label = "Back to account", "Forgot password?"
        current_password_error = "Please type your current password."

    assert signup.field("password1", "password")["placeholder"] == hints[0]
    assert signup.field("password2", "password")["placeholder"] == hints[1]
    assert current_password_error in invalid.content.decode()
    with translation.override(language):
        password_help = str(password_validators_help_text_html())
    assert password_help.count("<li>") == 4
    for response in (signup_response, initial, invalid):
        help_text = re.search(
            r'<div class="auth-help" id="id_password1_helptext">(.*?)</div>',
            response.content.decode(),
        )
        assert help_text and help_text[1] == password_help
        password = Controls(response).get("input", name="password1")
        assert "id_password1_helptext" in password["aria-describedby"].split()

    for response in (initial, invalid):
        assert response.status_code == 200
        html = response.content.decode()
        assert f'<html lang="{language}">' in html
        assert f"<h1>{title}</h1>" in html
        assert 'class="auth-eyebrow"' not in html
        assert_close_in_topbar(html)
        assert "Forgot your password?" not in html and "oldpassword_helptext" not in html
        controls = Controls(response)
        close = controls.get("a", href=reverse("account"), **{"aria-label": close_label})
        assert "auth-close" in close["class"].split()
        forgot = controls.get("a", href=reverse("account_reset_password"))
        assert "auth-button-outline" in forgot["class"].split()
        assert forgot_label in html
        assert html.count("auth-button-primary") == 1
        for name, label in zip(("oldpassword", "password1", "password2"), labels, strict=True):
            field = controls.field(name, "password")
            autocomplete = "current-password" if name == "oldpassword" else "new-password"
            assert field["autocomplete"] == autocomplete
            label_html = re.search(
                rf'<label[^>]*for="{field["id"]}"[^>]*>(.*?)</label>', html, re.S
            )
            assert label_html[1].strip() == f'{label} <span aria-hidden="true">*</span>'
        for name in ("password1", "password2"):
            assert (
                controls.get("input", name=name)["placeholder"]
                == signup.get("input", name=name)["placeholder"]
            )

    controls = Controls(invalid)
    for name in ("oldpassword", "password1"):
        field = controls.get("input", name=name)
        error_id = f"{field['id']}_error"
        assert error_id in field["aria-describedby"].split()
        controls.get("ul", id=error_id)
    assert "auth-toast-stack" not in invalid.content.decode()
    active_user.refresh_from_db()
    assert active_user.check_password("StrongPassword12")


@pytest.mark.django_db
def test_password_change_validation_keeps_legacy_hash_when_form_is_invalid(
    client, active_user, settings
):
    settings.PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
        "django.contrib.auth.hashers.MD5PasswordHasher",
    ]
    legacy_hash = make_password("StrongPassword12", hasher="md5")
    active_user.password = legacy_hash
    active_user.save(update_fields=["password"])
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        reverse("account_change_password"),
        {
            "oldpassword": "StrongPassword12",
            "password1": "NewStrongPassword34",
            "password2": "DoesNotMatch56",
        },
    )

    assert response.status_code == 200
    assert set(response.context["form"].errors) == {"password2"}
    active_user.refresh_from_db()
    assert active_user.password == legacy_hash

    changed = client.post(
        reverse("account_change_password"),
        {
            "oldpassword": "StrongPassword12",
            "password1": "NewStrongPassword34",
            "password2": "NewStrongPassword34",
        },
    )
    assert changed.url == reverse("account")
    assert client.get(changed.url).status_code == 200
    active_user.refresh_from_db()
    assert active_user.check_password("NewStrongPassword34")


@pytest.mark.django_db
@pytest.mark.parametrize("setting_first_password", [False, True])
@pytest.mark.parametrize("admin_action", ["suspend", "reset"])
def test_signed_in_password_write_checks_account_state_after_form_validation(
    rf, active_user, setting_first_password, admin_action
):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    user = User.objects.create_user(
        login_phone_number="0901234567",
        name="Phone-only member",
        password=None if setting_first_password else "StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    request = rf.post("/accounts/password/")
    request.session = {HASH_SESSION_KEY: user.get_session_auth_hash()}
    form_class = CoreSetPasswordForm if setting_first_password else CoreChangePasswordForm
    data = {"password1": "NewStrongPassword34", "password2": "NewStrongPassword34"}
    if not setting_first_password:
        data["oldpassword"] = "StrongPassword12"
    with context.request_context(request):
        form = form_class(user=user, data=data)
        assert form.is_valid(), form.errors
        if admin_action == "suspend":
            updated = change_account_state(actor=active_user, user_id=user.pk, action="suspend")
        else:
            updated = reset_account_password(
                actor=active_user, user_id=user.pk, password="AdminReplacement56"
            )
        with pytest.raises(PermissionDenied):
            form.save()

    user.refresh_from_db()
    assert user.status == updated.status
    assert user.is_active == updated.is_active
    assert user.password == updated.password


@pytest.mark.django_db
def test_setting_first_password_keeps_allauth_signal_and_current_session(
    client, active_user, monkeypatch
):
    active_user.set_unusable_password()
    active_user.save(update_fields=["password"])
    signaled_users = []
    original_send = signals.password_set.send

    def track_password_set(sender, **kwargs):
        signaled_users.append(kwargs["user"].pk)
        return original_send(sender, **kwargs)

    monkeypatch.setattr(signals.password_set, "send", track_password_set)
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    first_password = "NewStrongPassword34"
    data = {"password1": first_password, "password2": first_password}

    response = client.post(reverse("account_set_password"), data)

    assert response.url == reverse("account")
    assert client.get(response.url).status_code == 200
    active_user.refresh_from_db()
    assert active_user.check_password(first_password)
    assert signaled_users == [active_user.pk]

    repeated = client.post(reverse("account_set_password"), data)
    assert repeated.url == reverse("account_change_password")
    assert signaled_users == [active_user.pk]


@pytest.mark.django_db
def test_profile_write_checks_locked_account_after_access_is_lost(active_user):
    previous_name = active_user.name
    transition_account(active_user, AccountStatus.SUSPENDED)

    with pytest.raises(PermissionDenied):
        update_member_profile(user=active_user, data={"name": "New Name"})

    active_user.refresh_from_db()
    assert active_user.name == previous_name
    assert not MemberProfile.objects.filter(user=active_user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize("signed_in", [True, False])
def test_forgot_password_close_returns_to_account_or_sign_in(
    client, active_user, language, signed_in
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    if signed_in:
        client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
        destination = reverse("account")
        label = "Quay lại tài khoản" if language == "vi" else "Back to account"
    else:
        login = client.get(reverse("account_login"), {"next": "/oauth/authorize?state=recovery"})
        destination = login.context["core_login_url"]
        assert "next=" in destination
        label = "Quay lại đăng nhập" if language == "vi" else "Back to sign in"

    response = client.get(reverse("account_reset_password"))
    assert response.status_code == 200
    text = response.content.decode()
    title = "Đặt lại mật khẩu" if language == "vi" else "Reset your password"
    assert f"<h1>{title}</h1>" in text
    assert_close_in_topbar(text)
    close = Controls(response).get("a", href=destination, **{"aria-label": label})
    assert "auth-close" in close["class"].split()
    assert "←" not in text
    assert (
        sum(
            tag == "a" and attrs.get("href") == destination
            for tag, attrs in Controls(response).elements
        )
        == 1
    )


@pytest.mark.django_db
def test_signup_preserves_bound_value_and_references_error_without_echoing_password():
    client = Client(enforce_csrf_checks=True)
    path = reverse("account_signup")
    page = client.get(path)
    csrf = Controls(page).get("input", type="hidden", name="csrfmiddlewaretoken")["value"]
    data = {
        "phone": "0901234567",
        "name": "Example Member",
        "email": "member@example.test",
        "password1": "SyntheticPassword42",
        "password2": "SyntheticMismatch42",
        "next": "/account",
    }
    assert client.post(path, data).status_code == 403
    response = client.post(path, {**data, "csrfmiddlewaretoken": csrf})
    assert response.status_code == 200
    assert not User.objects.exists()
    assert "password2" in response.context["form"].errors
    controls = Controls(response)
    assert controls.field("email", "email", required=False)["value"] == data["email"]
    controls.get("input", type="hidden", name="next", value="/account")
    password = controls.field("password2", "password")
    assert password["aria-invalid"] == "true"
    controls.get("ul", id=f"{password['id']}_error")
    assert f"{password['id']}_error" in password["aria-describedby"].split()
    assert data["password1"].encode() not in response.content
    assert data["password2"].encode() not in response.content


@pytest.mark.django_db
@pytest.mark.parametrize("has_email", [False, True])
def test_completion_uses_bound_email_and_phone_widgets(client, has_email):
    user = User.objects.create_user(
        name="Example",
        email="member@example.test" if has_email else None,
        login_phone_number=None if has_email else "0901234567",
    )
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")
    response = client.get("/account/complete")
    assert response.status_code == 200
    controls = Controls(response)
    if has_email:
        controls.get("form", method="post", action="/account/phone/add")
        controls.field("login_phone_number", "tel")
        controls.field("current_password", "password")
    else:
        controls.get("form", method="post", action="/account/email/add")
        controls.field("email", "email")
        assert not any(attrs.get("name") == "current_password" for _, attrs in controls.elements)


@pytest.mark.django_db
def test_dashboard_only_renders_name_and_birth_date_and_posts_logout_with_csrf(active_user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    response = client.get("/account")
    assert response.status_code == 200
    controls = Controls(response)
    form = controls.get("form", method="post", action="/account")
    save = controls.get("button", type="submit", form=form["id"])
    group = controls.get("div", **{"class": "auth-fields auth-account-fields"})
    change = controls.get("a", href=reverse("account_change_password"))
    assert (
        controls.elements.index(("div", group))
        < controls.elements.index(("form", form))
        < controls.elements.index(("button", save))
        < controls.elements.index(("a", change))
    )
    controls.field("name", "text")
    controls.field("date_of_birth", "date")
    assert not {"phone_number", "province_code", "ward_code", "address"} & {
        attrs.get("name") for tag, attrs in controls.elements if tag in {"input", "select"}
    }
    logout = reverse("account_logout")
    logout_index = next(
        index
        for index, (tag, attrs) in enumerate(controls.elements)
        if tag == "form" and attrs["action"] == logout and attrs["method"] == "post"
    )
    csrf = controls.elements[logout_index + 1][1]
    assert csrf["name"] == "csrfmiddlewaretoken"
    logout_tag, logout_button = controls.elements[logout_index + 2]
    assert logout_tag == "button" and logout_button["type"] == "submit"
    assert set(change["class"].split()) == set(logout_button["class"].split())
    assert client.post(logout).status_code == 403
    assert client.post(logout, {"csrfmiddlewaretoken": csrf["value"]}).status_code == 302
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["vi", "en"])
@pytest.mark.parametrize(
    ("legacy_phone", "login_phone", "email_state"),
    [
        ("0987654321", None, "verified"),
        (None, "0912345678", "missing"),
        ("0987654321", "0912345678", "unverified"),
    ],
)
def test_dashboard_renders_account_identifiers_and_targeted_actions(
    client, active_user, language, legacy_phone, login_phone, email_state
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    active_user.login_phone_number = login_phone
    if email_state == "missing":
        active_user.emailaddress_set.all().delete()
        active_user.email = None
    elif email_state == "unverified":
        active_user.emailaddress_set.update(verified=False)
    else:
        active_user.email = ("member" * 10) + "@example.com"
        active_user.emailaddress_set.update(email=active_user.email)
    active_user.save(update_fields=["email", "login_phone_number"])
    if legacy_phone:
        MemberProfile.objects.create(user=active_user, phone_number=legacy_phone)
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(reverse("account"))
    assert response.status_code == 200
    text = response.content.decode()
    assert ("<h1>Tài khoản</h1>" if language == "vi" else "<h1>Account</h1>") in text
    for unwanted in (
        "Personal profile",
        "Hồ sơ cá nhân",
        "auth-footer",
        "Sign-in session",
        "Phiên đăng nhập",
        "Active account",
        "Đang hoạt động",
        "No login phone number",
        "Chưa có số điện thoại đăng nhập",
    ):
        assert unwanted not in text
    if legacy_phone:
        assert legacy_phone not in text
    completion = reverse("account_complete")
    add_phone = "Thêm số điện thoại" if language == "vi" else "Add phone number"
    assert f'href="{completion}#phone"' not in text
    if login_phone:
        assert active_user.login_phone_number in text
        assert add_phone not in text
        assert 'name="login_phone_number"' not in text
    else:
        controls = Controls(response)
        details = controls.get("details", id="add-phone")
        assert "open" not in details
        assert add_phone in text
        controls.get("span", **{"class": "auth-profile-value auth-profile-action"})
        controls.get("form", method="post", action=reverse("account_phone_add"))
        phone = controls.field("login_phone_number", "tel")
        password = controls.field("current_password", "password")
        assert phone["autocomplete"] == "tel"
        assert password["autocomplete"] == "current-password"
    if email_state == "missing":
        label = "Thêm email" if language == "vi" else "Add email"
        assert f'href="{completion}#email">{label}</a>' in text
    if email_state != "missing":
        assert active_user.email in text
    if email_state == "verified":
        values = re.findall(r'<dd class="auth-profile-value">(.*?)</dd>', text, re.S)
        email_value = next(value for value in values if active_user.email in value)
        assert "auth-pill" in email_value
        assert ("Đã xác minh" if language == "vi" else "Verified") in email_value
        assert f'href="{completion}#email"' not in text
    elif email_state == "unverified":
        label = "Xác minh email" if language == "vi" else "Verify email"
        assert f'href="{completion}#email">{label}</a>' in text
    assert f'href="{completion}"' not in text
    completion_controls = Controls(client.get(completion))
    completion_controls.get("h2", id="email")
    assert not any(
        attrs.get("action") == reverse("account_phone_add")
        for tag, attrs in completion_controls.elements
        if tag == "form"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("language", ["vi", "en"])
def test_dashboard_birth_date_has_single_bound_value_and_calendar_controls(
    client, active_user, language
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    MemberProfile.objects.create(user=active_user, date_of_birth="1990-06-15")
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(reverse("account"))
    assert response.status_code == 200
    controls = Controls(response)
    date = controls.field("date_of_birth", "date")
    assert date["value"] == "1990-06-15"
    assert date["autocomplete"] == "bday"
    assert date["max"] == timezone.localdate().isoformat()
    assert sum(attrs.get("name") == "date_of_birth" for _, attrs in controls.elements) == 1
    trigger = controls.get("button", type="button", popovertarget="profile-date-calendar")
    assert "hidden" in trigger
    controls.get("div", id="profile-date-calendar", role="dialog", popover="auto")
    controls.get("details", **{"data-calendar-month": None})
    controls.get("summary", autofocus=None)
    controls.get("div", **{"data-calendar-month-options": None})
    year = controls.get("input", type="text", inputmode="numeric", maxlength="4")
    assert "name" not in year and "data-calendar-year" in year
    controls.get("div", id=year["aria-describedby"])


@pytest.mark.django_db
def test_allauth_fallback_uses_shared_layout(client, settings, monkeypatch):
    monkeypatch.setattr(
        "identity.adapters.CoreAccountAdapter.is_open_for_signup", lambda self, request: False
    )
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    response = client.get("/register")
    assert response.status_code == 200
    controls = Controls(response)
    controls.get("link", rel="stylesheet", href="/static/css/account-ui.css")
    assert "Natural Farming Vietnam" in response.content.decode()
    assert "Sign Up Closed" in response.content.decode()
    assert b"<c-card" not in response.content


@pytest.mark.django_db
def test_oauth_choice_cancel_and_consent_keep_native_submit_controls(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    path = reverse("authorize")
    request = {
        "client_id": oauth_application.client_id,
        "redirect_uri": oauth_application.redirect_uris,
        "response_type": "code",
        "scope": "profile email",
        "state": "ui-contract",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
    }
    consent = client.get(path, request)
    assert consent.status_code == 200
    controls = Controls(consent)
    decision_form = controls.get("form", method="post", id="oauth-consent-form")
    allow = controls.get(
        "button", type="submit", form=decision_form["id"], name="allow", value="Authorize"
    )
    decline = next(
        attrs
        for tag, attrs in controls.elements
        if tag == "button"
        and attrs.get("type") == "submit"
        and attrs.get("form") == decision_form["id"]
        and "name" not in attrs
    )
    logout = controls.get("form", method="post", action=reverse("account_logout"))
    assert (
        controls.elements.index(("button", allow))
        < controls.elements.index(("button", decline))
        < controls.elements.index(("form", logout))
    )
    for field in consent.context["form"].hidden_fields():
        hidden = controls.get("input", type="hidden", name=field.html_name)
        value = field.value()
        assert hidden.get("value", "") == ("" if value is None else str(value))

    choice = client.get(path, {**request, "screen_hint": "signup"})
    assert choice.status_code == 200
    controls = Controls(choice)
    controls.get("form", method="post", action=reverse("resolve-signup-account"))
    controls.get("input", type="hidden", name="next", value=choice.context["oauth_next"])
    for action in ("continue", "logout"):
        controls.get("button", type="submit", name="action", value=action)


@pytest.mark.django_db
def test_oauth_consent_logout_allows_sign_in_with_another_account(active_user, oauth_application):
    another_user = User.objects.create_user(
        email="another-member@example.com",
        name="Another member",
        password="StrongPassword12",
        status=active_user.status,
    )
    client = Client(enforce_csrf_checks=True)
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    authorization = {
        "client_id": oauth_application.client_id,
        "redirect_uri": oauth_application.redirect_uris,
        "response_type": "code",
        "scope": "profile email",
        "state": "consent + logout/state",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "ui_locales": "vi",
    }
    consent = client.get(reverse("authorize"), authorization)
    assert consent.status_code == 200
    assert active_user.email in consent.content.decode()
    controls = Controls(consent)
    logout_url = reverse("account_logout")
    logout_index = next(
        index
        for index, (tag, attrs) in enumerate(controls.elements)
        if tag == "form" and attrs.get("action") == logout_url and attrs.get("method") == "post"
    )
    assert "Đăng xuất" in consent.content.decode()
    csrf, continuation, button = controls.elements[logout_index + 1 : logout_index + 4]
    assert csrf[0] == "input" and csrf[1]["name"] == "csrfmiddlewaretoken"
    assert continuation[0] == "input" and continuation[1]["name"] == "next"
    assert button[0] == "button" and button[1]["type"] == "submit"
    logout_data = {"next": continuation[1]["value"]}
    assert client.post(logout_url, logout_data).status_code == 403
    assert client.session["_auth_user_id"] == str(active_user.pk)
    logout_data["csrfmiddlewaretoken"] = csrf[1]["value"]
    logged_out = client.post(logout_url, logout_data)
    assert logged_out.status_code == 302
    assert "_auth_user_id" not in client.session
    assert parse_qs(urlsplit(logged_out.url).query) == {
        key: [value] for key, value in authorization.items()
    }

    login_redirect = client.get(logged_out.url)
    assert urlsplit(login_redirect.url).path == reverse("account_login")
    login = Controls(client.get(login_redirect.url))
    signed_in = client.post(
        reverse("account_login"),
        {
            "login": another_user.email,
            "password": "StrongPassword12",
            "next": login.get("input", type="hidden", name="next", value=logged_out.url)["value"],
            "csrfmiddlewaretoken": login.get("input", name="csrfmiddlewaretoken")["value"],
        },
    )
    assert signed_in.status_code == 302
    assert parse_qs(urlsplit(signed_in.url).query) == parse_qs(urlsplit(logged_out.url).query)
    resumed = client.get(signed_in.url)
    assert resumed.status_code == 200
    assert another_user.email in resumed.content.decode()
