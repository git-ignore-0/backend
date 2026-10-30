from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from identity.models import AuditLog
from memberships.models import (
    OPEN_CSA_PURCHASE_REQUEST_STATUSES,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
)

DEFAULT_EXPIRY_SECONDS = 7 * 24 * 60 * 60
MIN_EXPIRY_SECONDS = 5 * 60
MAX_EXPIRY_SECONDS = 30 * 24 * 60 * 60
DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 1000
EXPIRED_REASON = "request_expiry_elapsed"


def purchase_request_expiry_seconds() -> int:
    configured = int(
        getattr(settings, "CSA_PURCHASE_REQUEST_EXPIRY_SECONDS", DEFAULT_EXPIRY_SECONDS)
    )
    return max(MIN_EXPIRY_SECONDS, min(configured, MAX_EXPIRY_SECONDS))


def purchase_request_expires_at(*, now=None):
    return (now or timezone.now()) + timedelta(seconds=purchase_request_expiry_seconds())


def _expire_locked_request(item: CSAPurchaseRequest, *, now) -> bool:
    if (
        item.status not in OPEN_CSA_PURCHASE_REQUEST_STATUSES
        or item.expires_at is None
        or item.expires_at > now
    ):
        return False
    item.status = CSAPurchaseRequestStatus.EXPIRED
    item.expired_at = now
    item.expired_reason = EXPIRED_REASON
    item.customer = None
    item.membership = None
    item.approved_at = None
    item.approved_by = None
    item.rejected_at = None
    item.rejected_by = None
    item.rejection_reason = ""
    item.save(
        update_fields=[
            "status",
            "expired_at",
            "expired_reason",
            "customer",
            "membership",
            "approved_at",
            "approved_by",
            "rejected_at",
            "rejected_by",
            "rejection_reason",
            "updated_at",
        ]
    )
    AuditLog.objects.create(
        user=None,
        action="csa_purchase_request.expired",
        context={
            "request_id": str(item.pk),
            "expired_at": item.expired_at.isoformat(),
            "expired_reason": item.expired_reason,
        },
    )
    return True


@transaction.atomic
def expire_csa_purchase_request_if_due(*, request_id, now=None) -> bool:
    now = now or timezone.now()
    item = CSAPurchaseRequest.objects.select_for_update().filter(pk=request_id).first()
    return bool(item and _expire_locked_request(item, now=now))


@transaction.atomic
def expire_open_csa_purchase_request_for_phone(*, phone_e164, now=None) -> bool:
    now = now or timezone.now()
    item = (
        CSAPurchaseRequest.objects.select_for_update()
        .filter(
            phone_e164=phone_e164,
            status__in=OPEN_CSA_PURCHASE_REQUEST_STATUSES,
            expires_at__isnull=False,
            expires_at__lte=now,
        )
        .first()
    )
    return bool(item and _expire_locked_request(item, now=now))


@transaction.atomic
def expire_open_csa_purchase_requests_for_user(*, user_id, now=None) -> int:
    now = now or timezone.now()
    items = (
        CSAPurchaseRequest.objects.select_for_update()
        .filter(
            user_id=user_id,
            status__in=OPEN_CSA_PURCHASE_REQUEST_STATUSES,
            expires_at__isnull=False,
            expires_at__lte=now,
        )
        .order_by("expires_at", "id")
    )
    return sum(int(_expire_locked_request(item, now=now)) for item in items)


def expire_csa_purchase_requests(*, now=None, batch_size=DEFAULT_BATCH_SIZE, dry_run=False) -> int:
    now = now or timezone.now()
    batch_size = int(batch_size)
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    eligible = CSAPurchaseRequest.objects.filter(
        status__in=OPEN_CSA_PURCHASE_REQUEST_STATUSES,
        expires_at__isnull=False,
        expires_at__lte=now,
    )
    if dry_run:
        return eligible.count()

    expired_count = 0
    while True:
        with transaction.atomic():
            ids = list(
                eligible.order_by("expires_at", "id").values_list("pk", flat=True)[:batch_size]
            )
            if not ids:
                break
            items = list(
                CSAPurchaseRequest.objects.select_for_update()
                .filter(
                    pk__in=ids,
                    status__in=OPEN_CSA_PURCHASE_REQUEST_STATUSES,
                    expires_at__isnull=False,
                    expires_at__lte=now,
                )
                .order_by("expires_at", "id")
            )
            if not items:
                continue
            for item in items:
                expired_count += int(_expire_locked_request(item, now=now))
    return expired_count
