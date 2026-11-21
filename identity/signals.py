from allauth.account.signals import email_confirmed
from django.conf import settings
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from identity.models import AuditLog
from identity.services import activate_account


def request_metadata(request) -> dict[str, str | None]:
    if request is None:
        return {"ip_address": None, "user_agent": ""}
    return {
        "ip_address": request.META.get("REMOTE_ADDR"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:1000],
    }


@receiver(email_confirmed)
def activate_verified_identity(request, email_address, **kwargs) -> None:
    activate_account(user=email_address.user, source="email_verified")


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs) -> None:
    if request is not None and settings.LANGUAGE_COOKIE_NAME in request.COOKIES:
        language = request.LANGUAGE_CODE
        if language in {code for code, _name in settings.LANGUAGES} and user.locale != language:
            user.locale = language
            user.save(update_fields=["locale", "updated_at"])
    AuditLog.objects.create(user=user, action="auth.login", **request_metadata(request))


@receiver(user_logged_out)
def record_logout(sender, request, user, **kwargs) -> None:
    if user is not None and getattr(user, "is_authenticated", False):
        AuditLog.objects.create(user=user, action="auth.logout", **request_metadata(request))
