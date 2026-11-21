"""Verified-current-email recovery using allauth's existing link/token flow."""

from secrets import token_urlsafe
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.forms import (
    ResetPasswordForm,
    ResetPasswordKeyForm,
    UserTokenForm,
    default_token_generator,
)
from allauth.account.internal.flows.password_reset import finalize_password_reset
from allauth.account.utils import user_pk_to_url_str
from allauth.account.views import (
    INTERNAL_RESET_SESSION_KEY,
    PasswordResetFromKeyDoneView,
    PasswordResetFromKeyView,
    PasswordResetView,
)
from django import forms
from django.db import transaction
from django.shortcuts import redirect
from django.utils.crypto import salted_hmac
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.views.decorators.debug import sensitive_post_parameters

from identity.forms import NewPasswordHintsMixin
from identity.mail import deliver_account_email
from identity.models import User
from identity.navigation import (
    LOGIN_NEXT_SESSION_KEY,
    PUBLIC_RETURN_TO_SESSION_KEY,
    password_reset_login_url,
    safe_public_return_to,
)
from identity.passwords import (
    AccountPasswordReporterFilter,
    can_reset_password,
    revoke_core_credentials,
)

_RESET_FLOWS_KEY = "nfv_password_reset_flows"
_OPEN_RESET_FLOWS_KEY = "nfv_open_password_reset_flows"
_RESET_LOGIN_ORIGINS_KEY = "nfv_password_reset_login_origins"


def _reset_token_digest(key):
    return salted_hmac("identity.password_reset.origin", key, algorithm="sha256").hexdigest()


def _with_reset_flow(url, flow):
    parsed = urlsplit(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.append(("flow", flow))
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _remember_reset_flow(session, session_key, flow, value):
    flows = session.get(session_key, {})
    session[session_key] = dict([*list(flows.items())[-7:], (flow, value)])


class CoreResetPasswordForm(ResetPasswordForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].widget.attrs["placeholder"] = "you@example.com"

    def clean_email(self):
        return User.objects.normalize_email(self.cleaned_data["email"])

    def save(self, request, **kwargs):
        email = self.cleaned_data["email"]
        with transaction.atomic():
            user = User.objects.select_for_update().filter(email=email).first()
            if user is None or user.email != email or not can_reset_password(user):
                return email
            # allauth's token generator synchronizes email records and binds the token
            # to the current password, last login and addresses.
            generator = kwargs.get("token_generator") or default_token_generator
            token = generator.make_token(user)
            uid = user_pk_to_url_str(user)

        key = f"{uid}-{token}"
        adapter = get_adapter(request)
        url = adapter.get_reset_password_from_key_url(key)
        return_to = safe_public_return_to(request.GET.get("return_to"))
        if return_to:
            flow = token_urlsafe(16)
            _remember_reset_flow(
                request.session, _RESET_FLOWS_KEY, flow, [_reset_token_digest(key), return_to]
            )
            url = _with_reset_flow(url, flow)
        deliver_account_email(
            "Password reset email transport failed.",
            adapter.send_password_reset_mail,
            user,
            email,
            {
                "user": user,
                "password_reset_url": url,
                "uid": uid,
                "key": token,
                "request": request,
            },
        )
        return email


class CorePasswordResetView(PasswordResetView):
    def get_success_url(self):
        return str(self.success_url)


class CoreUserTokenForm(UserTokenForm):
    def _get_user(self, uidb36):
        user = super()._get_user(uidb36)
        # Check before the framework's token generator, which can sync addresses.
        return user if user is not None and can_reset_password(user) else None


class CoreResetPasswordKeyForm(NewPasswordHintsMixin, ResetPasswordKeyForm):
    show_password_rules = True

    def clean(self):
        cleaned = super().clean()
        user = User.objects.get(pk=self.user.pk)
        if not can_reset_password(user):
            raise get_adapter().validation_error("invalid_password_reset")
        return cleaned

    @transaction.atomic
    def save(self):
        user = User.objects.select_for_update().get(pk=self.user.pk)
        if not can_reset_password(user) or not default_token_generator.check_token(
            user, self.temp_key
        ):
            raise get_adapter().validation_error("invalid_password_reset")
        self.user = user
        super().save()
        revoke_core_credentials(user=user)


@method_decorator(sensitive_post_parameters("password1", "password2"), name="dispatch")
class CorePasswordResetFromKeyView(PasswordResetFromKeyView):
    def dispatch(self, request, uidb36, key, **kwargs):
        request.exception_reporter_filter = AccountPasswordReporterFilter()
        self.opened_reset = None
        if key != self.reset_url_key:
            return super().dispatch(request, uidb36, key, **kwargs)

        session = request.session
        self.open_flow = request.GET.get("flow")
        entry = session.get(_OPEN_RESET_FLOWS_KEY, {}).get(self.open_flow)
        if entry and entry[0] == uidb36:
            self.opened_reset = entry

        # allauth validates the token via its single session slot. Populate it for
        # this request only, without making form reads rewrite the shared session.
        modified = session.modified
        session[INTERNAL_RESET_SESSION_KEY] = self.opened_reset[1] if self.opened_reset else ""
        session.modified = modified
        try:
            return super().dispatch(request, uidb36, key, **kwargs)
        finally:
            modified = session.modified
            session.pop(INTERNAL_RESET_SESSION_KEY, None)
            session.modified = modified

    def logout(self):
        super().logout()
        if self.opened_reset:
            # allauth logs out a different account before showing the reset form.
            # Carry the validated, current form across that session rotation.
            _remember_reset_flow(
                self.request.session, _OPEN_RESET_FLOWS_KEY, self.open_flow, self.opened_reset
            )

    def _reset_origin(self, flow):
        entry = self.request.session.get(_RESET_FLOWS_KEY, {}).get(flow)
        if entry and entry[0] == _reset_token_digest(f"{self.kwargs['uidb36']}-{self.key}"):
            return safe_public_return_to(entry[1])
        return None

    def passthrough_next_url(self, url):
        # allauth calls this only after validating the UID and emailed token.
        session = self.request.session
        return_to = self._reset_origin(self.request.GET.get("flow"))
        flow = token_urlsafe(16)
        entry = [self.kwargs["uidb36"], self.key, return_to]
        session.pop(INTERNAL_RESET_SESSION_KEY, None)
        _remember_reset_flow(session, _OPEN_RESET_FLOWS_KEY, flow, entry)
        return _with_reset_flow(url, flow)

    def form_valid(self, form):
        request = self.request
        if self.opened_reset:
            flow = self.open_flow
            return_to = self.opened_reset[2]
        else:
            flow = request.GET.get("flow")
            return_to = self._reset_origin(flow)
        try:
            form.save()
        except forms.ValidationError:
            form.add_error(None, _("This password reset link is invalid or has expired."))
            return self.form_invalid(form)

        if self.opened_reset:
            flows = request.session.get(_OPEN_RESET_FLOWS_KEY, {})
            flows.pop(flow, None)
            request.session[_OPEN_RESET_FLOWS_KEY] = flows
        response = finalize_password_reset(request, form.user)
        if response:
            return response
        if not app_settings.LOGIN_ON_PASSWORD_RESET and request.user.is_authenticated:
            get_adapter(request).logout(request)
        request.session.pop(INTERNAL_RESET_SESSION_KEY, None)
        request.session.pop(LOGIN_NEXT_SESSION_KEY, None)
        request.session.pop(PUBLIC_RETURN_TO_SESSION_KEY, None)
        done_url = str(self.success_url)
        if return_to:
            _remember_reset_flow(request.session, _RESET_LOGIN_ORIGINS_KEY, flow, return_to)
            done_url = _with_reset_flow(done_url, flow)
        return redirect(done_url)


class CorePasswordResetFromKeyDoneView(PasswordResetFromKeyDoneView):
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        origins = self.request.session.get(_RESET_LOGIN_ORIGINS_KEY, {})
        return_to = origins.get(self.request.GET.get("flow"))
        context["password_reset_login_url"] = password_reset_login_url(self.request, return_to)
        return context
