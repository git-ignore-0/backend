"""Authenticated, add-only account identifiers and completion pages."""

from time import time

from allauth.account import app_settings
from allauth.account.models import EmailAddress
from allauth.decorators import rate_limit
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.shortcuts import redirect, render
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from identity.forms import PASSWORD_LOGIN_SESSION_KEY, AddPhoneForm, CurrentPasswordForm
from identity.models import User
from identity.navigation import pending_oauth_destination
from identity.verification import (
    claim_account_verification,
    send_account_verification,
    send_claimed_account_verification,
    verification_resend_seconds,
)
from identity.views import render_account_dashboard


class AddEmailForm(CurrentPasswordForm):
    email = forms.EmailField(
        label=_("Email"),
        max_length=User._meta.get_field("email").max_length,
        widget=forms.EmailInput(attrs={"autocomplete": "email", "placeholder": "you@example.com"}),
    )
    field_order = ["email", "current_password"]

    def __init__(self, *args, require_password=True, **kwargs):
        super().__init__(*args, **kwargs)
        if not require_password:
            del self.fields["current_password"]

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"])
        return User._meta.get_field("email").clean(email, None)


def render_completion(request, *, user=None, email_form=None, phone_form=None, error=None):
    if user is None:
        user = User.objects.get(pk=request.user.pk)
    if not user.can_authenticate():
        return render(request, "account/account_inactive.html", status=403)
    return render(
        request,
        "account/complete.html",
        {
            "account_user": user,
            "completion_error": error,
            "email_resend_seconds": verification_resend_seconds(user),
            "email_form": email_form if email_form is not None else AddEmailForm(),
            "phone_form": phone_form if phone_form is not None else AddPhoneForm(),
        },
    )


@login_required
@require_GET
@never_cache
def complete(request):
    user = User.objects.get(pk=request.user.pk)
    if destination := pending_oauth_destination(request, user=user):
        return redirect(destination)
    return render_completion(request, user=user)


def _has_recent_password_login(request):
    user_id, authenticated_at = request.session.get(PASSWORD_LOGIN_SESSION_KEY, (None, 0))
    return user_id == str(request.user.pk) and (
        0 <= time() - authenticated_at < app_settings.REAUTHENTICATION_TIMEOUT
    )


def _add_identifier(request, *, kind):
    require_password = kind == "phone" or not _has_recent_password_login(request)
    form = (
        AddEmailForm(request.POST, require_password=require_password)
        if kind == "email"
        else AddPhoneForm(request.POST)
    )
    address = None
    if form.is_valid():
        try:
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                if not user.can_authenticate():
                    raise forms.ValidationError(_("This account is unavailable."))
                if require_password:
                    if not user.check_password(form.cleaned_data["current_password"]):
                        raise forms.ValidationError(
                            {"current_password": _("The current password is incorrect.")}
                        )
                elif not constant_time_compare(
                    request.user.get_session_auth_hash(), user.get_session_auth_hash()
                ):
                    raise forms.ValidationError(_("Sign in again to continue."))
                field = "email" if kind == "email" else "login_phone_number"
                if getattr(user, field):
                    raise forms.ValidationError(
                        _("This identifier is already set and cannot be changed here.")
                    )
                if kind == "email" and EmailAddress.objects.filter(user=user).exists():
                    raise forms.ValidationError(
                        _("Email records are inconsistent. Contact an administrator.")
                    )
                value = form.cleaned_data[field]
                lookup = {"email__iexact": value} if kind == "email" else {field: value}
                if User.objects.filter(**lookup).exists() or (
                    kind == "email" and EmailAddress.objects.filter(email__iexact=value).exists()
                ):
                    raise forms.ValidationError({field: _("This identifier is already in use.")})
                setattr(user, field, value)
                user.save(update_fields=[field, "updated_at"])
                if kind == "email":
                    address = EmailAddress.objects.create(
                        user=user, email=value, primary=True, verified=False
                    )
        except forms.ValidationError as error:
            form.add_error(None, error)
        except IntegrityError:
            form.add_error(None, _("This identifier is already in use. Reload and try again."))
        else:
            if address:
                send_account_verification(request, address)
            else:
                messages.success(request, _("Account identifier added."))
            if kind == "phone" and user.can_access_core():
                return redirect("account")
            return redirect("account_complete")
    if kind == "phone":
        user = User.objects.get(pk=request.user.pk)
        if user.can_access_core():
            return render_account_dashboard(request, account_user=user, phone_form=form)
        return render_completion(request, user=user, phone_form=form)
    return render_completion(request, email_form=form)


@sensitive_post_parameters("current_password")
@login_required
@require_POST
@never_cache
@rate_limit(action="manage_email")
def add_email(request):
    return _add_identifier(request, kind="email")


@sensitive_post_parameters("current_password")
@login_required
@require_POST
@never_cache
@rate_limit(action="change_phone")
def add_phone(request):
    return _add_identifier(request, kind="phone")


@login_required
@require_POST
@never_cache
@rate_limit(action="manage_email")
def resend_email(request):
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=request.user.pk)
        if not user.can_authenticate():
            return render(request, "account/account_inactive.html", status=403)
        address = EmailAddress.objects.filter(user=user).first()
        if (
            not user.email
            or not address
            or User.objects.normalize_email(address.email) != user.email
        ):
            return render_completion(
                request, error=_("Email records are inconsistent. Contact an administrator.")
            )
        claimed = not address.verified and claim_account_verification(address)
    if claimed:
        send_claimed_account_verification(request, address)
    return redirect("account_complete")


@login_required
@require_http_methods(["GET", "POST"])
@never_cache
def phone_management_disabled(request):
    return redirect("account_complete")
