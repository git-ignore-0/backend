from allauth.account.signals import email_confirmed
from django.contrib.auth.signals import user_logged_in, user_logged_out
from django.dispatch import receiver

from identity.models import AccountStatus, AuditLog
from identity.services import transition_account


def request_metadata(request) -> dict[str, str | None]:
    if request is None:
        return {"ip_address": None, "user_agent": ""}
    return {
        "ip_address": request.META.get("REMOTE_ADDR"),
        "user_agent": request.META.get("HTTP_USER_AGENT", "")[:1000],
    }


@receiver(email_confirmed)
def activate_verified_identity(request, email_address, **kwargs) -> None:
    user = email_address.user
    if user.status == AccountStatus.PENDING_VERIFICATION:
        transition_account(user, AccountStatus.ACTIVE)


@receiver(user_logged_in)
def record_login(sender, request, user, **kwargs) -> None:
    AuditLog.objects.create(user=user, action="auth.login", **request_metadata(request))


@receiver(user_logged_out)
def record_logout(sender, request, user, **kwargs) -> None:
    if user is not None and getattr(user, "is_authenticated", False):
        AuditLog.objects.create(user=user, action="auth.logout", **request_metadata(request))
