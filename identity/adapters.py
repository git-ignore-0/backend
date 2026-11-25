from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from identity.mail import deliver_account_email
from identity.models import AccountStatus, User
from identity.navigation import account_destination, pending_oauth_destination
from identity.phones import normalize_login_phone


class CoreAccountAdapter(DefaultAccountAdapter):
    def populate_username(self, request, user) -> None:
        return None

    def save_user(self, request, user, form, commit=True):
        user = super().save_user(request, user, form, commit=False)
        user.email = User.objects.normalize_email(user.email)
        user.name = form.cleaned_data["name"].strip()
        language = translation.get_language()
        user.locale = (
            language
            if language in {code for code, _ in settings.LANGUAGES}
            else settings.LANGUAGE_CODE
        )
        user.status = AccountStatus.PENDING_ACTIVATION
        user.is_active = True
        if commit:
            user.save()
        return user

    def add_message(
        self,
        request,
        level,
        message_template=None,
        message_context=None,
        extra_tags="",
        message=None,
    ):
        if message_template == "account/messages/email_confirmation_sent.txt" and (
            message_context or {}
        ).get("signup"):
            return
        return super().add_message(
            request,
            level,
            message_template=message_template,
            message_context=message_context,
            extra_tags=extra_tags,
            message=message,
        )

    @transaction.atomic
    def confirm_email(self, request, email_address):
        from allauth.account.models import EmailAddress

        user = User.objects.select_for_update().filter(pk=email_address.user_id).first()
        address = (
            EmailAddress.objects.select_for_update().filter(pk=email_address.pk, user=user).first()
        )
        if (
            user is None
            or address is None
            or not user.email
            or (User.objects.normalize_email(address.email) != user.email)
        ):
            self.add_message(
                request,
                messages.ERROR,
                message=_("Email records are inconsistent. Contact an administrator."),
            )
            return False
        address.user = user
        # Native allauth verification still emits BE01's unchanged activation signal.
        return super().confirm_email(request, address)

    def get_login_redirect_url(self, request) -> str:
        return account_destination(request)

    def get_email_verification_redirect_url(self, email_address):
        request = self.request
        if not request.user.is_authenticated:
            return reverse("account_login")
        if email_address.user_id == request.user.pk:
            user = User.objects.get(pk=request.user.pk)
            if (
                user.email == User.objects.normalize_email(email_address.email)
                and user.email_verified
            ):
                if destination := pending_oauth_destination(request, user=user):
                    return destination
        return account_destination(request)

    def phone_form_field(self, **kwargs):
        from identity.forms import LoginPhoneField

        # allauth calls this field again to classify an already-normalized login.
        return LoginPhoneField(local_only=False, **kwargs)

    def clean_phone(self, phone):
        return normalize_login_phone(phone)

    def get_phone(self, user):
        return (user.login_phone_number, False) if user.login_phone_number else None

    def set_phone(self, user, phone, verified):
        if verified:
            raise ValidationError("Phone verification is not supported.")
        phone = normalize_login_phone(phone)
        if user.login_phone_number and user.login_phone_number != phone:
            raise ValidationError("Changing an existing login phone is not supported.")
        # Called even by allauth.save_user(commit=False); leave persistence to it.
        user.login_phone_number = phone

    def get_user_by_phone(self, phone):
        try:
            phone = normalize_login_phone(phone)
        except ValidationError:
            return None
        return User.objects.filter(login_phone_number=phone).first()

    def get_login_stages(self):
        # Required signup phone must not turn into a legacy-user login requirement.
        return [
            stage
            for stage in super().get_login_stages()
            if stage != "allauth.account.stages.PhoneVerificationStage"
        ]

    def get_signup_redirect_url(self, request) -> str:
        return "/accounts/confirm-email/"

    def get_password_change_redirect_url(self, request) -> str:
        return reverse("account")

    def send_mail(self, template_prefix, email, context):
        user = context.get("user")
        language = getattr(user, "locale", None)
        supported_languages = {code for code, _name in settings.LANGUAGES}
        if language not in supported_languages:
            language = translation.get_language()
        with translation.override(language):
            if template_prefix == "account/email/password_reset":
                deliver_account_email(
                    "Password reset notification email transport failed.",
                    super().send_mail,
                    template_prefix,
                    email,
                    context,
                )
                return None
            return super().send_mail(template_prefix, email, context)
