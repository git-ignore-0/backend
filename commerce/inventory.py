"""Stock and effective demand, grouped by product and fulfillment Farm."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.db.models import Case, Count, DateField, F, IntegerField, Min, Sum, Value, When
from django.utils import timezone

from commerce.models import StoreOrderStatus, StoreStockReservation
from farms.models import FarmProductStockBatch
from farms.stock import batch_net_sold, batch_remaining_units, current_stock_batch_q

WAITING_STATES = {
    StoreOrderStatus.PENDING_REVIEW,
    StoreOrderStatus.PAYMENT_PENDING,
    StoreOrderStatus.PAYMENT_REVIEW,
}

FarmStock = dict[tuple[UUID, UUID], int]


def active_stock_batches(*, now=None):
    return FarmProductStockBatch.objects.filter(
        current_stock_batch_q(today=timezone.localdate(now))
    )


def effective_store_stock_reservations(*, now):
    # The CSA selection owns stock for its linked order, including during reselection.
    return StoreStockReservation.objects.filter(
        released_at__isnull=True,
        line__order__status__in=WAITING_STATES,
        line__order__expires_at__gt=now,
        line__order__csa_selection__isnull=True,
    )


def _effective_inventory_holds(product_ids, *, now, exclude_order=None, exclude_selection=None):
    from memberships.csa_rounds import effective_stock_hold_rows

    store = effective_store_stock_reservations(now=now).filter(product_id__in=product_ids)
    csa = effective_stock_hold_rows(now=now, exclude_selection=exclude_selection).filter(
        product_id__in=product_ids
    )
    if exclude_order is not None:
        store = store.exclude(line__order_id=exclude_order.pk)
        csa = csa.exclude(selection__order_id=exclude_order.pk)
    return store, csa


def _pack_legacy_holds(quantities, free_by_farm):
    """Return a feasible placement or the first whole hold that could not be placed."""
    free = free_by_farm.copy()
    farms = sorted(free)
    for quantity, count in sorted(quantities.items(), reverse=True):
        # Fixed order keeps the same placement when a new assigned hold uses
        # only the capacity left free by this plan.
        for farm_id in farms:
            fits = min(count, max(0, free[farm_id]) // quantity)
            free[farm_id] -= fits * quantity
            count -= fits
            if not count:
                break
        if count:
            return {}, (quantity, max(0, max(free.values(), default=0)))
    return {farm: before - free[farm] for farm, before in free_by_farm.items()}, None


@dataclass(frozen=True)
class InventoryCapacity:
    physical: FarmStock
    assigned: FarmStock
    demand: FarmStock
    legacy: set[UUID]
    legacy_shortfalls: dict[UUID, tuple[int, int]]
    legacy_hold_counts: dict[UUID, int]
    total_demand: dict[UUID, int]
    farms_by_product: dict[UUID, list[UUID]]

    def remaining(self, product_id, farm_id):
        if product_id in self.legacy_shortfalls:
            return 0
        pair = (product_id, farm_id)
        return max(0, self.physical.get(pair, 0) - self.demand.get(pair, 0))

    def maximum(self, product_id):
        return max(
            (
                self.remaining(product_id, farm)
                for farm in self.farms_by_product.get(product_id, ())
            ),
            default=0,
        )

    def choose_farm(self, product_id, quantity, preferred_farm_id=None):
        if preferred_farm_id and self.remaining(product_id, preferred_farm_id) >= quantity:
            return preferred_farm_id
        return next(
            (
                farm
                for farm in self.farms_by_product.get(product_id, ())
                if self.remaining(product_id, farm) >= quantity
            ),
            None,
        )


def inventory_capacity(
    product_ids,
    *,
    now,
    exclude_order=None,
    exclude_selection=None,
    exclude_batch_id=None,
    exclude_farm_id=None,
    exclude_source_id=None,
    stock_adjustments=None,
):
    """Read bounded capacity. Writers call while holding the sorted Product mutexes."""
    product_ids = tuple(product_ids)
    physical = {}
    earliest_end = {}
    assigned = defaultdict(int)
    legacy = set()
    legacy_quantities = defaultdict(Counter)
    total_demand = defaultdict(int)
    if product_ids:
        batches = active_stock_batches(now=now).filter(farm_product__product_id__in=product_ids)
        if exclude_batch_id is not None:
            batches = batches.exclude(pk=exclude_batch_id)
        if exclude_farm_id is not None:
            batches = batches.exclude(farm_product__farm_id=exclude_farm_id)
        if exclude_source_id is not None:
            batches = batches.exclude(farm_product_id=exclude_source_id)
        stock = (
            batches.order_by()
            .annotate(
                remaining=batch_remaining_units(batch_net_sold()),
                product_id=F("farm_product__product_id"),
                farm_id=F("farm_product__farm_id"),
            )
            .filter(remaining__gt=0)
            .values("product_id", "farm_id")
            .annotate(
                first_expiry=Min("end_date"),
                total=Sum("remaining"),
                legacy_size=Value(None, output_field=IntegerField()),
                row_count=Value(None, output_field=IntegerField()),
            )
            .values_list(
                "product_id", "farm_id", "first_expiry", "total", "legacy_size", "row_count"
            )
        )
        store, csa = _effective_inventory_holds(
            product_ids, now=now, exclude_order=exclude_order, exclude_selection=exclude_selection
        )

        def grouped(rows):
            # For assigned holds, group by Farm. For legacy holds retain the size of
            # each indivisible demand, aggregated by equal quantity.
            return (
                rows.order_by()
                .annotate(
                    legacy_size=Case(
                        When(farm_id__isnull=True, then=F("quantity")),
                        output_field=IntegerField(),
                    )
                )
                .values("product_id", "farm_id", "legacy_size")
                .annotate(
                    first_expiry=Value(None, output_field=DateField()),
                    total=Sum("quantity"),
                    row_count=Count("pk"),
                )
                .values_list(
                    "product_id", "farm_id", "first_expiry", "total", "legacy_size", "row_count"
                )
            )

        # One statement keeps physical stock and demand on the same read snapshot.
        for product_id, farm_id, expiry, units, size, count in stock.union(
            grouped(store), grouped(csa), all=True
        ):
            if expiry is not None:
                physical[product_id, farm_id] = int(units)
                earliest_end[product_id, farm_id] = expiry
                continue
            total_demand[product_id] += int(units)
            if farm_id is None:
                legacy.add(product_id)
                legacy_quantities[product_id][size] += count
            else:
                assigned[product_id, farm_id] += int(units)

    for pair, delta in (stock_adjustments or {}).items():
        physical[pair] = max(0, physical.get(pair, 0) + delta)

    farms_by_product = defaultdict(list)
    for product_id, farm_id in sorted(
        physical, key=lambda pair: (earliest_end.get(pair, date.max), pair[1])
    ):
        farms_by_product[product_id].append(farm_id)

    demand = dict(assigned)
    shortfalls = {}
    for product_id, quantities in legacy_quantities.items():
        free_by_farm = {
            farm: max(0, physical[product_id, farm] - assigned.get((product_id, farm), 0))
            for farm in farms_by_product.get(product_id, ())
        }
        placement, shortfall = _pack_legacy_holds(quantities, free_by_farm)
        if shortfall:
            shortfalls[product_id] = shortfall
            continue
        for farm_id, units in placement.items():
            if units:
                pair = (product_id, farm_id)
                demand[pair] = demand.get(pair, 0) + units

    return InventoryCapacity(
        physical,
        dict(assigned),
        demand,
        legacy,
        shortfalls,
        {product_id: quantities.total() for product_id, quantities in legacy_quantities.items()},
        dict(total_demand),
        dict(farms_by_product),
    )
