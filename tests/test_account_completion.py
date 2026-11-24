import re
from datetime import timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from allauth.account.models import EmailAddress, EmailConfirmationHMAC
from django.db import IntegrityError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from identity.models import IdentityEvent, User
from identity.navigation import LOGIN_NEXT_SESSION_KEY, PUBLIC_RETURN_TO_SESSION_KEY
from identity.services import activate_account, transition_account
from members.models import MemberProfile

PASSWORD = "StrongPassword12"


@pytest.fixture(autouse=True)
def isolated_auth_rate_limits():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def pending(db):
    return User.objects.create_user(
        name="Pending", login_phone_number="0901234567", password=PASSWORD
    )


def login(client, user):
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")


@pytest.mark.django_db
def test_pending_profile_post_is_blocked_before_side_effects(client, pending):
    login(client, pending)
    response = client.post(
        "/account",
        {
            "name": "Changed",
            "phone_number": "0909999999",
            "date_of_birth": "1990-06-15",
            "province_code": "66",
            "ward_code": "22015",
            "address": "Test",
        },
    )
    assert response.status_code == 302 and response.url == "/account/complete"
    pending.refresh_from_db()
    assert pending.name == "Pending" and not MemberProfile.objects.filter(user=pending).exists()
    assert client.get("/accounts/password/change/").url == "/account/complete"
    assert client.get("/account/complete").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/me").status_code == 401
    assert client.post("/oauth/token", {}).status_code != 302


@pytest.mark.django_db
@pytest.mark.parametrize("mount", ["", "/core"])
@pytest.mark.parametrize("timing", ["after-first-visit", "before-first-visit", "with-stale-next"])
def test_completion_resumes_oauth_after_admin_activation(
    client, pending, oauth_application, mount, timing
):
    from django.urls import get_script_prefix, set_script_prefix

    login(client, pending)
    query = {
        "client_id": oauth_application.client_id,
        "redirect_uri": oauth_application.redirect_uris,
        "response_type": "code",
        "scope": "profile email",
        "state": "saved/+ state",
        "code_challenge": "a" * 43,
        "code_challenge_method": "S256",
        "screen_hint": "signup",
        "referral_code": "NFV2345678",
    }
    prior = get_script_prefix()
    try:
        set_script_prefix(mount + "/")
        restricted = client.get("/oauth/authorize", query, SCRIPT_NAME=mount)
        assert restricted.url == mount + "/account/complete"
        if timing == "after-first-visit":
            assert client.get("/account/complete", SCRIPT_NAME=mount).status_code == 200
        elif timing == "with-stale-next":
            assert client.get("/account/complete", SCRIPT_NAME=mount).status_code == 200
            session = client.session
            session[LOGIN_NEXT_SESSION_KEY] = (
                mount + "/oauth/authorize?" + urlencode({**query, "state": "different-flow"})
            )
            session.save()
        operator = User.objects.create_superuser("operator@example.com", PASSWORD)
        activate_account(user=pending, source="admin", actor=operator)
        continued = client.get("/account/complete", SCRIPT_NAME=mount)
        assert continued.status_code == 302
        destination = urlsplit(continued.url)
        assert destination.path == mount + "/oauth/authorize"
        assert parse_qs(destination.query) == {
            key: [value]
            for key, value in query.items()
            if key not in {"screen_hint", "referral_code"}
        }
        assert LOGIN_NEXT_SESSION_KEY not in client.session
        assert client.get("/account/complete", SCRIPT_NAME=mount).status_code == 200
    finally:
        set_script_prefix(prior)


@pytest.mark.django_db
def test_active_completion_opened_directly_keeps_email_form_even_with_old_oauth_next(
    client, active_user, oauth_application
):
    active_user.emailaddress_set.filter(email=active_user.email).update(verified=False)
    login(client, active_user)
    session = client.session
    session[LOGIN_NEXT_SESSION_KEY] = "/oauth/authorize?" + urlencode(
        {"client_id": oauth_application.client_id, "state": "other-flow"}
    )
    session.save()
    response = client.get("/account/complete")
    assert response.status_code == 200
    assert b'action="/account/email/resend"' in response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "path", ["/account/email/add", "/account/phone/add", "/account/email/resend"]
)
def test_identifier_mutations_require_login_post_and_csrf(client, pending, path):
    assert client.post(path, {}).status_code == 302
    login(client, pending)
    assert client.get(path).status_code == 405
    browser = Client(enforce_csrf_checks=True)
    login(browser, pending)
    assert browser.post(path, {}).status_code == 403


@pytest.mark.django_db
def test_add_email_requires_current_password_and_starts_unverified(client, pending, mailoutbox):
    login(client, pending)
    assert (
        client.post(
            "/account/email/add", {"email": "new@example.com", "current_password": "bad"}
        ).status_code
        == 200
    )
    pending.refresh_from_db()
    assert (
        pending.email is None
        and not EmailAddress.objects.filter(user=pending).exists()
        and not mailoutbox
    )
    response = client.post(
        "/account/email/add",
        {"email": " New@EXAMPLE.COM ", "current_password": PASSWORD, "user_id": "ignored"},
    )
    assert response.status_code == 302 and response.url == "/account/complete"
    pending.refresh_from_db()
    assert pending.email == "new@example.com" and pending.status == "pending_activation"
    address = EmailAddress.objects.get(user=pending)
    assert address.primary and not address.verified and len(mailoutbox) == 1
    confirm = f"/accounts/confirm-email/{EmailConfirmationHMAC(address).key}/"
    assert client.get(confirm).status_code == 200
    assert not User.objects.get(pk=pending.pk).email_verified
    confirmed = client.post(confirm)
    assert confirmed.status_code == 302 and confirmed.url == "/account"
    pending.refresh_from_db()
    assert pending.status == "active" and pending.email_verified
    assert IdentityEvent.objects.filter(user=pending, type="user.activated").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("expired", [False, True])
def test_add_email_from_password_session_prompts_only_if_expired(
    client, pending, mailoutbox, settings, expired
):
    assert client.post("/login", {"login": "0901234567", "password": PASSWORD}).url == (
        "/account/complete"
    )
    page = client.get("/account/complete")
    assert b'name="email"' in page.content
    assert b'name="current_password"' not in page.content

    if expired:
        settings.ACCOUNT_REAUTHENTICATION_TIMEOUT = 0
    response = client.post("/account/email/add", {"email": "new@example.com"})

    address = EmailAddress.objects.filter(user=pending).first()
    if expired:
        assert response.status_code == 200 and address is None and not mailoutbox
        assert b'name="current_password"' in response.content
        assert b'value="new@example.com"' in response.content
        response = client.post(
            "/account/email/add", {"email": "new@example.com", "current_password": PASSWORD}
        )

    assert response.status_code == 302 and response.url == "/account/complete"
    address = EmailAddress.objects.get(user=pending)
    assert address.email == "new@example.com" and not address.verified
    assert len(mailoutbox) == 1
    completed = client.get(response.url).content.decode()
    assert "Kiểm tra hộp thư và mở link xác minh." in completed
    assert 'action="/account/email/resend"' in completed


@pytest.mark.django_db
def test_active_email_confirmation_does_not_duplicate_activation(client, pending):
    admin = User.objects.create_superuser("operator@example.com", PASSWORD)
    activate_account(user=pending, source="admin", actor=admin)
    login(client, pending)
    response = client.post(
        "/account/email/add", {"email": "new@example.com", "current_password": PASSWORD}
    )
    assert response.status_code == 302
    address = EmailAddress.objects.get(user=pending)
    assert (
        client.post(f"/accounts/confirm-email/{EmailConfirmationHMAC(address).key}/").status_code
        == 302
    )
    assert IdentityEvent.objects.filter(user=pending, type="user.activated").count() == 1


@pytest.mark.django_db
def test_confirmation_does_not_open_user_locked_after_link_issued(client, pending):
    pending.email = "member@example.com"
    pending.save()
    address = EmailAddress.objects.create(
        user=pending, email=pending.email, verified=False, primary=True
    )
    key = EmailConfirmationHMAC(address).key
    transition_account(pending, "suspended")
    assert client.post(f"/accounts/confirm-email/{key}/").status_code == 302
    pending.refresh_from_db()
    assert pending.status == "suspended" and not pending.is_active
    assert not IdentityEvent.objects.filter(user=pending, type="user.activated").exists()


@pytest.mark.django_db
def test_existing_or_inconsistent_email_cannot_be_replaced(client, pending, mailoutbox):
    EmailAddress.objects.create(
        user=pending, email="history@example.com", verified=True, primary=True
    )
    login(client, pending)
    response = client.post(
        "/account/email/add", {"email": "new@example.com", "current_password": PASSWORD}
    )
    assert response.status_code == 200
    pending.refresh_from_db()
    assert pending.email is None and not mailoutbox
    address = EmailAddress.objects.get(user=pending)
    assert address.email == "history@example.com" and address.verified


@pytest.mark.django_db
def test_add_phone_requires_password_and_cannot_replace_existing_number(
    client, active_user, monkeypatch
):
    login(client, active_user)
    MemberProfile.objects.create(user=active_user, phone_number="Contact phone")
    import time

    now = time.time()
    monkeypatch.setattr("allauth.core.internal.ratelimit.time.time", lambda: now)
    for phone, password in [("", PASSWORD), ("abc", PASSWORD), ("0901234567", "bad")]:
        assert (
            client.post(
                "/account/phone/add", {"login_phone_number": phone, "current_password": password}
            ).status_code
            == 200
        )
        assert User.objects.get(pk=active_user.pk).login_phone_number is None
        now += 61
    success = client.post(
        "/account/phone/add",
        {"login_phone_number": "(090) 123-4567", "current_password": PASSWORD},
    )
    assert success.status_code == 302 and success.url == "/account"
    now += 61
    assert (
        client.post(
            "/account/phone/add", {"login_phone_number": "0912345678", "current_password": PASSWORD}
        ).status_code
        == 200
    )
    active_user.refresh_from_db()
    assert active_user.login_phone_number == "+84901234567"
    assert active_user.member_profile.phone_number == "Contact phone"
    assert not IdentityEvent.objects.filter(user=active_user).exists()
    dashboard = client.get("/account").content.decode()
    assert active_user.login_phone_number in dashboard
    assert "Contact phone" not in dashboard
    assert "/account/complete#phone" not in dashboard


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("return_to", "destination"),
    [
        (
            "https://site.example.test/csa/purchase/vi?package=second&page=3",
            "https://site.example.test/csa/purchase/vi?package=second&page=3",
        ),
        ("https://attacker.example/csa/purchase/vi?package=second&page=3", "/account"),
    ],
)
def test_active_phone_add_consumes_validated_website_return(
    client, active_user, settings, return_to, destination
):
    settings.PUBLIC_SITE_ORIGINS = ("https://site.example.test",)
    login(client, active_user)
    assert client.get("/account", {"return_to": return_to}).status_code == 200

    response = client.post(
        "/account/phone/add", {"login_phone_number": "0901234567", "current_password": PASSWORD}
    )

    assert response.status_code == 302 and response.url == destination
    assert PUBLIC_RETURN_TO_SESSION_KEY not in client.session
    active_user.refresh_from_db()
    assert active_user.login_phone_number == "+84901234567"


@pytest.mark.django_db
def test_pending_phone_add_with_website_return_stays_in_completion(client, settings):
    settings.PUBLIC_SITE_ORIGINS = ("https://site.example.test",)
    pending_without_phone = User.objects.create_user(
        email="pending@example.test", name="Pending", password=PASSWORD
    )
    login(client, pending_without_phone)
    return_to = "https://site.example.test/csa/purchase/vi?package=second&page=3"
    assert client.get("/account", {"return_to": return_to}).url == "/account/complete"

    response = client.post(
        "/account/phone/add", {"login_phone_number": "0901234567", "current_password": PASSWORD}
    )

    assert response.status_code == 302 and response.url == "/account/complete"
    assert client.session[PUBLIC_RETURN_TO_SESSION_KEY] == return_to
    pending_without_phone.refresh_from_db()
    assert pending_without_phone.login_phone_number == "+84901234567"
    assert not pending_without_phone.can_access_core()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("number", "password", "error_field"),
    [("0912345678", "bad", "current_password"), ("invalid", PASSWORD, "login_phone_number")],
)
def test_active_phone_errors_render_inside_open_dashboard_form(
    active_user, number, password, error_field
):
    client = Client(enforce_csrf_checks=True)
    login(client, active_user)
    dashboard = client.get("/account")
    endpoint = reverse("account_phone_add")
    data = {"login_phone_number": number, "current_password": password}
    assert client.post(endpoint, data).status_code == 403
    initial_form = re.search(
        rf'<form\b[^>]*action="{endpoint}"[^>]*>(.*?)</form>',
        dashboard.content.decode(),
        re.S,
    )
    assert initial_form
    token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', initial_form[1])[1]

    response = client.post(endpoint, {**data, "csrfmiddlewaretoken": token})
    assert response.status_code == 200
    assert "account/dashboard.html" in [template.name for template in response.templates]
    assert set(response.context["phone_form"].errors) == {error_field}
    assert not response.context["profile_form"].is_bound
    details = re.search(
        r'<details\b[^>]*id="add-phone"[^>]*>(.*?)</details>', response.content.decode(), re.S
    )
    assert details and " open" in details[0].split(">", 1)[0]
    assert f'id="id_{error_field}_error"' in details[1]
    assert password not in details[1]
    active_user.refresh_from_db()
    assert active_user.login_phone_number is None


@pytest.mark.django_db
def test_pending_phone_add_stays_in_completion(client, pending):
    pending.login_phone_number = None
    pending.email = "pending@example.com"
    pending.save(update_fields=["email", "login_phone_number"])
    login(client, pending)
    completion = client.get(reverse("account_complete")).content.decode()
    assert f'action="{reverse("account_phone_add")}"' in completion

    response = client.post(
        reverse("account_phone_add"),
        {"login_phone_number": "0912345678", "current_password": PASSWORD},
    )

    assert response.status_code == 302 and response.url == "/account/complete"
    pending.refresh_from_db()
    assert pending.login_phone_number == "+84912345678"
    assert pending.status == "pending_activation"


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["email", "phone"])
def test_identifier_integrity_conflict_is_safe_form_error_and_rolls_back(
    client, pending, monkeypatch, kind
):
    if kind == "phone":
        pending.login_phone_number = None
        pending.email = "member@example.com"
        pending.save()
    login(client, pending)
    original = User.save

    def conflicting_save(self, *args, **kwargs):
        if self.pk == pending.pk:
            raise IntegrityError("simulated concurrent unique conflict")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(User, "save", conflicting_save)
    data = {
        "email": "new@example.com",
        "login_phone_number": "0912345678",
        "current_password": PASSWORD,
    }
    assert client.post(f"/account/{kind}/add", data).status_code == 200
    pending.refresh_from_db()
    assert not EmailAddress.objects.filter(user=pending).exists()
    assert (pending.email is None) if kind == "email" else (pending.login_phone_number is None)


@pytest.mark.django_db
def test_resend_uses_only_current_email_and_framework_cooldown(client, pending, mailoutbox):
    pending.email = "member@example.com"
    pending.save()
    EmailAddress.objects.create(user=pending, email=pending.email, verified=False, primary=True)
    login(client, pending)
    for _ in range(2):
        response = client.post("/account/email/resend", {"email": "attacker@example.com"})
        assert response.status_code == 302
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [pending.email]
    assert client.get("/accounts/email/").url == "/account/complete"
    assert (
        client.post("/accounts/email/", {"action_add": True, "email": "other@example.com"}).url
        == "/account/complete"
    )
    assert EmailAddress.objects.filter(user=pending).count() == 1


@pytest.mark.django_db
def test_first_verification_email_starts_server_side_resend_countdown(
    client, pending, mailoutbox, monkeypatch
):
    from django.core.cache import cache

    now = timezone.now()
    monkeypatch.setattr("identity.verification.timezone.now", lambda: now)
    login(client, pending)
    response = client.post(
        "/account/email/add",
        {"email": "member@example.com", "current_password": PASSWORD},
        follow=True,
    )
    button = re.search(rb'<button\b[^>]*data-email-resend-seconds="60"[^>]*>', response.content)
    assert button and b"disabled" in button[0]
    assert len(mailoutbox) == 1

    now += timedelta(seconds=59)
    cache.clear()
    blocked = client.post("/account/email/resend", follow=True)
    assert b'data-email-resend-seconds="1"' in blocked.content
    assert len(mailoutbox) == 1

    now += timedelta(seconds=1)
    assert b"data-email-resend-seconds" not in client.get("/account/complete").content
    resent = client.post("/account/email/resend", follow=True)
    assert b'data-email-resend-seconds="60"' in resent.content
    assert len(mailoutbox) == 2


@pytest.mark.django_db
def test_disabled_phone_management_cannot_send_sms_or_change_number(client, pending):
    login(client, pending)
    for path in ["/accounts/phone/change/", "/accounts/phone/verify/"]:
        response = client.post(path, {"phone": "0912345678", "action": "verify"})
        assert response.status_code in {302, 403, 404}
    pending.refresh_from_db()
    assert pending.login_phone_number == "+84901234567"


@pytest.mark.django_db
def test_existing_session_loses_web_access_after_suspension(client, active_user):
    login(client, active_user)
    transition_account(active_user, "suspended")
    response = client.post(
        "/account/phone/add", {"login_phone_number": "0901234567", "current_password": PASSWORD}
    )
    assert response.status_code == 302
    active_user.refresh_from_db()
    assert active_user.login_phone_number is None


@pytest.mark.django_db
def test_phone_add_keeps_framework_rate_limit(client, active_user):
    login(client, active_user)
    data = {"login_phone_number": "0901234567", "current_password": "bad"}
    assert client.post("/account/phone/add", data).status_code == 200
    assert client.post("/account/phone/add", data).status_code == 429
    active_user.refresh_from_db()
    assert active_user.login_phone_number is None


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["email", "phone"])
def test_duplicate_identifier_is_rejected_and_stale_session_cannot_overwrite(client, pending, kind):
    existing = User.objects.create_user(
        "taken@example.com", name="Existing", login_phone_number="0912345678"
    )
    if kind == "phone":
        pending.login_phone_number = None
        pending.email = "pending@example.com"
        pending.save()
    login(client, pending)
    data = {
        "current_password": PASSWORD,
        "email": "taken@example.com",
        "login_phone_number": "0912345678",
    }
    assert client.post(f"/account/{kind}/add", data).status_code == 200
    pending.refresh_from_db()
    assert (pending.email is None) if kind == "email" else (pending.login_phone_number is None)
    existing.refresh_from_db()
    assert existing.email == "taken@example.com" and existing.login_phone_number == "+84912345678"


@pytest.mark.django_db
def test_active_without_email_can_change_password_and_keep_current_session(client):
    user = User.objects.create_user(
        name="Phone", login_phone_number="0901234567", password=PASSWORD, status="active"
    )
    login(client, user)
    response = client.post(
        "/accounts/password/change/",
        {
            "oldpassword": PASSWORD,
            "password1": "NewStrongPassword34",
            "password2": "NewStrongPassword34",
        },
    )
    assert response.status_code == 302 and response.url == "/account"
    assert client.get("/account").status_code == 200
    user.refresh_from_db()
    assert user.check_password("NewStrongPassword34") and user.email is None


@pytest.mark.django_db
@pytest.mark.parametrize("concurrent_change", ["password", "state", "phone"])
def test_add_phone_rechecks_current_data_after_form_validation(
    client, active_user, monkeypatch, concurrent_change
):
    from identity.completion import AddPhoneForm

    login(client, active_user)
    original = AddPhoneForm.is_valid
    changed = False

    def validate(self):
        nonlocal changed
        valid = original(self)
        if valid and not changed:
            changed = True
            current = User.objects.get(pk=active_user.pk)
            if concurrent_change == "password":
                current.set_password("NewStrongPassword34")
                current.save(update_fields=["password"])
            elif concurrent_change == "state":
                transition_account(current, "suspended")
            else:
                current.login_phone_number = "+84912345678"
                current.save(update_fields=["login_phone_number"])
        return valid

    monkeypatch.setattr(AddPhoneForm, "is_valid", validate)
    response = client.post(
        "/account/phone/add", {"login_phone_number": "0901234567", "current_password": PASSWORD}
    )
    assert response.status_code == (403 if concurrent_change == "state" else 200)
    active_user.refresh_from_db()
    assert active_user.login_phone_number == (
        "+84912345678" if concurrent_change == "phone" else None
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["email", "phone"])
def test_two_concurrent_identifier_requests_cannot_claim_same_value(kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection

    if not connection.features.has_select_for_update:
        pytest.skip("Requires database row locking; SQLite stale/conflict tests run separately")
    users = [
        User.objects.create_user(
            email=f"member{index}@example.com" if kind == "phone" else None,
            login_phone_number=f"+8490123456{index}" if kind == "email" else None,
            name=f"Member {index}",
            password=PASSWORD,
            status="active",
        )
        for index in range(2)
    ]
    browsers = [Client(), Client()]
    for browser, user in zip(browsers, users, strict=True):
        login(browser, user)
    start = Barrier(2)

    def add(browser):
        close_old_connections()
        try:
            start.wait(timeout=10)
            return browser.post(
                f"/account/{kind}/add",
                {
                    "email": "shared@example.com",
                    "login_phone_number": "0912345678",
                    "current_password": PASSWORD,
                },
            ).status_code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(add, browsers))
    assert sorted(statuses) == [200, 302]
    lookup = (
        {"email": "shared@example.com"}
        if kind == "email"
        else {"login_phone_number": "+84912345678"}
    )
    assert User.objects.filter(**lookup).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("primary", [False, True])
def test_stale_confirmation_cannot_replace_current_email_or_activate(client, primary):
    user = User.objects.create_user("current@example.com", PASSWORD, name="Pending")
    address = EmailAddress.objects.create(
        user=user, email="historical@example.com", primary=primary, verified=False
    )
    response = client.post(f"/accounts/confirm-email/{EmailConfirmationHMAC(address).key}/")
    assert response.status_code in {200, 302}
    user.refresh_from_db()
    address.refresh_from_db()
    assert user.email == "current@example.com" and user.status == "pending_activation"
    assert not address.verified and address.primary == primary
    assert not IdentityEvent.objects.filter(user=user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "email",
    [
        "x" * 63 + "@" + ".".join(["a" * 63] * 3) + ".com",
        "member@" + ".".join(["İ" * 30] * 5) + ".vn",
    ],
)
def test_add_email_validates_canonical_model_length_before_write(
    client, pending, mailoutbox, email
):
    login(client, pending)
    assert (
        client.post(
            "/account/email/add", {"email": email, "current_password": PASSWORD}
        ).status_code
        == 200
    )
    pending.refresh_from_db()
    assert (
        pending.email is None
        and not EmailAddress.objects.filter(user=pending).exists()
        and not mailoutbox
    )


@pytest.mark.django_db
@pytest.mark.parametrize("kind", ["email", "phone"])
def test_current_password_is_redacted_by_exception_reporter(client, active_user, kind):
    from django.test import override_settings
    from django.views.debug import SafeExceptionReporterFilter

    login(client, active_user)
    response = client.post(
        f"/account/{kind}/add",
        {
            "email": "new@example.com",
            "login_phone_number": "0901234567",
            "current_password": "private-test-password",
        },
    )
    with override_settings(DEBUG=False):
        safe = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
    assert safe["current_password"] == "********************"


@pytest.mark.django_db
def test_restricted_session_cannot_change_password_under_script_prefix(client, pending):
    from django.urls import get_script_prefix, set_script_prefix

    login(client, pending)
    previous = get_script_prefix()
    try:
        # WSGI sets this from SCRIPT_NAME; Django's test client does not.
        set_script_prefix("/core/")
        response = client.post(
            "/accounts/password/change/",
            {
                "oldpassword": PASSWORD,
                "password1": "NewStrongPassword34",
                "password2": "NewStrongPassword34",
            },
            SCRIPT_NAME="/core",
        )
    finally:
        set_script_prefix(previous)
    assert response.status_code == 302 and response.url == "/core/account/complete"
    pending.refresh_from_db()
    assert pending.check_password(PASSWORD)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "mutation_path", ["/account/email/add", "/account/phone/add", "/account/email/resend"]
)
def test_language_switch_from_post_only_form_returns_to_get_completion(
    client, pending, mutation_path
):
    login(client, pending)
    response = client.post("/language/", {"language": "en", "next": mutation_path})
    assert response.status_code == 302 and response.url == "/account/complete"
    assert client.get(response.url).status_code == 200
    pending.refresh_from_db()
    assert pending.locale == "en"


@pytest.mark.django_db
def test_language_switch_after_inline_phone_error_returns_to_dashboard(client, active_user):
    login(client, active_user)
    response = client.post("/language/", {"language": "en", "next": reverse("account_phone_add")})
    assert response.status_code == 302 and response.url == "/account"
    assert client.get(response.url).status_code == 200


@pytest.fixture
def failing_mail_backend(settings, monkeypatch):
    from smtplib import SMTPConnectError

    from django.core.mail.backends.locmem import EmailBackend

    recipients = []

    def fail(self, email_messages):
        recipients.extend(recipient for mail in email_messages for recipient in mail.to)
        raise SMTPConnectError(421, "Synthetic SMTP failure")

    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    monkeypatch.setattr(EmailBackend, "send_messages", fail)
    return recipients


@pytest.mark.django_db
def test_signup_smtp_failure_keeps_pending_account_and_generic_response(
    client, settings, failing_mail_backend
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    data = {
        "name": "Pending",
        "phone": "0901234567",
        "email": "pending@example.com",
        "password1": PASSWORD,
        "password2": PASSWORD,
    }
    first = client.post("/register", data, follow=True)
    repeated = client.post("/register", data, follow=True)
    assert first.redirect_chain == repeated.redirect_chain == [("/accounts/login/", 302)]
    first_messages = [str(message) for message in first.context["messages"]]
    assert first_messages == [str(message) for message in repeated.context["messages"]]
    assert first_messages == [
        "You can sign in to continue. If you provided an email address, "
        "check your inbox for a verification link."
    ]
    assert failing_mail_backend == ["pending@example.com"]
    assert "_auth_user_id" not in client.session
    user = User.objects.get()
    assert user.status == "pending_activation" and not user.email_verified
    address = EmailAddress.objects.get(user=user)
    assert address.primary and address.email == user.email and not address.verified


@pytest.mark.django_db
@pytest.mark.parametrize("stage", ["add", "resend"])
def test_completion_smtp_failure_keeps_unverified_email_and_shows_retry(
    client, pending, settings, failing_mail_backend, stage
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    login(client, pending)
    data = {"email": "pending@example.com", "current_password": PASSWORD}
    if stage == "resend":
        pending.email = data["email"]
        pending.save(update_fields=["email"])
        EmailAddress.objects.create(user=pending, email=pending.email, primary=True)
        data["email"] = "ignored@example.com"

    response = client.post(f"/account/email/{stage}", data, follow=True)
    assert response.redirect_chain == [("/account/complete", 302)]
    assert (
        b"We could not send the verification email. Try sending it again later." in response.content
    )
    assert failing_mail_backend == ["pending@example.com"]
    pending.refresh_from_db()
    assert pending.email == "pending@example.com" and pending.status == "pending_activation"
    assert not EmailAddress.objects.get(user=pending).verified
    assert not IdentityEvent.objects.filter(user=pending).exists()


@pytest.mark.django_db(transaction=True)
def test_resend_claims_cooldown_before_sending_outside_transaction(
    client, pending, mailoutbox, monkeypatch
):
    from django.core.mail.backends.locmem import EmailBackend
    from django.db import connection

    pending.email = "pending@example.com"
    pending.save(update_fields=["email"])
    EmailAddress.objects.create(user=pending, email=pending.email, primary=True)
    login(client, pending)
    original_send = EmailBackend.send_messages
    observed = []

    def send(backend, messages):
        pending.refresh_from_db()
        observed.append((connection.in_atomic_block, pending.email_verification_attempted_at))
        return original_send(backend, messages)

    monkeypatch.setattr(EmailBackend, "send_messages", send)
    assert client.post("/account/email/resend").status_code == 302
    assert client.post("/account/email/resend").status_code == 302
    assert len(mailoutbox) == len(observed) == 1
    assert observed[0][0] is False and observed[0][1] is not None


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("email", "verified", "expected"),
    [
        (None, False, "Add email"),
        ("member@example.com", False, "Verify email"),
        ("member@example.com", True, "Verified"),
    ],
)
def test_dashboard_shows_email_action_or_verified_badge(
    client, pending, settings, email, verified, expected
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    activate_account(
        user=pending,
        source="admin",
        actor=User.objects.create_superuser("operator@example.com", PASSWORD),
    )
    if email:
        pending.email = email
        pending.save(update_fields=["email"])
        EmailAddress.objects.create(user=pending, email=email, verified=verified)
    pending.refresh_from_db()
    login(client, pending)
    response = client.get("/account")
    assert response.status_code == 200
    text = response.content.decode()
    if email:
        assert email in text
    if verified:
        assert re.search(rf">\s*{expected}\s*<", text)
        assert "/account/complete#email" not in text
    else:
        assert f'href="/account/complete#email">{expected}</a>' in text
        assert not re.search(r">\s*Verified\s*<", text)
