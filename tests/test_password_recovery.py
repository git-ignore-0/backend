import re
from types import SimpleNamespace

import pytest
from allauth.account.forms import default_token_generator
from allauth.account.models import EmailAddress
from allauth.account.utils import user_pk_to_url_str
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone
from oauth2_provider.models import AccessToken, Grant, RefreshToken

from identity.models import IdentityEvent, User
from identity.oauth import CoreOAuth2Validator

OLD_PASSWORD = "StrongPassword12"
NEW_PASSWORD = "NewStrongPassword34"


@pytest.fixture(autouse=True)
def isolated_auth_rate_limits():
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


def key_path(user):
    return reverse(
        "account_reset_password_from_key",
        kwargs={
            "uidb36": user_pk_to_url_str(user),
            "key": default_token_generator.make_token(user),
        },
    )


def reset_payload(**changes):
    return {"password1": NEW_PASSWORD, "password2": NEW_PASSWORD, **changes}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("language", "password_hint", "confirmation_hint"),
    [("vi", "Mật khẩu", "Nhập lại mật khẩu"), ("en", "Password", "Re-enter your password")],
)
def test_new_password_page_has_required_markers_and_consistent_hints(
    client, active_user, settings, language, password_hint, confirmation_hint
):
    client.cookies[settings.LANGUAGE_COOKIE_NAME] = language
    response = client.get(key_path(active_user), follow=True)

    assert response.status_code == 200
    html = response.content.decode()
    for field, placeholder in {"password1": password_hint, "password2": confirmation_hint}.items():
        label = re.search(rf'<label\b[^>]*for="id_{field}"[^>]*>(.*?)</label>', html, re.S)
        assert label and '<span aria-hidden="true">*</span>' in label[1]
        assert response.context["form"].fields[field].widget.attrs["placeholder"] == placeholder
    rules = re.search(r'<div class="auth-help" id="id_password1_helptext">(.*?)</div>', html)
    assert rules and rules[1].count("<li>") == 4


@pytest.mark.django_db
@pytest.mark.parametrize(
    "kind",
    [
        "verified",
        "unverified",
        "unknown",
        "suspended",
        "deletion_requested",
        "anonymized",
        "wrong_accent",
    ],
)
def test_reset_request_sends_only_for_current_verified_email(client, mailoutbox, kind):
    email = "person@example.com"
    if kind != "unknown":
        email = "person@café.vn" if kind == "wrong_accent" else email
        locked = kind in {"suspended", "deletion_requested", "anonymized"}
        user = User.objects.create_user(
            email,
            OLD_PASSWORD,
            name="Member",
            status=kind if locked else "active",
            is_active=not locked,
        )
        EmailAddress.objects.create(
            user=user,
            email="person@cafe.vn" if kind == "wrong_accent" else email,
            primary=True,
            verified=kind != "unverified",
        )
    response = client.post("/accounts/password/reset/", {"email": email})
    assert response.status_code == 302 and response.url == "/accounts/password/reset/done/"
    assert len(mailoutbox) == (1 if kind == "verified" else 0)
    page = client.get(response.url)
    assert page.status_code == 200
    assert "admin" in page.content.decode().lower() or "quản trị" in page.content.decode().lower()
    if mailoutbox:
        assert mailoutbox[0].to == [email]
        token_url = re.search(
            r"https?://[^\s]+/accounts/password/reset/key/[^\s]+", mailoutbox[0].body
        )
        assert token_url is not None and token_url.group().encode() not in page.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["unverified", "email_changed", "suspended", "deletion_requested", "anonymized"]
)
def test_reset_rechecks_evidence_after_token_was_accepted(client, active_user, change):
    accepted = client.get(key_path(active_user))
    assert accepted.status_code == 302
    if change == "unverified":
        EmailAddress.objects.filter(user=active_user).update(verified=False)
    elif change == "email_changed":
        User.objects.filter(pk=active_user.pk).update(email="changed@example.com")
    else:
        User.objects.filter(pk=active_user.pk).update(status=change, is_active=False)
    response = client.post(accepted.url, reset_payload())
    assert response.status_code == 200
    active_user.refresh_from_db()
    assert active_user.check_password(OLD_PASSWORD)
    assert not IdentityEvent.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_reset_invalidates_old_password_session_tokens_and_codes_only_for_target(
    client, active_user, oauth_application, access_token
):
    old_session = Client()
    old_session.force_login(active_user, backend="identity.backends.CoreAuthenticationBackend")
    refresh = RefreshToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="old-refresh",
        access_token=access_token,
    )
    Grant.objects.create(
        user=active_user,
        application=oauth_application,
        code="old-code",
        expires=timezone.now() + timezone.timedelta(minutes=1),
        scope="profile email",
        redirect_uri="https://forum.example.com/auth/nfv",
    )
    other = User.objects.create_user(
        "other@example.com", OLD_PASSWORD, name="Other", status="active"
    )
    other_access = AccessToken.objects.create(
        user=other,
        application=oauth_application,
        token="other-access",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )
    other_refresh = RefreshToken.objects.create(
        user=other, application=oauth_application, token="other-refresh", access_token=other_access
    )
    other_grant = Grant.objects.create(
        user=other,
        application=oauth_application,
        code="other-code",
        expires=timezone.now() + timezone.timedelta(minutes=1),
        scope="profile email",
        redirect_uri="https://forum.example.com/auth/nfv",
    )
    token_path = key_path(active_user)
    accepted = client.get(token_path)
    assert accepted.status_code == 302
    assert client.post(accepted.url, reset_payload()).status_code == 302
    active_user.refresh_from_db()
    assert active_user.check_password(NEW_PASSWORD) and not active_user.check_password(OLD_PASSWORD)
    assert active_user.status == "active" and active_user.email_verified
    assert not AccessToken.objects.filter(user=active_user).exists()
    assert not RefreshToken.objects.filter(pk=refresh.pk, revoked__isnull=True).exists()
    assert not Grant.objects.filter(user=active_user).exists()
    assert AccessToken.objects.filter(pk=other_access.pk).exists()
    assert RefreshToken.objects.filter(pk=other_refresh.pk, revoked__isnull=True).exists()
    assert Grant.objects.filter(pk=other_grant.pk).exists()
    assert old_session.get("/account").status_code == 302
    assert "_auth_user_id" not in old_session.session and "_auth_user_id" not in client.session
    assert client.get(token_path).status_code == 200
    assert not CoreOAuth2Validator().validate_refresh_token(
        "old-refresh", oauth_application, SimpleNamespace(user=None)
    )
    assert not CoreOAuth2Validator().validate_code(
        oauth_application.client_id,
        "old-code",
        oauth_application,
        SimpleNamespace(user=None, scopes=None),
    )
    assert not IdentityEvent.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_revoke_helper_is_scoped_and_idempotent(active_user, access_token):
    from identity.passwords import revoke_core_credentials

    original_hash = active_user.password
    assert revoke_core_credentials(user=active_user) is None
    assert revoke_core_credentials(user=active_user) is None
    active_user.refresh_from_db()
    assert (
        active_user.password == original_hash
        and active_user.status == "active"
        and active_user.email_verified
    )
    assert not AccessToken.objects.filter(pk=access_token.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes", [{"password2": "mismatch"}, {"password1": "short", "password2": "short"}]
)
def test_reset_keeps_password_policy_and_confirm(client, active_user, changes):
    accepted = client.get(key_path(active_user))
    assert client.post(accepted.url, reset_payload(**changes)).status_code == 200
    active_user.refresh_from_db()
    assert active_user.check_password(OLD_PASSWORD)


@pytest.mark.django_db
def test_pending_verified_reset_does_not_activate(client):
    user = User.objects.create_user("pending@example.com", OLD_PASSWORD, name="Pending")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    accepted = client.get(key_path(user))
    assert client.post(accepted.url, reset_payload()).status_code == 302
    user.refresh_from_db()
    assert user.status == "pending_activation" and user.email_verified
    assert not IdentityEvent.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_expired_and_bad_tokens_do_not_change_password(client, active_user, monkeypatch):
    from datetime import timedelta

    now = default_token_generator._now()
    with monkeypatch.context() as context:
        context.setattr(default_token_generator, "_now", lambda: now - timedelta(days=30))
        expired = key_path(active_user)
    for path in [
        expired,
        reverse(
            "account_reset_password_from_key",
            kwargs={"uidb36": user_pk_to_url_str(active_user), "key": "bad-token"},
        ),
    ]:
        assert client.get(path).status_code == 200
        assert client.post(path, reset_payload()).status_code == 200
    active_user.refresh_from_db()
    assert active_user.check_password(OLD_PASSWORD)


@pytest.mark.django_db
def test_reset_rollback_restores_password_and_credentials_if_revocation_fails(
    client, active_user, access_token, monkeypatch
):
    from django.db import IntegrityError

    accepted = client.get(key_path(active_user))

    def fail(**kwargs):
        raise IntegrityError("Revocation failed")

    monkeypatch.setattr("identity.recovery.revoke_core_credentials", fail)
    with pytest.raises(IntegrityError):
        client.post(accepted.url, reset_payload())
    active_user.refresh_from_db()
    assert active_user.check_password(OLD_PASSWORD)
    assert AccessToken.objects.filter(pk=access_token.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("failure_stage", ["token_validation", "password_save", "revocation"])
def test_reset_exception_report_redacts_new_passwords_and_rolls_back(
    client, active_user, access_token, monkeypatch, settings, failure_stage
):
    from django.views.debug import ExceptionReporter, SafeExceptionReporterFilter

    from identity.adapters import CoreAccountAdapter
    from identity.passwords import revoke_core_credentials

    accepted = client.get(key_path(active_user))
    assert accepted.status_code == 302
    original_hash = active_user.password

    def fail(*args, **kwargs):
        if failure_stage == "revocation":
            revoke_core_credentials(**kwargs)
        raise RuntimeError("Synthetic reset failure")

    if failure_stage == "password_save":
        save_password = CoreAccountAdapter.set_password

        def fail_after_password_save(adapter, user, password):
            save_password(adapter, user, password)
            raise RuntimeError("Synthetic password save failure")

        monkeypatch.setattr(CoreAccountAdapter, "set_password", fail_after_password_save)
    else:
        target = (
            "identity.recovery.CoreUserTokenForm.is_valid"
            if failure_stage == "token_validation"
            else "identity.recovery.revoke_core_credentials"
        )
        monkeypatch.setattr(target, fail)
    settings.DEBUG = False
    client.raise_request_exception = False
    response = client.post(accepted.url, reset_payload())

    assert response.status_code == 500
    assert response.exc_info[0] is RuntimeError
    safe_post = SafeExceptionReporterFilter().get_post_parameters(response.wsgi_request)
    assert safe_post["password1"] == "********************"
    assert safe_post["password2"] == "********************"
    report = ExceptionReporter(response.wsgi_request, *response.exc_info).get_traceback_html()
    assert NEW_PASSWORD not in report
    active_user.refresh_from_db()
    assert active_user.password == original_hash
    assert AccessToken.objects.filter(pk=access_token.pk).exists()
    assert active_user.status == "active" and active_user.email_verified


@pytest.mark.django_db
def test_reset_link_csrf_and_generic_result_are_identical_for_eligible_and_unknown(
    client, active_user
):
    from django.core.cache import cache

    pages = []
    for email in [active_user.email, "unknown@example.com"]:
        cache.clear()
        browser = Client()
        response = browser.post("/accounts/password/reset/", {"email": email}, follow=True)
        pages.append(response.context["request"].path)
        assert (
            "admin" in response.content.decode().lower()
            or "quản trị" in response.content.decode().lower()
        )
    assert pages == ["/accounts/password/reset/done/"] * 2
    browser = Client(enforce_csrf_checks=True)
    assert (
        browser.post("/accounts/password/reset/", {"email": active_user.email}).status_code == 403
    )


@pytest.mark.django_db(transaction=True)
def test_reset_request_mail_failure_is_generic_and_mail_runs_outside_lock(
    active_user, monkeypatch, caplog
):
    from smtplib import SMTPConnectError

    from django.core.mail.backends.locmem import EmailBackend
    from django.db import connection

    sent_inside_transaction = []

    def fail(self, email_messages):
        sent_inside_transaction.append(connection.in_atomic_block)
        raise SMTPConnectError(421, "Synthetic sensitive SMTP details")

    monkeypatch.setattr(EmailBackend, "send_messages", fail)
    eligible = Client()
    unknown = Client()
    with_mail = eligible.post("/accounts/password/reset/", {"email": active_user.email})
    no_mail = unknown.post("/accounts/password/reset/", {"email": "unknown@example.com"})
    assert with_mail.status_code == no_mail.status_code == 302
    assert with_mail.url == no_mail.url == "/accounts/password/reset/done/"
    public_text = re.compile(rb"<p>([^<]+)</p>")
    eligible_text = public_text.findall(eligible.get(with_mail.url).content)
    assert eligible_text
    assert eligible_text == public_text.findall(unknown.get(no_mail.url).content)
    assert sent_inside_transaction == [False]
    assert "Password reset email transport failed." in caplog.text
    assert active_user.email not in caplog.text
    assert "Synthetic sensitive SMTP details" not in caplog.text


@pytest.mark.django_db
def test_reset_request_does_not_hide_mail_preparation_errors(active_user, monkeypatch):
    from identity.adapters import CoreAccountAdapter

    def fail(self, key):
        raise RuntimeError("Reset link preparation failed")

    monkeypatch.setattr(CoreAccountAdapter, "get_reset_password_from_key_url", fail)
    with pytest.raises(RuntimeError, match="Reset link preparation failed"):
        Client().post("/accounts/password/reset/", {"email": active_user.email})


@override_settings(ACCOUNT_EMAIL_NOTIFICATIONS=True)
@pytest.mark.django_db(transaction=True)
def test_reset_notification_runs_after_password_and_revocation_commit(
    client, active_user, monkeypatch, caplog
):
    from smtplib import SMTPConnectError

    from django.core.mail.backends.locmem import EmailBackend
    from django.db import connection

    opened = client.get(key_path(active_user))
    attempts = []

    def fail(self, email_messages):
        attempts.append(connection.in_atomic_block)
        raise SMTPConnectError(421, "Synthetic sensitive SMTP details")

    monkeypatch.setattr(EmailBackend, "send_messages", fail)
    response = client.post(opened.url, reset_payload())

    assert response.url == reverse("account_reset_password_from_key_done")
    assert attempts == [False]
    active_user.refresh_from_db()
    assert active_user.check_password(NEW_PASSWORD)
    assert "Password reset notification email transport failed." in caplog.text
    assert "Synthetic sensitive SMTP details" not in caplog.text


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["refresh", "code"])
@pytest.mark.parametrize("script_name", ["", "/core"])
def test_native_token_exchange_runs_in_user_serialization_transaction(
    client, active_user, oauth_application, access_token, monkeypatch, kind, script_name
):
    import base64
    import hashlib

    from django.db import connection

    observed = []
    method = "validate_refresh_token" if kind == "refresh" else "validate_code"
    original = getattr(CoreOAuth2Validator, method)

    def validate(self, *args, **kwargs):
        observed.append(connection.in_atomic_block)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CoreOAuth2Validator, method, validate)
    data = {"client_id": oauth_application.client_id, "client_secret": "test-forum-secret"}
    if kind == "refresh":
        RefreshToken.objects.create(
            user=active_user,
            application=oauth_application,
            token="serialize-refresh",
            access_token=access_token,
        )
        data.update(grant_type="refresh_token", refresh_token="serialize-refresh")
    else:
        verifier = "a" * 64
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        Grant.objects.create(
            user=active_user,
            application=oauth_application,
            code="serialize-code",
            redirect_uri="https://forum.example.com/auth/nfv",
            scope="profile email",
            expires=timezone.now() + timezone.timedelta(minutes=1),
            code_challenge=challenge,
            code_challenge_method="S256",
        )
        data.update(
            grant_type="authorization_code",
            code="serialize-code",
            code_verifier=verifier,
            redirect_uri="https://forum.example.com/auth/nfv",
        )
    response = client.post("/oauth/token", data, SCRIPT_NAME=script_name)
    assert response.status_code == 200
    assert observed == [True]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["refresh", "code"])
def test_reset_and_inflight_native_exchange_share_user_lock(
    active_user, oauth_application, access_token, monkeypatch, kind
):
    import base64
    import hashlib
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections, connection

    if not connection.features.has_select_for_update:
        pytest.skip("Requires row locking to verify overlapping reset and token exchange")
    data = {"client_id": oauth_application.client_id, "client_secret": "test-forum-secret"}
    if kind == "refresh":
        RefreshToken.objects.create(
            user=active_user,
            application=oauth_application,
            token="concurrent-refresh",
            access_token=access_token,
        )
        data.update(grant_type="refresh_token", refresh_token="concurrent-refresh")
    else:
        verifier = "a" * 64
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        Grant.objects.create(
            user=active_user,
            application=oauth_application,
            code="concurrent-code",
            redirect_uri="https://forum.example.com/auth/nfv",
            scope="profile email",
            expires=timezone.now() + timezone.timedelta(minutes=1),
            code_challenge=challenge,
            code_challenge_method="S256",
        )
        data.update(
            grant_type="authorization_code",
            code="concurrent-code",
            code_verifier=verifier,
            redirect_uri="https://forum.example.com/auth/nfv",
        )
    reset_browser = Client()
    form_url = reset_browser.get(key_path(active_user)).url
    validated, resume, reset_started, reset_done = (Event() for _ in range(4))
    method = "validate_refresh_token" if kind == "refresh" else "validate_code"
    original = getattr(CoreOAuth2Validator, method)

    def validate(self, *args, **kwargs):
        valid = original(self, *args, **kwargs)
        if valid:
            validated.set()
            assert resume.wait(timeout=15)
        return valid

    monkeypatch.setattr(CoreOAuth2Validator, method, validate)

    def exchange():
        close_old_connections()
        try:
            return Client().post("/oauth/token", data).status_code
        finally:
            connection.close()

    def reset():
        close_old_connections()
        reset_started.set()
        try:
            return reset_browser.post(form_url, reset_payload()).status_code
        finally:
            reset_done.set()
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        exchanging = pool.submit(exchange)
        try:
            assert validated.wait(timeout=10)
            resetting = pool.submit(reset)
            assert reset_started.wait(timeout=5)
            assert not reset_done.wait(timeout=2)
        finally:
            resume.set()
        assert exchanging.result(timeout=15) == 200
        assert resetting.result(timeout=15) == 302
    assert not AccessToken.objects.filter(user=active_user).exists()
    assert not RefreshToken.objects.filter(user=active_user, revoked__isnull=True).exists()
    assert not Grant.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_reset_form_cleans_email_without_loading_identity(django_assert_num_queries):
    from identity.recovery import CoreResetPasswordForm

    form = CoreResetPasswordForm({"email": " PERSON@Example.com "})
    with django_assert_num_queries(0):
        assert form.is_valid()
    assert form.cleaned_data["email"] == "person@example.com"
