from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings
from django.utils import translation

from identity.models import AccountStatus


class CoreAccountAdapter(DefaultAccountAdapter):
    def populate_username(self, request, user) -> None:
        return None

    def save_user(self, request, user, form, commit=True):
        user = super().save_user(request, user, form, commit=False)
        user.email = user.email.strip().lower()
        user.status = AccountStatus.PENDING_VERIFICATION
        user.is_active = True
        if commit:
            user.save()
        return user

    def get_login_redirect_url(self, request) -> str:
        return "/account"

    def get_signup_redirect_url(self, request) -> str:
        return "/accounts/confirm-email/"

    def send_mail(self, template_prefix, email, context):
        user = context.get("user")
        language = getattr(user, "locale", None)
        supported_languages = {code for code, _name in settings.LANGUAGES}
        if language not in supported_languages:
            language = translation.get_language()
        with translation.override(language):
            return super().send_mail(template_prefix, email, context)
