from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from identity.lifecycle import (
    SUPPORTED_LIFECYCLE_EVENTS,
    LifecycleDeliveryError,
    deliver_identity_event,
)
from identity.models import EventStatus, IdentityEvent

PROCESSING_LEASE = timedelta(minutes=10)
MAX_RETRY_DELAY_SECONDS = 3600


def claim_event(event_id) -> IdentityEvent | None:
    now = timezone.now()
    with transaction.atomic():
        event = IdentityEvent.objects.select_for_update().get(pk=event_id)
        is_available = event.available_at is None or event.available_at <= now
        if (
            event.status not in {EventStatus.PENDING, EventStatus.FAILED}
            or not is_available
            or event.attempts >= settings.FLARUM_LIFECYCLE_MAX_ATTEMPTS
        ):
            return None

        event.status = EventStatus.PROCESSING
        event.attempts += 1
        event.available_at = None
        event.save(update_fields=["status", "attempts", "available_at", "updated_at"])
        return event


def record_success(event_id) -> None:
    IdentityEvent.objects.filter(pk=event_id, status=EventStatus.PROCESSING).update(
        status=EventStatus.PROCESSED,
        available_at=None,
        processed_at=timezone.now(),
        updated_at=timezone.now(),
    )


def record_failure(event_id) -> None:
    event = IdentityEvent.objects.get(pk=event_id)
    retry_delay = min(60 * (2 ** max(event.attempts - 1, 0)), MAX_RETRY_DELAY_SECONDS)
    event.status = EventStatus.FAILED
    event.processed_at = None
    event.available_at = (
        timezone.now() + timedelta(seconds=retry_delay)
        if event.attempts < settings.FLARUM_LIFECYCLE_MAX_ATTEMPTS
        else None
    )
    event.save(update_fields=["status", "processed_at", "available_at", "updated_at"])


class Command(BaseCommand):
    help = "Deliver pending Core identity lifecycle events to Flarum."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25)

    def handle(self, *args, **options):
        limit = options["limit"]
        if limit < 1 or limit > 250:
            raise CommandError("--limit must be between 1 and 250")
        if not settings.FLARUM_LIFECYCLE_URL:
            raise CommandError("FLARUM_LIFECYCLE_URL is not configured")
        if len(settings.FLARUM_LIFECYCLE_SECRET) < 32:
            raise CommandError("FLARUM_LIFECYCLE_SECRET must contain at least 32 characters")

        now = timezone.now()
        IdentityEvent.objects.filter(
            type__in=SUPPORTED_LIFECYCLE_EVENTS,
            status=EventStatus.PROCESSING,
            updated_at__lt=now - PROCESSING_LEASE,
            attempts__lt=settings.FLARUM_LIFECYCLE_MAX_ATTEMPTS,
        ).update(status=EventStatus.FAILED, available_at=now, updated_at=now)

        event_ids = list(
            IdentityEvent.objects.filter(
                Q(available_at__isnull=True) | Q(available_at__lte=now),
                type__in=SUPPORTED_LIFECYCLE_EVENTS,
                status__in=[EventStatus.PENDING, EventStatus.FAILED],
                attempts__lt=settings.FLARUM_LIFECYCLE_MAX_ATTEMPTS,
            )
            .order_by("created_at")
            .values_list("pk", flat=True)[:limit]
        )

        delivered = 0
        failed = 0
        for event_id in event_ids:
            event = claim_event(event_id)
            if event is None:
                continue
            try:
                deliver_identity_event(event)
            except LifecycleDeliveryError as error:
                record_failure(event.pk)
                failed += 1
                self.stderr.write(self.style.WARNING(f"{event.pk}: {error}"))
            else:
                record_success(event.pk)
                delivered += 1

        self.stdout.write(f"Delivered {delivered} lifecycle event(s); {failed} failed.")
