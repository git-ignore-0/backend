import base64
import hashlib
import re
from urllib.parse import urlparse

import pytest
from django.conf import settings
from django.template.loader import render_to_string
from django.utils import translation


@pytest.mark.parametrize(
    ("template_name", "context", "expected"),
    [
        (
            "account/messages/cannot_delete_primary_email.txt",
            {"email": "member@example.com"},
            "Bạn không thể xóa địa chỉ email chính (member@example.com).",
        ),
        (
            "account/messages/email_confirmation_failed.txt",
            {"email": "member@example.com"},
            "Không thể xác minh member@example.com vì địa chỉ này đã được xác minh "
            "bởi một tài khoản khác.",
        ),
        (
            "account/messages/email_confirmation_sent.txt",
            {"email": "member@example.com"},
            "Email xác minh đã được gửi tới member@example.com.",
        ),
        (
            "account/messages/email_confirmed.txt",
            {"email": "member@example.com"},
            "Bạn đã xác minh member@example.com.",
        ),
        (
            "account/messages/email_deleted.txt",
            {"email": "member@example.com"},
            "Đã xóa địa chỉ email member@example.com.",
        ),
        ("account/messages/logged_out.txt", {}, "Bạn đã đăng xuất."),
        (
            "account/messages/login_code_sent.txt",
            {"recipient": "member@example.com"},
            "Mã đăng nhập đã được gửi tới member@example.com.",
        ),
        ("account/messages/password_changed.txt", {}, "Đổi mật khẩu thành công."),
        ("account/messages/password_set.txt", {}, "Đặt mật khẩu thành công."),
        (
            "account/messages/phone_verification_sent.txt",
            {"phone": "0900000000"},
            "Mã xác minh đã được gửi tới 0900000000.",
        ),
        (
            "account/messages/phone_verified.txt",
            {"phone": "0900000000"},
            "Bạn đã xác minh số điện thoại 0900000000.",
        ),
        ("account/messages/primary_email_set.txt", {}, "Đã đặt địa chỉ email chính."),
        (
            "account/messages/unverified_primary_email.txt",
            {},
            "Địa chỉ email chính của bạn phải được xác minh.",
        ),
    ],
)
def test_allauth_account_messages_have_vietnamese_translations(template_name, context, expected):
    with translation.override("vi"):
        rendered = render_to_string(template_name, context).strip()

    assert rendered == expected


def test_language_switch_translates_auth_pages_and_preserves_destination(client):
    response = client.post("/language/", {"language": "en", "next": "/login"})

    assert response.status_code == 302
    assert response.url == "/login"
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"

    login = client.get("/login")
    assert '<html lang="en">' in login.content.decode()
    assert (
        "Use one account for the Natural Farming Vietnam website and forum."
        in login.content.decode()
    )

    client.post("/language/", {"language": "vi", "next": "/register"})
    signup = client.get("/register")
    assert '<html lang="vi">' in signup.content.decode()
    assert "Tài khoản dùng chung" in signup.content.decode()
    assert "Họ và tên" in signup.content.decode()
    assert 'name="locale"' not in signup.content.decode()

    login = client.get("/login")
    assert "Ghi nhớ đăng nhập" in login.content.decode()
    assert "Forgot your password?" not in login.content.decode()


def test_language_switch_rejects_external_destination(client):
    response = client.post(
        "/language/",
        {"language": "en", "next": "https://attacker.example/collect"},
    )

    assert response.status_code == 302
    assert response.url == "/"


@pytest.mark.parametrize(
    "path",
    ["/login", "/register", "/accounts/password/reset/"],
)
def test_auth_form_validation_is_vietnamese(client, path):
    client.post("/language/", {"language": "vi", "next": path})

    response = client.post(path, {})
    content = response.content.decode()

    assert response.status_code == 200
    assert "Trường này là bắt buộc." in content
    assert "This field is required." not in content


@pytest.mark.parametrize(
    ("path", "vietnamese_text", "english_text"),
    [
        ("/login", "Đăng nhập", "Sign in"),
        ("/register", "Tạo tài khoản", "Create account"),
        ("/accounts/confirm-email/", "Kiểm tra email của bạn", "Check your email"),
        ("/accounts/password/reset/", "Đặt lại mật khẩu", "Reset your password"),
        ("/accounts/password/reset/done/", "Đã gửi email", "Email sent"),
        ("/accounts/inactive/", "Tài khoản hiện không thể đăng nhập", "Account unavailable"),
    ],
)
def test_public_auth_pages_use_project_layout_in_both_languages(
    client, path, vietnamese_text, english_text
):
    client.post("/language/", {"language": "vi", "next": path})
    vietnamese = client.get(path)
    vietnamese_content = vietnamese.content.decode()

    assert vietnamese.status_code == 200
    assert '<html lang="vi">' in vietnamese_content
    assert 'class="language-switcher"' in vietnamese_content
    assert vietnamese_text in vietnamese_content
    assert "Menu:" not in vietnamese_content

    client.post("/language/", {"language": "en", "next": path})
    english = client.get(path)
    english_content = english.content.decode()

    assert english.status_code == 200
    assert '<html lang="en">' in english_content
    assert 'class="language-switcher"' in english_content
    assert english_text in english_content
    assert "Menu:" not in english_content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/account", "Tài khoản đang hoạt động"),
        ("/accounts/logout/", "Bạn có chắc muốn kết thúc phiên đăng nhập?"),
        ("/accounts/reauthenticate/", "Xác nhận danh tính"),
        ("/accounts/email/", "Các địa chỉ email"),
        ("/accounts/password/change/", "Đổi mật khẩu"),
    ],
)
def test_authenticated_auth_pages_use_vietnamese_project_layout(
    client, active_user, path, expected
):
    active_user.locale = "vi"
    active_user.save(update_fields=["locale", "updated_at"])
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get(path)
    content = response.content.decode()

    assert response.status_code == 200
    assert '<html lang="vi">' in content
    assert 'class="language-switcher"' in content
    assert expected in content
    assert "Menu:" not in content


@pytest.mark.django_db
def test_authenticated_language_switch_is_saved_to_profile(client, active_user):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    client.post("/language/", {"language": "en", "next": "/account"})

    active_user.refresh_from_db()
    assert active_user.locale == "en"


@pytest.mark.django_db
def test_saved_profile_language_is_used_on_a_new_browser(client, active_user):
    active_user.locale = "en"
    active_user.save(update_fields=["locale", "updated_at"])
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")

    response = client.get("/account")

    assert '<html lang="en">' in response.content.decode()
    assert "Hello, Nông Dân Xanh" in response.content.decode()
    assert response.cookies[settings.LANGUAGE_COOKIE_NAME].value == "en"


@pytest.mark.django_db
def test_login_feedback_is_translated(client, active_user):
    client.post("/language/", {"language": "vi", "next": "/login"})

    success = client.post(
        "/login",
        {"login": active_user.email, "password": "StrongPassword12"},
        follow=True,
    )
    assert f"Đăng nhập thành công với tài khoản {active_user.email}." in success.content.decode()

    signed_out = client.post("/accounts/logout/", follow=True)
    assert "Bạn đã đăng xuất." in signed_out.content.decode()
    assert "You have signed out." not in signed_out.content.decode()

    invalid = client.post(
        "/login",
        {"login": active_user.email, "password": "wrong-password"},
    )
    assert "Địa chỉ email hoặc mật khẩu không chính xác." in invalid.content.decode()


@pytest.mark.django_db
def test_verification_email_uses_registration_language(client, mailoutbox):
    client.post("/language/", {"language": "en", "next": "/register"})
    response = client.post(
        "/register",
        {
            "name": "English Member",
            "email": "english@example.com",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
        },
    )

    assert response.status_code == 302
    assert "Verify your email address" in mailoutbox[0].subject
    assert "Open the link below to verify your email" in mailoutbox[0].body


@pytest.mark.django_db
def test_email_confirmation_feedback_is_fully_vietnamese(client, mailoutbox):
    email = "xac-minh@example.com"
    client.post("/language/", {"language": "vi", "next": "/register"})
    registration = client.post(
        "/register",
        {
            "name": "Người Xác Minh",
            "email": email,
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
        },
        follow=True,
    )
    registration_content = registration.content.decode()
    assert f"Email xác minh đã được gửi tới {email}." in registration_content
    assert f"Confirmation email sent to {email}." not in registration_content
    confirmation_url = re.search(
        r"https?://[^\s]+/accounts/confirm-email/[^\s]+", mailoutbox[0].body
    )
    assert confirmation_url is not None
    confirmation_path = urlparse(confirmation_url.group()).path

    client.get(confirmation_path)
    response = client.post(confirmation_path, follow=True)
    content = response.content.decode()

    assert f"Bạn đã xác minh {email}." in content
    assert f"You have confirmed {email}." not in content


@pytest.mark.django_db
def test_password_reset_email_uses_saved_profile_language(client, active_user, mailoutbox):
    active_user.locale = "en"
    active_user.save(update_fields=["locale", "updated_at"])

    response = client.post("/accounts/password/reset/", {"email": active_user.email})

    assert response.status_code == 302
    assert "Reset your Natural Farming Vietnam password" in mailoutbox[0].subject
    assert "We received a request to reset the password" in mailoutbox[0].body


@pytest.mark.django_db
def test_password_reset_flow_is_fully_vietnamese(client, active_user, mailoutbox):
    active_user.locale = "vi"
    active_user.save(update_fields=["locale", "updated_at"])
    client.post("/language/", {"language": "vi", "next": "/accounts/password/reset/"})
    client.post("/accounts/password/reset/", {"email": active_user.email})
    reset_url = re.search(
        r"https?://[^\s]+/accounts/password/reset/key/[^\s]+",
        mailoutbox[0].body,
    )
    assert reset_url is not None

    token_response = client.get(urlparse(reset_url.group()).path)
    assert token_response.status_code == 302
    reset_form_path = token_response.url
    reset_form = client.get(reset_form_path)
    reset_form_content = reset_form.content.decode()
    assert "Mật khẩu mới" in reset_form_content
    assert "Nhập lại mật khẩu mới" in reset_form_content

    completed = client.post(
        reset_form_path,
        {
            "password1": "NewStrongPassword34",
            "password2": "NewStrongPassword34",
        },
        follow=True,
    )
    completed_content = completed.content.decode()
    assert "Đổi mật khẩu thành công." in completed_content
    assert "Password successfully changed." not in completed_content


@pytest.mark.django_db
def test_unknown_account_email_is_fully_vietnamese(client, mailoutbox):
    client.post("/language/", {"language": "vi", "next": "/accounts/password/reset/"})

    response = client.post(
        "/accounts/password/reset/",
        {"email": "khong-ton-tai@example.com"},
    )

    assert response.status_code == 302
    assert "Không tìm thấy tài khoản" in mailoutbox[0].subject
    assert "không có tài khoản Natural Farming Vietnam nào" in mailoutbox[0].body
    assert "Unknown Account" not in mailoutbox[0].subject


@pytest.mark.django_db
def test_oauth_consent_and_scope_descriptions_are_translated(
    client, active_user, oauth_application
):
    client.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    client.post("/language/", {"language": "vi", "next": "/account"})
    verifier = "a" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )

    response = client.get(
        "/oauth/authorize",
        {
            "client_id": oauth_application.client_id,
            "response_type": "code",
            "redirect_uri": "https://forum.example.com/auth/nfv",
            "scope": "profile email",
            "state": "i18n-test",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    content = response.content.decode()

    assert response.status_code == 200
    assert "Cho phép NFV Forum?" in content
    assert "Đọc địa chỉ email và trạng thái xác minh của bạn" in content
