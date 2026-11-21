"""Handle delivery failures without changing allauth's verification flow."""

from datetime import timedelta
from math import ceil

from allauth.account.internal.flows.email_verification import send_verification_email_to_address
from django.conf import settings
from django.contrib import messages
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from identity.mail import deliver_account_email
from identity.models import User


def verification_resend_seconds(user):
    attempted_at = user.email_verification_attempted_at
    if attempted_at is None:
        return 0
    elapsed = (timezone.now() - attempted_at).total_seconds()
    return max(0, ceil(settings.EMAIL_VERIFICATION_RESEND_SECONDS - elapsed))


def claim_account_verification(address):
    now = timezone.now()
    cutoff = now - timedelta(seconds=settings.EMAIL_VERIFICATION_RESEND_SECONDS)
    return (
        User.objects.filter(pk=address.user_id, email=address.email, is_active=True)
        .filter(
            Q(email_verification_attempted_at__isnull=True)
            | Q(email_verification_attempted_at__lte=cutoff)
        )
        .update(email_verification_attempted_at=now)
    )


def send_claimed_account_verification(request, address, *, signup=False):
    delivered = deliver_account_email(
        "Account verification email transport failed.",
        send_verification_email_to_address,
        request,
        address,
        signup=signup,
    )
    if not delivered and not signup:
        # Signup must show the same message for new and duplicate identifiers.
        messages.warning(
            request, _("We could not send the verification email. Try sending it again later.")
        )


def send_account_verification(request, address, *, signup=False):
    if claim_account_verification(address):
        send_claimed_account_verification(request, address, signup=signup)
