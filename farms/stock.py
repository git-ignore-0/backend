from django.db.models import BigIntegerField, Case, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Cast, Coalesce, Greatest
from django.utils import timezone

from commerce.models import StoreStockMovement, StoreStockMovementKind


def stock_batch_status(batch, *, today=None):
    current_date = today or timezone.localdate()
    if batch.cancelled_at is not None:
        return "cancelled"
    if batch.start_date > current_date:
        return "upcoming"
    if batch.end_date < current_date:
        return "expired"
    return "active"


def is_historical_stock_batch(batch):
    source = batch.farm_product
    return source.removed_at is not None or batch.supply_generation != source.supply_generation


def is_current_stock_batch(batch, *, today=None):
    return (
        not is_historical_stock_batch(batch)
        and batch.farm_product.farm.is_active
        and stock_batch_status(batch, today=today) == "active"
    )


def current_stock_batch_q(*, today):
    return stock_batch_status_q("active", today=today) & Q(
        farm_product__farm__is_active=True,
        farm_product__removed_at__isnull=True,
        supply_generation=F("farm_product__supply_generation"),
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


def stock_movement_totals(movements):
    units = Cast("quantity", BigIntegerField())
    return (
        movements.order_by()
        .values("batch_id")
        .annotate(
            net_sold=Sum(
                Case(
                    When(kind=StoreStockMovementKind.SALE, then=units),
                    default=-units,
                    output_field=BigIntegerField(),
                )
            )
        )
    )


def batch_net_sold():
    movements = stock_movement_totals(StoreStockMovement.objects.filter(batch_id=OuterRef("pk")))
    return Coalesce(
        Subquery(movements.values("net_sold")), Value(0), output_field=BigIntegerField()
    )


def batch_remaining_units(net_sold):
    # The cast avoids unsigned subtraction on MySQL for legacy over-sold batches.
    return Greatest(
        Value(0),
        Cast("quantity_units", BigIntegerField()) - net_sold,
        output_field=BigIntegerField(),
    )


def product_stock_totals_query(batches):
    return (
        batches.order_by()
        .values("farm_product__product_id")
        .annotate(total=Sum(batch_remaining_units(batch_net_sold())))
    )
