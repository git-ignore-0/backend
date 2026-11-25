from time import time

from allauth.account import app_settings
from allauth.account.adapter import get_adapter
from allauth.account.fields import EmailField
from allauth.account.forms import ChangePasswordForm, LoginForm, SetPasswordForm, SignupForm
from allauth.core import context
from django import forms
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth.hashers import check_password
from django.core.validators import validate_email
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from identity.models import User
from identity.navigation import account_destination
from identity.passwords import set_authenticated_password
from identity.phones import normalize_login_phone, validate_local_login_phone
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY

PASSWORD_LOGIN_SESSION_KEY = "identity_password_login"


class LoginPhoneField(forms.CharField):
    def __init__(self, *, local_only=True, **kwargs):
        self.local_only = local_only
        kwargs.setdefault("label", _("Login phone number"))
        kwargs.setdefault(
            "widget",
            forms.TextInput(
                attrs={"type": "tel", "autocomplete": "tel", "data-phone-paste": "phone"}
            ),
        )
        kwargs["strip"] = False
        super().__init__(**kwargs)

    def clean(self, value):
        value = super().clean(value)
        if value:
            normalize = validate_local_login_phone if self.local_only else normalize_login_phone
            try:
                return normalize(value)
            except forms.ValidationError as error:
                raise forms.ValidationError(
                    _("Enter a valid Vietnamese phone number."), code="invalid_phone"
                ) from error
        return value


class CurrentPasswordForm(forms.Form):
    current_password = forms.CharField(
        label=_("Current password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class AddPhoneForm(CurrentPasswordForm):
    login_phone_number = LoginPhoneField(label=_("Phone number"))
    field_order = ["login_phone_number", "current_password"]


class CoreLoginForm(LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["login"].strip = False
        self.fields["login"].label = _("Phone number or email")
        self.fields["login"].widget.attrs.update(
            {
                "autocomplete": "username",
                "placeholder": _("Enter your phone number or email"),
                "data-phone-paste": "login",
            }
        )
        self.fields["remember"].label = _("Remember me")
        self.fields["password"].widget.attrs["placeholder"] = _("Enter your password")
        self.fields["password"].help_text = ""

    def clean_login(self):
        value = self.cleaned_data["login"]
        if "@" in value:
            value = value.strip()
            validate_email(value)
            return User.objects.normalize_email(value)
        return LoginPhoneField().clean(value)

    def login(self, request, redirect_url=None):
        redirect_url = account_destination(request, user=self.user, candidate=redirect_url)
        response = super().login(request, redirect_url=redirect_url)
        if request.user.is_authenticated:
            request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
            if self.cleaned_data.get("password") and request.user.pk == self.user.pk:
                request.session[PASSWORD_LOGIN_SESSION_KEY] = [str(request.user.pk), time()]
        return response


class NewPasswordHintsMixin:
    show_password_rules = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if password := self.fields.get("password1"):
            if self.show_password_rules:
                password.widget.attrs["placeholder"] = _("Password")
            else:
                password.widget.attrs["placeholder"] = _("At least 12 characters")
                password.help_text = ""
        if confirmation := self.fields.get("password2"):
            confirmation.widget.attrs["placeholder"] = _("Re-enter your password")


class AuthenticatedPasswordFormMixin(NewPasswordHintsMixin):
    def save(self):
        self.user = set_authenticated_password(
            user_id=self.user.pk,
            session_hash=context.request.session.get(HASH_SESSION_KEY, ""),
            password=self.cleaned_data["password1"],
            current_password=self.cleaned_data.get("oldpassword"),
        )


class CoreChangePasswordForm(AuthenticatedPasswordFormMixin, ChangePasswordForm):
    show_password_rules = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["oldpassword"].help_text = ""

    def clean_oldpassword(self):
        password = self.cleaned_data["oldpassword"]
        # User.check_password can save a hash upgrade using the pre-lock instance.
        if not check_password(password, self.user.password):
            raise get_adapter().validation_error("enter_current_password")
        return password


class CoreSetPasswordForm(AuthenticatedPasswordFormMixin, SetPasswordForm):
    pass


class CoreSignupForm(NewPasswordHintsMixin, SignupForm):
    show_password_rules = True

    email = EmailField(max_length=User._meta.get_field("email").max_length)
    name = forms.CharField(max_length=150, label=_("Full name"))
    referral_code = forms.CharField(max_length=12, required=False, label=_("Referral code"))

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        self.locked_referral_code = ""
        if request is not None:
            from referrals.services import is_referral_code_format, normalize_referral_code

            session_code = normalize_referral_code(
                request.session.get(SIGNUP_REFERRAL_SESSION_KEY, "")
            )
            if is_referral_code_format(session_code):
                self.locked_referral_code = session_code

        if self.locked_referral_code:
            if args and args[0] is not None:
                bound_data = args[0].copy()
                bound_data["referral_code"] = self.locked_referral_code
                args = (bound_data, *args[1:])
            elif kwargs.get("data") is not None:
                bound_data = kwargs["data"].copy()
                bound_data["referral_code"] = self.locked_referral_code
                kwargs["data"] = bound_data

        super().__init__(*args, **kwargs)
        self.fields["phone"] = LoginPhoneField(required=self.fields["phone"].required)
        self.fields["phone"].widget.attrs["placeholder"] = "0912345678"
        self.fields["name"].widget.attrs["placeholder"] = _("Your full name")
        self.fields["email"].widget.attrs["placeholder"] = "you@example.com"

        if self.locked_referral_code:
            field = self.fields["referral_code"]
            field.initial = self.locked_referral_code
            self.initial["referral_code"] = self.locked_referral_code
            field.widget.attrs["readonly"] = True
            field.help_text = _(
                "This referral code was applied from your invitation link and cannot be changed."
            )
        else:
            self.fields.pop("referral_code")

    def clean_email(self):
        email = User.objects.normalize_email(super().clean_email())
        return User._meta.get_field("email").clean(email, None)

    def validate_unique_email(self, value):
        if not app_settings.PREVENT_ENUMERATION:
            return super().validate_unique_email(value)
        from allauth.account.models import EmailAddress

        # Ordinary duplicate submissions receive the same public completion as
        # new submissions, without allauth's duplicate-account mail/SMS path.
        self.account_already_exists = (
            User.objects.filter(email__iexact=value).exists()
            or EmailAddress.objects.filter(email__iexact=value).exists()
        )
        return value

    def clean_referral_code(self):
        from referrals.services import referral_code_exists

        code = self.locked_referral_code
        if code and not referral_code_exists(code):
            raise forms.ValidationError(_("Enter a valid referral code."))
        return code

    @transaction.atomic
    def save(self, request):
        user = super().save(request)
        referral_code = self.cleaned_data.get("referral_code")
        if referral_code:
            from referrals.services import assign_referrer

            assign_referrer(referee=user, code=referral_code)
        if self.locked_referral_code:
            request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
        return user
