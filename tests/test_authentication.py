import re

import pytest
from allauth.account import app_settings
from allauth.account.models import EmailAddress
from django.db import IntegrityError, transaction
from django.test import override_settings
from django.utils import timezone

from identity.models import AccountStatus, AuditLog, User
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY
from members.models import MemberProfile


@pytest.mark.django_db
def test_active_user_can_log_in(client, active_user):
    response = client.post(
        "/login",
        {"login": active_user.email, "password": "StrongPassword12"},
    )

    assert response.status_code == 302
    assert response.url == "/account"
    assert client.session.get("_auth_user_id") == str(active_user.pk)
    assert AuditLog.objects.filter(user=active_user, action="auth.login").exists()


@override_settings(
    PUBLIC_SITE_ORIGINS=(
        "https://naturalfarmingvietnam.com",
        "https://preview.naturalfarmingvietnam.com",
    )
)
@pytest.mark.django_db
def test_public_logout_clears_core_and_referral_session_then_returns_to_public_site(
    client, active_user
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    session = client.session
    session[SIGNUP_REFERRAL_SESSION_KEY] = active_user.referral_account.code
    session.save()

    response = client.get(
        "/logout/public/",
        {
            "locale": "en",
            "return_to": "https://preview.naturalfarmingvietnam.com/en",
        },
    )

    assert response.status_code == 302
    assert response.url == "https://preview.naturalfarmingvietnam.com/en"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    assert "_auth_user_id" not in client.session
    assert SIGNUP_REFERRAL_SESSION_KEY not in client.session


@override_settings(
    PUBLIC_SITE_ORIGINS=("https://naturalfarmingvietnam.com",),
    LANGUAGE_CODE="vi",
)
@pytest.mark.django_db
def test_public_logout_rejects_external_return_target(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(
        "/logout/public/",
        {"locale": "invalid", "return_to": "https://attacker.example/session"},
    )

    assert response.status_code == 302
    assert response.url == "https://naturalfarmingvietnam.com/vi"
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_suspended_user_cannot_log_in(client):
    user = User.objects.create_user(
        email="suspended@example.com",
        name="Suspended User",
        password="StrongPassword12",
        status=AccountStatus.SUSPENDED,
        is_active=False,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)

    response = client.post(
        "/login",
        {"login": user.email, "password": "StrongPassword12"},
    )

    assert response.status_code == 302
    assert response.url == "/accounts/inactive/"
    assert "_auth_user_id" not in client.session


@pytest.mark.django_db
def test_pending_user_is_sent_to_account_completion(client):
    user = User.objects.create_user(
        email="pending@example.com",
        name="Pending User",
        password="StrongPassword12",
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=False)
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/account")

    assert response.status_code == 302
    assert response.url == "/account/complete"


@pytest.mark.django_db
def test_account_page_does_not_create_empty_member_profile(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/account")

    assert response.status_code == 200
    assert response.context["profile_form"]["name"].value() == active_user.name
    content = response.content.decode()
    assert 'id="account-profile-form"' in content
    assert 'autocomplete="name"' in content
    assert 'type="date"' in content
    assert f'max="{timezone.localdate().isoformat()}"' in content
    assert set(response.context["profile_form"].fields) == {"name", "date_of_birth"}
    assert "Manage email" not in content
    assert "/accounts/email/" not in content
    assert not MemberProfile.objects.filter(user=active_user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("method", ["get", "post"])
def test_account_email_management_is_disabled(client, active_user, method):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = getattr(client, method)("/accounts/email/")

    assert response.status_code == 302
    assert response.url == "/account"
    assert app_settings.MAX_EMAIL_ADDRESSES == 1
    assert not EmailAddress.objects.can_add_email(active_user)
    assert EmailAddress.objects.filter(user=active_user).count() == 1
    with pytest.raises(IntegrityError), transaction.atomic():
        EmailAddress.objects.create(
            user=active_user,
            email="secondary@example.com",
            primary=False,
            verified=False,
        )


@pytest.mark.django_db
def test_account_page_updates_shared_identity_and_member_profile(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/account",
        {
            "name": "Nông Dân Mới",
            "phone_number": "+84 912 345 678",
            "login_phone_number": "0955555555",
            "date_of_birth": "1990-06-15",
            "email": "attacker@example.com",
        },
    )

    assert response.status_code == 302
    assert response.url == "/account"
    active_user.refresh_from_db()
    assert active_user.name == "Nông Dân Mới"
    assert active_user.email == "member@example.com"
    assert active_user.login_phone_number is None
    profile = MemberProfile.objects.get(user=active_user)
    assert profile.phone_number == ""
    assert profile.date_of_birth.isoformat() == "1990-06-15"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("language", "saved_text"),
    [("vi", "Đã lưu thay đổi"), ("en", "Changes saved")],
)
def test_profile_save_feedback_is_shown_once_in_shared_toast(
    client, active_user, settings, language, saved_text
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    login = client.post("/login", {"login": active_user.email, "password": "StrongPassword12"})
    assert login.status_code == 302

    response = client.post(
        "/account", {"name": "Member", "date_of_birth": "1990-06-15"}, follow=True
    )

    assert response.redirect_chain == [("/account", 302)]
    page = response.content.decode()
    toast_stack = page.split('<div class="auth-toast-stack">', 1)[1]
    assert re.findall(r'id="(auth-toast-\d+)"', toast_stack) == ["auth-toast-1", "auth-toast-2"]
    assert toast_stack.count('x-init="show()"') == toast_stack.count('role="status"') == 2
    assert saved_text in toast_stack and page.count(saved_text) == 1
    assert active_user.email in toast_stack  # The separate sign-in message.
    assert page.count("/static/js/vendor/alpine-3.14.8.min.js") == 1
    assert "auth-toast-stack" not in client.get("/account").content.decode()


@pytest.mark.django_db
def test_saving_unchanged_profile_does_not_show_a_toast(client, active_user):
    MemberProfile.objects.create(user=active_user, date_of_birth="1990-06-15")
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/account", {"name": active_user.name, "date_of_birth": "1990-06-15"}, follow=True
    )

    assert response.redirect_chain == [("/account", 302)]
    assert b"auth-toast-stack" not in response.content


@override_settings(PUBLIC_SITE_ORIGINS=("http://localhost:3000",))
@pytest.mark.django_db
def test_account_update_returns_to_validated_public_csa_purchase(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(
        "/account",
        {"return_to": "http://localhost:3000/csa/purchase/vi"},
    )
    assert response.status_code == 200

    response = client.post(
        "/account",
        {
            "name": "Nông Dân Mới",
            "phone_number": "+84 912 345 678",
            "date_of_birth": "1990-06-15",
        },
    )

    assert response.status_code == 302
    assert response.url == "http://localhost:3000/csa/purchase/vi"
    assert "nfv_public_return_to" not in client.session
    assert "Đã lưu thay đổi" not in client.get("/account").content.decode()


@override_settings(PUBLIC_SITE_ORIGINS=("http://localhost:3000",))
@pytest.mark.django_db
def test_account_update_ignores_unvalidated_public_return(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    client.get("/account", {"return_to": "https://attacker.example/csa/purchase/vi"})
    response = client.post(
        "/account",
        {
            "name": "Nông Dân Mới",
            "phone_number": "+84 912 345 678",
            "date_of_birth": "1990-06-15",
        },
    )

    assert response.status_code == 302
    assert response.url == "/account"


@pytest.mark.django_db
def test_account_page_rejects_invalid_profile_fields(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.post(
        "/account",
        {
            "name": active_user.name,
            "phone_number": "call-me",
            "date_of_birth": (timezone.localdate() + timezone.timedelta(days=1)).isoformat(),
        },
    )

    assert response.status_code == 200
    assert set(response.context["profile_form"].errors) == {"date_of_birth"}
    assert 'aria-describedby="id_date_of_birth_error"' in response.content.decode()
    assert not MemberProfile.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_account_page_preserves_legacy_profile_phone_and_address(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    profile = MemberProfile.objects.create(
        user=active_user,
        phone_number="0987654321",
        province_code="66",
        ward_code="22015",
        address="Số 12, ngõ 5",
    )

    response = client.post(
        "/account",
        {
            "name": active_user.name,
            "phone_number": "0912345678",
            "date_of_birth": "1990-06-15",
            "province_code": "01",
            "ward_code": "00004",
            "address": "Địa chỉ thay thế",
        },
    )

    assert response.status_code == 302
    profile.refresh_from_db()
    assert profile.phone_number == "0987654321"
    assert (profile.province_code, profile.ward_code, profile.address) == (
        "66",
        "22015",
        "Số 12, ngõ 5",
    )


def test_administrative_wards_endpoint_returns_localized_wards(client):
    response = client.get(
        "/api/v1/administrative-units/wards",
        {"province": "01"},
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    assert response.json()["data"][0] == {"code": "00004", "name": "Ba Dinh Ward"}


def test_administrative_provinces_endpoint_returns_localized_provinces(client):
    response = client.get(
        "/api/v1/administrative-units/provinces",
        HTTP_ACCEPT_LANGUAGE="en",
    )

    assert response.status_code == 200
    assert response.json()["data"][0] == {"code": "01", "name": "Ha Noi City"}
    assert len(response.json()["data"]) == 34


def test_administrative_wards_endpoint_rejects_unknown_province(client):
    response = client.get(
        "/api/v1/administrative-units/wards",
        {"province": "00"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_province"}
