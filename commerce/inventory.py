"""Stock and effective demand, grouped by product and fulfillment Farm."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.db.models import Case, Count, DateField, F, IntegerField, Min, Q, Sum, Value, When
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


def _effective_inventory_holds(product_ids, *, now):
    from memberships.csa_rounds import effective_stock_hold_rows

    return (
        effective_store_stock_reservations(now=now).filter(product_id__in=product_ids),
        effective_stock_hold_rows(now=now).filter(product_id__in=product_ids),
    )


def _pack_holds(quantities, free_by_farm):
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
    return free, None


def _partition_assigned(physical, assigned):
    free = physical.copy()
    stranded = Counter()
    deficient_farms = set()
    for farm, quantities in assigned.items():
        required = sum(quantity * count for quantity, count in quantities.items())
        if required > physical.get(farm, 0):
            # At a deficient Farm there is no business priority for individual holds.
            stranded.update(quantities)
            deficient_farms.add(farm)
        else:
            free[farm] -= required
    return free, stranded, deficient_farms


@dataclass(frozen=True)
class InventoryCapacity:
    physical: FarmStock
    assigned: FarmStock
    free: FarmStock
    recovery_shortfalls: dict[UUID, tuple[int, int]]
    legacy_hold_counts: dict[UUID, int]
    stranded_hold_counts: dict[UUID, int]
    total_demand: dict[UUID, int]
    farms_by_product: dict[UUID, list[UUID]]

    def remaining(self, product_id, farm_id):
        return self.free.get((product_id, farm_id), 0)

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
    recover_stranded=False,
    exclude_farm_id=None,
    exclude_source_id=None,
    stock_adjustments=None,
):
    """Read bounded capacity. Writers call while holding the sorted Product mutexes."""
    product_ids = tuple(product_ids)
    physical = {}
    earliest_end = {}
    assigned = defaultdict(int)
    assigned_sizes = defaultdict(lambda: defaultdict(Counter))
    own_holds = defaultdict(lambda: defaultdict(Counter))
    legacy_quantities = defaultdict(Counter)
    total_demand = defaultdict(int)
    if product_ids:
        batches = active_stock_batches(now=now).filter(farm_product__product_id__in=product_ids)
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
                hold_size=Value(None, output_field=IntegerField()),
                is_own=Value(0, output_field=IntegerField()),
                row_count=Value(None, output_field=IntegerField()),
            )
            .values_list(
                "product_id", "farm_id", "first_expiry", "total", "hold_size", "row_count", "is_own"
            )
        )
        store, csa = _effective_inventory_holds(product_ids, now=now)

        def grouped(rows, owns_hold):
            # Group equal-sized whole holds in SQL. Keep own demand in the same
            # snapshot so recovery can distinguish shortage from stock left free.
            return (
                rows.order_by()
                .annotate(
                    hold_size=F("quantity"),
                    is_own=Case(When(owns_hold, then=Value(1)), default=Value(0))
                    if owns_hold is not None
                    else Value(0, output_field=IntegerField()),
                )
                .values("product_id", "farm_id", "hold_size", "is_own")
                .annotate(
                    first_expiry=Value(None, output_field=DateField()),
                    total=Sum("quantity"),
                    row_count=Count("pk"),
                )
                .values_list(
                    "product_id",
                    "farm_id",
                    "first_expiry",
                    "total",
                    "hold_size",
                    "row_count",
                    "is_own",
                )
            )

        owns_store = Q(line__order_id=exclude_order.pk) if exclude_order is not None else None
        if exclude_selection is not None:
            owns_csa = Q(selection_id=exclude_selection.pk)
        elif exclude_order is not None:
            owns_csa = Q(selection__order_id=exclude_order.pk)
        else:
            owns_csa = None

        # One statement keeps physical stock and demand on the same read snapshot.
        for product_id, farm_id, expiry, units, size, count, is_own in stock.union(
            grouped(store, owns_store), grouped(csa, owns_csa), all=True
        ):
            if expiry is not None:
                physical[product_id, farm_id] = int(units)
                earliest_end[product_id, farm_id] = expiry
            elif is_own:
                if recover_stranded or exclude_selection is not None:
                    own_holds[product_id][farm_id][size] += count
            else:
                total_demand[product_id] += int(units)
                if farm_id is None:
                    legacy_quantities[product_id][size] += count
                else:
                    assigned[product_id, farm_id] += int(units)
                    assigned_sizes[product_id][farm_id][size] += count

    for pair, delta in (stock_adjustments or {}).items():
        physical[pair] = max(0, physical.get(pair, 0) + delta)

    farms_by_product = defaultdict(list)
    for product_id, farm_id in sorted(
        physical, key=lambda pair: (earliest_end.get(pair, date.max), pair[1])
    ):
        farms_by_product[product_id].append(farm_id)

    free = {}
    shortfalls = {}
    stranded_counts = {}
    for product_id in product_ids:
        farm_stock = {
            farm: physical[product_id, farm] for farm in farms_by_product.get(product_id, ())
        }
        assigned_holds = assigned_sizes.get(product_id, {})
        legacy = legacy_quantities.get(product_id, Counter())
        unreserved, stranded, deficient_farms = _partition_assigned(farm_stock, assigned_holds)
        if stranded:
            stranded_counts[product_id] = stranded.total()
        plan, shortfall = _pack_holds(legacy + stranded, unreserved)
        if shortfall:
            shortfalls[product_id] = shortfall

        # A deficient Farm may contribute to the placement but should not take a new assignment.
        plan = {farm: units for farm, units in plan.items() if farm not in deficient_farms}
        own = own_holds.get(product_id)
        if own:
            _add_recovery_options(
                plan, own, farm_stock, assigned_holds, legacy, for_approval=recover_stranded
            )
        free.update({(product_id, farm): units for farm, units in plan.items()})

    return InventoryCapacity(
        physical,
        dict(assigned),
        free,
        shortfalls,
        {product_id: quantities.total() for product_id, quantities in legacy_quantities.items()},
        stranded_counts,
        dict(total_demand),
        dict(farms_by_product),
    )


def _add_recovery_options(plan, own, physical, assigned, legacy, *, for_approval):
    all_assigned = assigned | {
        farm: assigned.get(farm, Counter()) + quantities
        for farm, quantities in own.items()
        if farm is not None
    }
    before_free, before_stranded, before_deficient = _partition_assigned(physical, all_assigned)
    _, before_shortfall = _pack_holds(
        legacy + before_stranded + own.get(None, Counter()), before_free
    )
    if before_shortfall and not for_approval:
        return

    unreserved, stranded, deficient_farms = _partition_assigned(physical, assigned)
    for farm, quantities in own.items():
        quantity = sum(size * count for size, count in quantities.items())
        if farm is not None and farm not in before_deficient:
            # Consuming a healthy own hold leaves the same stock for everyone else.
            plan[farm] = max(plan.get(farm, 0), quantity)
        elif before_shortfall is None and for_approval:
            for target, units in physical.items():
                if units < quantity or plan.get(target, 0) >= quantity:
                    continue
                # Keep holds that were covered before excluding this order pinned to their Farm.
                if target not in before_deficient and unreserved[target] < quantity:
                    continue
                # Selling can strand a hold that only looked covered after exclusion.
                # Reclassify the remaining holds using the stock left by this sale.
                after_stock = physical | {target: units - quantity}
                after_free, after_stranded, _ = _partition_assigned(after_stock, assigned)
                _, shortfall = _pack_holds(legacy + after_stranded, after_free)
                if shortfall is None:
                    plan[target] = quantity
        elif max(plan.values(), default=0) >= quantity:
            continue
        elif before_shortfall is None:
            # CSA replaces a hold. The new assignment must fit alongside the current holds.
            remaining_demand = legacy + stranded
            for target, units in unreserved.items():
                if units < quantity or target in deficient_farms:
                    continue
                _, shortfall = _pack_holds(
                    remaining_demand, unreserved | {target: units - quantity}
                )
                if shortfall is None:
                    plan[target] = quantity
        elif farm is not None:
            # Admin can recover this whole hold in an already unverifiable product.
            # Removing this hold can leave enough stock at its Farm for other assigned holds.
            # Keep those holds and legacy holds protected before offering a recovery source.
            legacy_plan, _ = _pack_holds(legacy, unreserved)
            for target, units in legacy_plan.items():
                if units >= quantity:
                    plan[target] = units
