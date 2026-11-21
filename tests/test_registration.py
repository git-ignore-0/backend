import re

import pytest
from allauth.account.models import EmailAddress
from allauth.account.signals import email_confirmed
from django.db import IntegrityError
from django.urls import reverse

from identity.forms import CoreSignupForm
from identity.models import AccountStatus, IdentityEvent, User


@pytest.mark.django_db
def test_registration_creates_pending_uuid_identity_and_sends_verification(client, mailoutbox):
    response = client.post(
        "/register",
        {
            "phone": "0901234567",
            "name": "  Nông Dân Xanh  ",
            "email": "HELLO@example.com",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
        },
    )

    user = User.objects.get()
    assert response.status_code == 302
    assert user.name == "Nông Dân Xanh"
    assert user.email == "hello@example.com"
    assert user.locale == "vi"
    assert user.status == AccountStatus.PENDING_ACTIVATION
    assert re.fullmatch(r"[0-9a-f-]{36}", str(user.id))
    assert EmailAddress.objects.filter(user=user, email=user.email, verified=False).exists()
    assert len(mailoutbox) == 1
    assert "Xác minh địa chỉ email của bạn" in mailoutbox[0].subject
    assert "Mở liên kết bên dưới để xác minh email" in mailoutbox[0].body
    assert response.url == reverse("account_login")
    page = client.get(response.url).content.decode()
    toast = page.split('class="auth-toast-stack"', 1)[1]
    message = (
        "Bạn có thể đăng nhập để tiếp tục. "
        "Nếu bạn đã nhập email, hãy kiểm tra hộp thư để tìm liên kết xác minh."
    )
    assert message in toast
    assert 'x-init="show(true)"' in toast
    assert message not in client.get(response.url).content.decode()


@pytest.mark.django_db
@pytest.mark.parametrize("duplicate_field", ["login_phone_number", "email"])
def test_signup_identifier_conflict_during_write_uses_generic_toast(
    client, duplicate_field, monkeypatch, mailoutbox
):
    original_clean = CoreSignupForm.clean
    original_save = CoreSignupForm.save
    write_failed = False

    def clean_before_duplicate_is_visible(form):
        cleaned_data = original_clean(form)
        form.account_already_exists = False
        return cleaned_data

    def save_with_conflict(form, request):
        nonlocal write_failed
        try:
            return original_save(form, request)
        except IntegrityError:
            write_failed = True
            raise

    monkeypatch.setattr(CoreSignupForm, "clean", clean_before_duplicate_is_visible)
    monkeypatch.setattr(CoreSignupForm, "save", save_with_conflict)
    value = "+84901234567" if duplicate_field == "login_phone_number" else "hello@example.com"
    existing = User.objects.create_user(name="Existing member", **{duplicate_field: value})

    response = client.post(
        "/register",
        {
            "phone": "0901234567",
            "name": "New member",
            "email": "hello@example.com",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
        },
    )

    assert write_failed
    assert response.url == reverse("account_login")
    assert "Bạn có thể đăng nhập để tiếp tục." in client.get(response.url).content.decode()
    assert list(User.objects.all()) == [existing]
    assert not mailoutbox


@pytest.mark.django_db
def test_signup_rolls_back_nonduplicate_integrity_failure_without_success_notice(
    client, monkeypatch, mailoutbox, settings
):
    settings.DEBUG = False
    client.raise_request_exception = False
    original_save = CoreSignupForm.save

    def fail_after_account_write(form, request):
        original_save(form, request)
        raise IntegrityError("internal write failure")

    monkeypatch.setattr(CoreSignupForm, "save", fail_after_account_write)
    response = client.post(
        "/register",
        {
            "phone": "0901234567",
            "name": "New member",
            "email": "hello@example.com",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
        },
    )

    assert not User.objects.exists()
    assert not EmailAddress.objects.exists()
    assert not mailoutbox
    assert response.status_code == 500
    assert b"internal write failure" not in response.content
    assert (
        "Bạn có thể đăng nhập để tiếp tục."
        not in client.get(reverse("account_login")).content.decode()
    )


@pytest.mark.django_db
def test_confirmed_email_activates_identity_and_creates_outbox_event():
    user = User.objects.create_user(
        email="pending@example.com",
        name="Pending User",
        password="StrongPassword12",
    )
    email = EmailAddress.objects.create(
        user=user,
        email=user.email,
        primary=True,
        verified=True,
    )

    email_confirmed.send(sender=EmailAddress, request=None, email_address=email)

    user.refresh_from_db()
    assert user.status == AccountStatus.ACTIVE
    assert IdentityEvent.objects.filter(
        user=user,
        type="user.activated",
        status="pending",
    ).exists()
