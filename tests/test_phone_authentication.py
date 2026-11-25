from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from allauth.account.adapter import get_adapter
from allauth.account.models import EmailAddress
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from identity.forms import LoginPhoneField
from identity.models import AuditLog, IdentityEvent, User
from members.models import MemberProfile
from referrals.models import ReferralAccount

PASSWORD = "StrongPassword12"
NON_LOCAL_PHONES = [
    "84901234567",
    "+84901234567",
    " (090) 123-45.67 ",
    "090 123 4567",
    "090-123-4567",
    "090a1234567",
    "090!1234567",
    "0901234567 ",
    "\t0901234567",
    "090\r\n1234567",
    "0901234567\n",
]


@pytest.fixture(autouse=True)
def isolated_auth_rate_limits():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def signup_data(**overrides):
    return {
        "name": "New Member",
        "phone": "0901234567",
        "email": "",
        "password1": PASSWORD,
        "password2": PASSWORD,
        **overrides,
    }


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/register", "/register/", "/accounts/signup/"])
@pytest.mark.parametrize("email", ["", " New@EXAMPLE.COM "])
def test_signup_aliases_create_pending_phone_identity_without_login(
    client, mailoutbox, path, email
):
    response = client.post(
        path,
        signup_data(
            email=email,
            status="active",
            is_active=False,
            is_staff=True,
            is_superuser=True,
            verified=True,
        ),
    )
    assert response.status_code == 302
    assert urlsplit(response.url).path in {"/login", "/accounts/login/"}
    assert "_auth_user_id" not in client.session
    user = User.objects.get()
    assert user.login_phone_number == "+84901234567"
    assert user.status == "pending_activation" and user.is_active
    assert not user.is_staff and not user.is_superuser and not user.groups.exists()
    assert user.check_password(PASSWORD)
    assert not IdentityEvent.objects.filter(user=user).exists()
    assert ReferralAccount.objects.filter(user=user).exists()
    if email:
        assert user.email == "new@example.com"
        address = EmailAddress.objects.get(user=user)
        assert address.primary and not address.verified and address.email == user.email
        assert len(mailoutbox) == 1
    else:
        assert user.email is None
        assert not EmailAddress.objects.filter(user=user).exists()
        assert not mailoutbox


@pytest.mark.django_db
@pytest.mark.parametrize("phone", NON_LOCAL_PHONES)
def test_signup_rejects_non_local_phone_input(client, phone):
    response = client.post("/register", signup_data(phone=phone))
    assert response.status_code == 200
    assert response.context["form"].errors.as_data()["phone"][0].code == "invalid_phone"
    assert not User.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes",
    [
        {"phone": ""},
        {"phone": "abc"},
        {"phone": "09012"},
        {"password2": "mismatch"},
        {"password1": "short", "password2": "short"},
    ],
)
def test_signup_validation_creates_no_partial_identity(client, changes):
    response = client.post("/register", signup_data(email="new@example.com", **changes))
    assert response.status_code == 200
    assert response.context["form"].errors
    assert not User.objects.exists() and not ReferralAccount.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("duplicate", ["phone", "email"])
def test_signup_duplicate_identifier_creates_no_identity_or_mail(client, mailoutbox, duplicate):
    user = User.objects.create_user(
        "taken@example.com", name="Existing", login_phone_number="0901234567"
    )
    data = signup_data(email="other@example.com")
    if duplicate == "email":
        data.update(phone="0912345678", email="TAKEN@example.com")
    response = client.post("/register", data)
    assert response.status_code == 302
    assert list(User.objects.values_list("pk", flat=True)) == [user.pk]
    assert not mailoutbox


@pytest.mark.django_db
def test_adapter_commit_false_only_populates_unsaved_complete_user(rf):
    adapter = get_adapter(rf.post("/register"))
    form = SimpleNamespace(_has_phone_field=True, cleaned_data=signup_data(phone="+84901234567"))
    user = adapter.save_user(rf.post("/register"), User(), form, commit=False)
    assert user._state.adding and not User.objects.exists()
    assert user.email is None and user.login_phone_number == "+84901234567"
    assert user.name == "New Member" and user.check_password(PASSWORD)
    assert adapter.get_phone(user) == ("+84901234567", False)


def test_allauth_can_classify_a_canonical_phone_after_form_validation():
    assert get_adapter().phone_form_field().clean("+84901234567") == "+84901234567"


def test_form_accepts_a_domestic_vietnamese_landline():
    assert LoginPhoneField().clean("02412345678") == "+842412345678"


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["pending_activation", "active"])
@pytest.mark.parametrize("login", ["0901234567", "member@example.com", " Member@EXAMPLE.com\t"])
def test_phone_or_email_login_uses_status_not_email_verification(client, status, login):
    user = User.objects.create_user(
        "member@example.com",
        PASSWORD,
        name="Member",
        login_phone_number="0901234567",
        status=status,
    )
    response = client.post("/login", {"login": login, "password": PASSWORD, "remember": True})
    assert response.status_code == 302
    assert response.url == ("/account/complete" if status == "pending_activation" else "/account")
    assert client.session.get("_auth_user_id") == str(user.pk)
    assert not client.session.get_expire_at_browser_close()
    assert AuditLog.objects.filter(user=user, action="auth.login").exists()
    assert get_adapter().get_phone(user) == ("+84901234567", False)


@pytest.mark.django_db
@pytest.mark.parametrize("login", NON_LOCAL_PHONES)
def test_login_rejects_non_local_phone_input(client, login):
    User.objects.create_user(
        name="Phone", login_phone_number="+84901234567", password=PASSWORD, status="active"
    )
    response = client.post("/login", {"login": login, "password": PASSWORD})
    assert response.status_code == 200
    assert response.context["form"].errors.as_data()["login"][0].code == "invalid_phone"
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("status", ["suspended", "deletion_requested", "anonymized"])
def test_locked_phone_login_never_creates_authenticated_session(client, status):
    User.objects.create_user(
        name="Locked",
        login_phone_number="0901234567",
        password=PASSWORD,
        status=status,
        is_active=False,
    )
    response = client.post("/login", {"login": "0901234567", "password": PASSWORD})
    assert response.status_code in {200, 302}
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_phone_login_never_uses_contact_profile(client, active_user):
    MemberProfile.objects.create(user=active_user, phone_number="0901234567")
    response = client.post("/login", {"login": "0901234567", "password": PASSWORD})
    assert response.status_code == 200
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_active_phone_only_user_and_legacy_email_only_user_can_log_in(client, active_user):
    phone_user = User.objects.create_user(
        name="Phone", login_phone_number="0901234567", password=PASSWORD, status="active"
    )
    for login, user in [("0901234567", phone_user), (active_user.email, active_user)]:
        browser = Client()
        response = browser.post("/login", {"login": login, "password": PASSWORD})
        assert response.status_code == 302 and response.url == "/account"
        assert browser.get("/account").status_code == 200
        assert browser.session.get("_auth_user_id") == str(user.pk)


@pytest.mark.django_db
def test_phone_login_is_text_input_and_csrf_protected(client):
    response = client.get("/login")
    assert response.context["form"]["login"].field.widget.input_type == "text"
    browser = Client(enforce_csrf_checks=True)
    assert browser.post("/login", {"login": "0901234567", "password": PASSWORD}).status_code == 403


@pytest.mark.django_db
def test_signup_keeps_safe_continuation_until_pending_login(client):
    next_url = "/oauth/authorize?client_id=public&screen_hint=signup&state=kept"
    response = client.post("/register", signup_data(next=next_url))
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.url).query)["next"] == [next_url]
    login = client.post(
        response.url, {"login": "0901234567", "password": PASSWORD, "next": next_url}
    )
    assert login.status_code == 302 and login.url == "/account/complete"
    assert client.session["nfv_login_next"] == next_url


@pytest.mark.django_db
def test_signup_referral_save_failure_rolls_back_user_email_and_referral(
    client, mailoutbox, monkeypatch
):
    from referrals.models import Referral
    from referrals.services import ReferralCodeInvalid

    referrer = User.objects.create_user("referrer@example.com", name="Referrer")
    code = referrer.referral_account.code
    invite = client.get(
        "/oauth/authorize",
        {
            "client_id": "public-client",
            "response_type": "code",
            "screen_hint": "signup",
            "referral_code": code,
        },
    )
    assert invite.status_code == 302
    continuation = parse_qs(urlsplit(invite.url).query)["next"][0]

    def disappeared(**kwargs):
        raise ReferralCodeInvalid("Referrer disappeared after form validation")

    monkeypatch.setattr("referrals.services.assign_referrer", disappeared)
    response = client.post("/register", signup_data(email="new@example.com", next=continuation))
    assert response.status_code == 200
    assert list(User.objects.values_list("pk", flat=True)) == [referrer.pk]
    assert not EmailAddress.objects.exists() and not Referral.objects.exists() and not mailoutbox
    assert ReferralAccount.objects.count() == 1


@pytest.mark.django_db
def test_phone_login_rate_limit_is_not_bypassed(client, settings):
    settings.ACCOUNT_RATE_LIMITS = {"login": "2/m/ip"}
    User.objects.create_user(name="Phone", login_phone_number="0901234567", password=PASSWORD)
    for _ in range(2):
        assert client.post("/login", {"login": "0901234567", "password": "bad"}).status_code == 200
    assert client.post("/login", {"login": "0901234567", "password": PASSWORD}).status_code == 429
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
@pytest.mark.parametrize("email", ["", "fresh@example.com"])
def test_signup_duplicates_have_same_public_outcome_as_new_registration(
    mailoutbox, settings, email
):
    User.objects.create_user(
        "existing@example.com", name="Existing", login_phone_number="0912345678"
    )
    expected_message = "You can sign in to continue."
    submissions = [signup_data(email=email), signup_data(email=email, phone="0912345678")]
    if email:
        expected_message += (
            " If you provided an email address, check your inbox for a verification link."
        )
        submissions.append(signup_data(email=email, phone="0987654321"))
    for fields in submissions:
        browser = Client()
        browser.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        response = browser.post("/register", fields)
        assert response.status_code == 302 and response.url == reverse("account_login")
        messages = [str(message) for message in get_messages(response.wsgi_request)]
        assert messages == [expected_message]
    assert User.objects.count() == 2
    assert len(mailoutbox) == (1 if email else 0)


@pytest.mark.django_db
def test_signup_rejects_email_whose_canonical_value_exceeds_model_limit(client):
    email = "member@" + ".".join(["İ" * 30] * 5) + ".vn"
    assert len(email) <= 254 < len(email.lower())
    response = client.post("/register", signup_data(email=email))
    assert response.status_code == 200 and not User.objects.exists()


@pytest.mark.django_db
def test_signup_saves_name_and_locale_at_creation_without_extra_user_update(client, settings):
    from django.db.models.signals import post_save

    writes = []

    def capture(sender, instance, created, **kwargs):
        writes.append((created, instance.name, instance.locale))

    client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
    post_save.connect(capture, sender=User)
    try:
        response = client.post("/register", signup_data(name="  New Member  "))
    finally:
        post_save.disconnect(capture, sender=User)
    assert response.status_code == 302
    assert writes == [(True, "New Member", "en")]
