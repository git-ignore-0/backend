from django.db.models import Q
from django.utils import timezone


def stock_batch_status(batch, *, today=None):
    current_date = today or timezone.localdate()
    if batch.cancelled_at is not None:
        return "cancelled"
    if batch.start_date > current_date:
        return "upcoming"
    if batch.end_date < current_date:
        return "expired"
    return "active"


def active_stock_batch_q(path="stock_batches", *, today=None):
    current_date = today or timezone.localdate()
    prefix = f"{path}__" if path else ""
    return Q(
        **{
            f"{prefix}cancelled_at__isnull": True,
            f"{prefix}start_date__lte": current_date,
            f"{prefix}end_date__gte": current_date,
        }
    )


def stock_batch_status_q(status, *, today=None):
    current_date = today or timezone.localdate()
    if status == "cancelled":
        return Q(cancelled_at__isnull=False)
    active = Q(cancelled_at__isnull=True)
    if status == "upcoming":
        return active & Q(start_date__gt=current_date)
    if status == "expired":
        return active & Q(end_date__lt=current_date)
    if status == "active":
        return active & Q(start_date__lte=current_date, end_date__gte=current_date)
    raise ValueError(f"Unknown stock batch status: {status}")
