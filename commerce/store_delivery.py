"""Vietnam delivery dates and per-owner CSA companion shipping eligibility."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone

from commerce.models import StoreOrder
from memberships.models import CSARound, CSASelection

VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")


def validate_delivery_date(value, *, now=None):
    from commerce.store_orders import StoreOrderError

    today = (now or timezone.now()).astimezone(VIETNAM).date()
    if value is None or not today <= value <= today + timedelta(days=7):
        raise StoreOrderError("store_delivery_date_invalid")
    return value


def _selections(user, first, last):
    start = datetime.combine(first, time.min, tzinfo=VIETNAM)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=VIETNAM)
    return CSASelection.objects.filter(
        membership__customer__user=user,
        order__isnull=False,
        round__delivery_at__gte=start,
        round__delivery_at__lt=end,
    )


def lock_companion_sources(*, user, delivery_date):
    """Before Product locks: match CSA Round -> StoreOrder -> Product order.

    Lock even disabled/cancelled candidates, so admin opening/closing and order
    cancellation serialize with checkout. Re-read eligibility after these waits.
    A new selection/order requires the same Round lock.
    """
    if not user or not user.is_authenticated:
        return
    sources = _selections(user, delivery_date, delivery_date)
    round_ids = list(sources.values_list("round_id", flat=True))
    list(CSARound.objects.select_for_update().filter(pk__in=round_ids).order_by("pk"))
    order_ids = list(sources.values_list("order_id", flat=True))
    list(StoreOrder.objects.select_for_update().filter(pk__in=order_ids).order_by("pk"))


def companion_benefits(*, user, first, last, now=None):
    """One batched query across all dates; no admin settings in public projections."""
    if not user or not user.is_authenticated:
        return {}
    now = now or timezone.now()
    rows = (
        _selections(user, first, last)
        .filter(
            round__published_at__isnull=False,
            round__cancelled_at__isnull=True,
            round__companion_delivery_enabled=True,
            round__companion_delivery_cutoff__gt=now,
        )
        .exclude(state="cancelled")
        .exclude(order__status="cancelled")
        .exclude(Q(state="reselecting") & Q(reopen_until__lte=now))
        .values("id", "order_id", "round_id", "round__delivery_at")
        .order_by("round__delivery_at", "round_id", "id")
    )
    benefits = {}
    for row in rows:
        day = row["round__delivery_at"].astimezone(VIETNAM).date()
        benefits.setdefault(
            day,
            {
                "reason": "csa_companion_delivery",
                "round_id": str(row["round_id"]),
                "selection_id": str(row["id"]),
                "csa_order_id": str(row["order_id"]),
                "delivery_at": row["round__delivery_at"].isoformat(),
            },
        )
    return benefits


def delivery_calendar(*, user):
    from commerce.store_orders import _active_membership_for_user

    _active_membership_for_user(user)  # Preserve the frozen-only Store restriction.
    now = timezone.now()
    today = now.astimezone(VIETNAM).date()
    benefits = companion_benefits(user=user, first=today, last=today + timedelta(days=7), now=now)
    return {
        "server_time": now.isoformat(),
        "dates": [
            {
                "delivery_date": (today + timedelta(days=i)).isoformat(),
                "free_shipping": today + timedelta(days=i) in benefits,
            }
            for i in range(8)
        ],
    }
