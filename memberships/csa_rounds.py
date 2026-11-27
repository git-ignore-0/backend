"""CSA round lifecycle using shared Store product, identity, address and member locks."""

import logging
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from commerce.models import Product, StoreOrder, StoreOrderLine
from commerce.public_catalog import _public_product_filter
from commerce.store_orders import (
    _active_reserved_stock,
    _address_for_checkout,
    _lock_checkout_identity,
    _reference_code,
    _translation,
    active_product_stock_totals,
)
from memberships.models import (
    CSARound,
    CSARoundConfiguration,
    CSARoundScheduleLock,
    CSASelection,
    CSASelectionEvent,
    CSASelectionLine,
    Membership,
    MembershipCycleQuota,
    MembershipStatus,
    MembershipUsageAllocation,
    MembershipUsageLine,
    MembershipUsageOrder,
    MembershipUsageReversal,
)
from memberships.services import ensure_due_cycles, quota_availability_rows

logger = logging.getLogger(__name__)

ROUND_STATUSES = ("draft", "scheduled", "open", "closed", "delivery", "cancelled")
TERMINAL = ("cancelled", "delivered")
VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")
WEEK = timedelta(weeks=1)
DRAFT_LEAD_TIME = timedelta(days=1)
RECURRENCE_GENERATION_LIMIT = 8
RECURRENCE_SCAN_PAGE_SIZE = 100


def delivery_start(round_):
    """Boundary for deadlines, never an asserted delivery appointment."""
    return datetime.combine(round_.delivery_date, time.min, tzinfo=VIETNAM)


class CSARoundError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code):
    raise CSARoundError(code)


def _next_round_sequence():
    CSARoundScheduleLock.objects.get_or_create(pk=1)
    CSARoundScheduleLock.objects.select_for_update().get(pk=1)
    last = CSARound.objects.order_by("-sequence").values_list("sequence", flat=True).first()
    return (last or 0) + 1


def round_status(round_, now=None):
    now = now or timezone.now()
    if round_.cancelled_at:
        return "cancelled"
    if round_.published_at is None:
        return "draft"
    if now < round_.opens_at:
        return "scheduled"
    if now < round_.closes_at:
        return "open"
    if now.astimezone(VIETNAM).date() < round_.delivery_date:
        return "closed"
    return "delivery"


def filter_rounds_by_status(rounds, status, *, now):
    """Filter by the same lifecycle boundaries used in round_status."""
    if status == "cancelled":
        return rounds.filter(cancelled_at__isnull=False)

    rounds = rounds.filter(cancelled_at__isnull=True)
    if status == "draft":
        return rounds.filter(published_at__isnull=True)

    today = now.astimezone(VIETNAM).date()
    lifecycle = {
        "scheduled": Q(opens_at__gt=now),
        "open": Q(opens_at__lte=now, closes_at__gt=now),
        "closed": Q(closes_at__lte=now, delivery_date__gt=today),
        "delivery": Q(closes_at__lte=now, delivery_date__lte=today),
    }
    return rounds.filter(lifecycle[status], published_at__isnull=False)


def _validate_schedule(round_):
    if (
        type(round_.delivery_date) is not date
        or not round_.name.strip()
        or not round_.opens_at < round_.closes_at < delivery_start(round_)
    ):
        _fail("csa_round_schedule_invalid")


def _create_round(*, name, opens_at, closes_at, delivery_date, sequence, **fields):
    round_ = CSARound(
        name=name,
        opens_at=opens_at,
        closes_at=closes_at,
        delivery_date=delivery_date,
        sequence=sequence,
        **fields,
    )
    _validate_schedule(round_)
    round_.save()
    return round_


@transaction.atomic
def create_round_configuration(
    *, name, opens_at, closes_at, delivery_date, actor, recurrence_mode=None
):
    configuration = CSARoundConfiguration(
        name=name.strip(),
        opens_at=opens_at,
        closes_at=closes_at,
        delivery_date=delivery_date,
        recurrence_mode=recurrence_mode,
        created_by=actor,
    )
    _validate_schedule(configuration)
    configuration.save()
    return configuration


def _check_schedule_version(schedule, version):
    if version is not None and schedule.version != version:
        _fail("version_conflict")


def _draft_configuration_for_update(configuration_id, version):
    configuration = CSARoundConfiguration.objects.select_for_update().get(pk=configuration_id)
    _check_schedule_version(configuration, version)
    if configuration.status != "draft":
        _fail("csa_configuration_not_draft")
    return configuration


def _validate_draft_companion_cutoff(configuration):
    cutoff = configuration.draft_companion_delivery_cutoff
    if cutoff is not None and cutoff >= delivery_start(configuration):
        _fail("csa_companion_deadline_invalid")


@transaction.atomic
def update_round_configuration(*, configuration_id, version=None, **values):
    configuration = _draft_configuration_for_update(configuration_id, version)
    for field in ("name", "opens_at", "closes_at", "delivery_date", "recurrence_mode"):
        if field in values:
            setattr(configuration, field, values[field])
    _validate_schedule(configuration)
    _validate_draft_companion_cutoff(configuration)
    configuration.version += 1
    configuration.save()
    return configuration


@transaction.atomic
def delete_round_configuration(*, configuration_id, version=None):
    _draft_configuration_for_update(configuration_id, version).delete()


def can_edit_round(round_):
    """Published participation makes the round's dates immutable."""
    if round_.cancelled_at:
        return False
    if round_.published_at is None:
        return True
    has_selections = getattr(round_, "has_csa_selections", None)
    return not (round_.selections.exists() if has_selections is None else has_selections)


@transaction.atomic
def update_round(*, round_id, actor, version=None, **values):
    row = CSARound.objects.select_for_update().get(pk=round_id)
    _check_schedule_version(row, version)
    # Once public, dates define entitlement and reservations; create drafts freely.
    if not can_edit_round(row):
        _fail("csa_round_published_immutable")
    for key in ("name", "opens_at", "closes_at", "delivery_date"):
        if key in values:
            setattr(row, key, values[key])
    _validate_schedule(row)
    if row.companion_delivery_cutoff and row.companion_delivery_cutoff >= delivery_start(row):
        _fail("csa_companion_deadline_invalid")
    if row.published_at and row.closes_at <= timezone.now():
        _fail("csa_round_already_closed")
    row.version += 1
    row.save()
    return row


def _recurrence_due_at(configuration, week):
    opens_at = configuration.opens_at + WEEK * week
    if configuration.recurrence_mode == CSARoundConfiguration.Mode.DRAFT:
        return opens_at - DRAFT_LEAD_TIME
    return opens_at


def _last_recurrence_week(delivery_date):
    # The delivery date comes after both timestamps, so its bound also protects those shifts.
    return (date.max - delivery_date) // WEEK


@transaction.atomic
def publish_round_configuration(*, configuration_id, version=None):
    configuration = CSARoundConfiguration.objects.select_for_update().get(pk=configuration_id)
    _check_schedule_version(configuration, version)
    if configuration.cancelled_at is not None:
        _fail("csa_configuration_cancelled")
    if configuration.published_at is not None:
        return configuration
    _validate_schedule(configuration)
    _validate_draft_companion_cutoff(configuration)
    if configuration.recurrence_mode and _last_recurrence_week(configuration.delivery_date) < 1:
        _fail("csa_round_schedule_invalid")
    sequence = _next_round_sequence()
    now = timezone.now()
    if configuration.closes_at <= now:
        _fail("csa_round_already_closed")
    _create_round(
        configuration=configuration,
        recurrence_week=0,
        name=configuration.name,
        opens_at=configuration.opens_at,
        closes_at=configuration.closes_at,
        delivery_date=configuration.delivery_date,
        companion_delivery_enabled=configuration.draft_companion_delivery_enabled,
        companion_delivery_cutoff=configuration.draft_companion_delivery_cutoff,
        sequence=sequence,
        created_by_id=configuration.created_by_id,
        published_at=now,
    )
    configuration.published_at = now
    configuration.version += 1
    configuration.draft_companion_delivery_enabled = False
    configuration.draft_companion_delivery_cutoff = None
    if configuration.recurrence_mode:
        configuration.next_create_at = _recurrence_due_at(configuration, 1)
    configuration.save(
        update_fields=[
            "published_at",
            "version",
            "next_create_at",
            "draft_companion_delivery_enabled",
            "draft_companion_delivery_cutoff",
        ]
    )
    return configuration


@transaction.atomic
def publish_round(*, round_id, actor, version=None):
    row = CSARound.objects.select_for_update().get(pk=round_id)
    _check_schedule_version(row, version)
    if row.cancelled_at:
        _fail("csa_round_cancelled")
    if row.published_at:
        return row
    _validate_schedule(row)
    now = timezone.now()
    if row.closes_at <= now:
        _fail("csa_round_already_closed")
    row.published_at = now
    row.version += 1
    row.save(update_fields=["published_at", "version", "updated_at"])
    return row


@transaction.atomic
def stop_round_recurrence(*, configuration_id, version=None):
    configuration = CSARoundConfiguration.objects.select_for_update().get(pk=configuration_id)
    _check_schedule_version(configuration, version)
    if configuration.recurrence_mode is None or configuration.published_at is None:
        _fail("csa_recurrence_not_active")
    if configuration.cancelled_at is None:
        configuration.cancelled_at = timezone.now()
        configuration.next_create_at = None
        configuration.version += 1
        configuration.save(update_fields=["cancelled_at", "next_create_at", "version"])
    return configuration


@transaction.atomic
def configure_companion_delivery(*, round_id, actor, version=None, **values):
    row = CSARound.objects.select_for_update().get(pk=round_id)
    _check_schedule_version(row, version)
    now = timezone.now()  # Decide after waiting for the round lock.
    if row.cancelled_at:
        _fail("csa_round_cancelled")
    enabled = values.get("companion_delivery_enabled", row.companion_delivery_enabled)
    cutoff = values.get("companion_delivery_cutoff", row.companion_delivery_cutoff)
    if (enabled or "companion_delivery_cutoff" in values) and (
        cutoff is None or not now < cutoff < delivery_start(row)
    ):
        _fail("csa_companion_deadline_invalid")
    row.companion_delivery_enabled = enabled
    row.companion_delivery_cutoff = cutoff
    row.version += 1
    row.save(
        update_fields=[
            "companion_delivery_enabled",
            "companion_delivery_cutoff",
            "version",
            "updated_at",
        ]
    )
    return row


def _event(selection, action, actor=None, **context):
    CSASelectionEvent.objects.create(
        selection=selection, action=action, actor=actor, context=context
    )


def _credits(selection):
    if not selection or not selection.current_usage_id:
        return {}
    return {
        item["cycle_quota_id"]: item["total"]
        for item in MembershipUsageAllocation.objects.filter(
            line__order_id=selection.current_usage_id
        )
        .values("cycle_quota_id")
        .annotate(total=Sum("units"))
    }


def active_stock_holds(*, exclude_selection=None, now=None):
    rows = CSASelection.objects.exclude(state__in=TERMINAL).filter(round__cancelled_at__isnull=True)
    if exclude_selection:
        rows = rows.exclude(pk=exclude_selection.pk)
    # A late worker must never keep expired reselection drafts in stock availability.
    return rows.exclude(state="reselecting", reopen_until__lte=now or timezone.now())


def selection_held_lines(selection, *, now):
    """Return only lines backed by an effective stock hold on the same selection."""
    if selection is None:
        return {}
    holds = dict(
        selection.stock_holds.filter(selection__in=active_stock_holds(now=now)).values_list(
            "product_id", "quantity"
        )
    )
    return {
        line.product_id: (line, min(line.quantity, holds[line.product_id]))
        for line in selection.lines.filter(product_id__in=holds)
    }


def selectable_csa_products():
    return Product.objects.filter(
        _public_product_filter(), csa_quota_group__is_active=True, csa_quota_cost__gt=0
    )


def selection_stock_capacity(product_ids, selection, *, now=None):
    now = now or timezone.now()
    stock = active_product_stock_totals(product_ids, now=now)
    held = _active_reserved_stock(product_ids, exclude_selection=selection, now=now)
    return {pk: max(0, stock.get(pk, 0) - held.get(pk, 0)) for pk in product_ids}


def selection_quantity_limit(product, capacity, held_line, own_hold):
    if product is None or (
        held_line is not None
        and (
            uuid.UUID(held_line.snapshot["quota_group_id"]) != product.csa_quota_group_id
            or Decimal(held_line.snapshot["quota_cost"]) != product.csa_quota_cost
        )
    ):
        return own_hold
    return max(own_hold, capacity)


def effective_stock_hold_rows(*, now, exclude_selection=None):
    from memberships.models import CSAStockHold

    return CSAStockHold.objects.filter(
        selection__in=active_stock_holds(exclude_selection=exclude_selection, now=now),
    )


def reserved_stock(product_ids, *, exclude_selection=None, now=None):
    return dict(
        effective_stock_hold_rows(now=now or timezone.now(), exclude_selection=exclude_selection)
        .filter(product_id__in=product_ids)
        .values("product_id")
        .annotate(total=Sum("quantity"))
        .values_list("product_id", "total")
    )


def reserved_quota(quota_ids, *, exclude_selection=None):
    from memberships.models import CSAQuotaHold

    rows = list(
        CSAQuotaHold.objects.filter(
            selection__in=active_stock_holds(exclude_selection=exclude_selection),
            selection__state__in=["selecting", "reselecting"],
            cycle_quota_id__in=quota_ids,
        ).values("selection__current_usage_id", "cycle_quota_id", "units")
    )
    usage_ids = {row["selection__current_usage_id"] for row in rows} - {None}
    credits = {
        (row["line__order_id"], row["cycle_quota_id"]): row["total"]
        for row in MembershipUsageAllocation.objects.filter(
            line__order_id__in=usage_ids, cycle_quota_id__in=quota_ids
        )
        .values("line__order_id", "cycle_quota_id")
        .annotate(total=Sum("units"))
    }
    totals = defaultdict(Decimal)
    for hold in rows:
        credit = credits.get((hold["selection__current_usage_id"], hold["cycle_quota_id"]), 0)
        totals[hold["cycle_quota_id"]] += max(Decimal(0), hold["units"] - credit)
    return totals


def extension_date(membership):
    return membership.extension_end_date or membership.end_date - timedelta(days=1)


def preserve_expiring_quota(membership):
    """Only extend expiry for resources of a legitimate outstanding participation."""
    return (
        extension_date(membership) >= timezone.now().astimezone(VIETNAM).date()
        and membership.csa_selections.exclude(state__in=TERMINAL).exists()
    )


def _lock_resources(selection, product_ids):
    if selection and selection.order_id:
        StoreOrder.objects.select_for_update().get(pk=selection.order_id)
    ids = set(product_ids)
    if selection:
        ids.update(selection.lines.values_list("product_id", flat=True))
        if selection.order_id:
            ids.update(selection.order.lines.values_list("product_id", flat=True))
    return list(Product.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


def _entitled_membership(user, round_, existing, now):
    today = now.astimezone(VIETNAM).date()
    if existing:
        member = Membership.objects.select_for_update().get(pk=existing.membership_id)
        if member.status == MembershipStatus.REVOKED or today > extension_date(member):
            _fail("csa_membership_unavailable")
        return member
    member = (
        Membership.objects.select_for_update()
        .filter(
            customer__user=user,
            start_date__lte=today,
            end_date__gt=today,
            status__in=["active", "scheduled"],
        )
        .order_by("start_date", "id")
        .first()
    )
    if not member:
        _fail("csa_membership_unavailable")
    return member


def _quota_candidates(membership, selection, group_ids, *, lock=True):
    credits = _credits(selection)
    rows = quota_availability_rows(
        membership, product_ids=group_ids, lock=lock, exclude_csa_selection=selection
    )
    ids = [row[0].pk for row in rows]
    result = defaultdict(list)
    for quota, _, available, _, _ in rows:
        result[quota.product_snapshot.product_id].append(
            [quota, available + credits.get(quota.pk, 0)]
        )
    if not selection:
        return result
    # Current rows exclude our own hold. Older rows may only
    # reuse this selection's preserved hold/debit; never another order's balance.
    own = dict(selection.quota_holds.values_list("cycle_quota_id", "units"))
    extra = (
        (MembershipCycleQuota.objects.select_for_update() if lock else MembershipCycleQuota.objects)
        .filter(
            pk__in=set(own) | set(credits),
            cycle__membership=membership,
            product_snapshot__product_id__in=group_ids,
        )
        .exclude(pk__in=ids)
        .select_related("cycle", "product_snapshot")
        .order_by("pk")
    )
    extra = list(extra)
    extra_ids = [q.pk for q in extra]
    other_csa = reserved_quota(extra_ids, exclude_selection=selection)
    from commerce.models import StoreQuotaReservation

    store = dict(
        StoreQuotaReservation.objects.filter(
            cycle_quota_id__in=extra_ids,
            released_at__isnull=True,
            consumed_at__isnull=True,
            line__order__status__in=["payment_pending", "pending_review", "payment_review"],
            line__order__expires_at__gt=timezone.now(),
        )
        .values("cycle_quota_id")
        .annotate(total=Sum("units"))
        .values_list("cycle_quota_id", "total")
    )
    for quota in extra:
        available = max(
            Decimal(0), quota.remaining_units - store.get(quota.pk, 0) - other_csa.get(quota.pk, 0)
        )
        available = min(available, max(Decimal(0), own.get(quota.pk, 0) - credits.get(quota.pk, 0)))
        result[quota.product_snapshot.product_id].append(
            [quota, available + credits.get(quota.pk, 0)]
        )
    return result


def _validate_selection_window(round_, member, existing, now):
    today = now.astimezone(VIETNAM).date()
    if (existing and today > extension_date(member)) or (
        not existing and not member.start_date <= today < member.end_date
    ):
        _fail("csa_membership_unavailable")
    if existing and existing.state == "reselecting":
        if now >= existing.reopen_until:
            _fail("csa_reselection_expired")
    elif not (round_.opens_at <= now < round_.closes_at) or (
        existing and existing.state != "selecting"
    ):
        _fail("csa_selection_locked")


@transaction.atomic
def save_selection(*, round_id, user, lines, locale="vi", now=None, expected_revision=None):
    supplied_now = now
    round_ = CSARound.objects.select_for_update().get(
        pk=round_id, published_at__isnull=False, cancelled_at__isnull=True
    )
    existing = CSASelection.objects.filter(round=round_, membership__customer__user=user).first()
    if not lines and existing is None:
        _fail("csa_selection_empty")
    requested = [item["product_id"] for item in lines]
    if len(requested) != len(set(requested)) or len(lines) > 100:
        _fail("csa_selection_invalid")
    locked_products = {p.pk: p for p in _lock_resources(existing, requested)}
    # Snapshot shipping once. Later edits/reselections keep this delivery contract
    # even when the member changes or deletes their current profile address.
    address = None
    if existing is None:
        _lock_checkout_identity(user)
        address, _area, _saved_address = _address_for_checkout(user, None)
    now = supplied_now or timezone.now()
    member = _entitled_membership(user, round_, existing, now)
    now = supplied_now or timezone.now()
    if existing:
        existing = CSASelection.objects.select_for_update().get(pk=existing.pk)
    _validate_selection_window(round_, member, existing, now)
    if (
        expected_revision is not None
        and (existing.revision if existing else 0) != expected_revision
    ):
        _fail("csa_selection_stale")
    ensure_due_cycles(member)
    products = {
        p.pk: p
        for p in selectable_csa_products().filter(pk__in=requested).prefetch_related("translations")
    }
    previous = selection_held_lines(existing, now=now)
    capacity = selection_stock_capacity(products, existing, now=now)
    draft = []
    protected_points = defaultdict(Decimal)
    new_points = defaultdict(Decimal)
    for item in lines:
        product_id = item["product_id"]
        qty = item["quantity"]
        if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
            _fail("csa_selection_invalid")
        product = products.get(product_id)
        old_line, own = previous.get(product_id, (None, 0))
        available = capacity.get(product_id, 0)
        limit = selection_quantity_limit(product, available, old_line, own)
        if qty > limit:
            if product is None or limit < available:
                _fail("csa_product_unavailable")
            _fail("csa_stock_insufficient")
        product = product or locked_products[product_id]
        if old_line:
            # A retained quantity keeps the terms already committed, including
            # when the product comes back on sale or its retail price changes.
            snapshot = old_line.snapshot
            group_id = uuid.UUID(snapshot["quota_group_id"])
            cost = Decimal(snapshot["quota_cost"])
        else:
            group_id = product.csa_quota_group_id
            cost = product.csa_quota_cost
            translation = _translation(product, locale)
            snapshot = {
                "sku": product.sku,
                "name": translation.name,
                "unit_size": str(product.unit_size),
                "unit_label": product.unit_label_vi if locale == "vi" else product.unit_label_en,
                "quota_group_id": str(group_id),
                "quota_cost": str(cost),
                "unit_price_vnd": product.retail_price_vnd,
            }
        protected_points[group_id] += min(qty, own) * cost
        new_points[group_id] += max(0, qty - own) * cost
        draft.append((product, qty, snapshot))

    candidates = _quota_candidates(member, existing, set(new_points))
    own_quota = (
        dict(existing.quota_holds.values_list("cycle_quota_id", "units")) if existing else {}
    )
    allocations = defaultdict(Decimal)
    for group_id, rows in candidates.items():
        needed = protected_points.pop(group_id, Decimal(0))
        # A reduction releases newer quota before it gives up a carried hold.
        for row in sorted(rows, key=lambda item: item[0].cycle.sequence):
            quota, remaining = row
            take = min(needed, remaining, own_quota.get(quota.pk, 0))
            if take > 0:
                allocations[quota.pk] += take
                row[1] -= take
                needed -= take
        if needed:
            _fail("csa_quota_insufficient")
    if any(protected_points.values()):
        _fail("csa_quota_insufficient")

    for group_id, needed in new_points.items():
        for row in candidates[group_id]:
            quota, remaining = row
            take = min(needed, remaining)
            if take > 0:
                allocations[quota.pk] += take
                row[1] -= take
                needed -= take
        if needed:
            _fail("csa_quota_insufficient")
    # Inventory/quota locks may have waited past a deadline or a batch date boundary.
    decision_at = supplied_now or timezone.now()
    _validate_selection_window(round_, member, existing, decision_at)
    if timezone.localdate(decision_at) != timezone.localdate(now):
        capacity = selection_stock_capacity(products, existing, now=decision_at)
        for product, quantity, _snapshot in draft:
            old_line, own = previous.get(product.pk, (None, 0))
            if quantity > selection_quantity_limit(
                products.get(product.pk), capacity.get(product.pk, 0), old_line, own
            ):
                _fail("csa_stock_insufficient")
    selection = existing or CSASelection.objects.create(
        round=round_, membership=member, locale=locale, address_snapshot=address
    )
    selection.lines.all().delete()
    selection.stock_holds.all().delete()
    selection.quota_holds.all().delete()
    from memberships.models import CSAQuotaHold, CSAStockHold

    CSASelectionLine.objects.bulk_create(
        [
            CSASelectionLine(selection=selection, product=p, quantity=q, snapshot=snapshot)
            for p, q, snapshot in draft
        ]
    )
    CSAStockHold.objects.bulk_create(
        [CSAStockHold(selection=selection, product=p, quantity=q) for p, q, _ in draft]
    )
    CSAQuotaHold.objects.bulk_create(
        [
            CSAQuotaHold(selection=selection, cycle_quota_id=pk, units=units)
            for pk, units in allocations.items()
        ]
    )
    selection.revision += 1
    selection.locale = locale
    selection.save(update_fields=["revision", "locale", "updated_at"])
    member.extension_end_date = max(extension_date(member), round_.delivery_date)
    member.save(update_fields=["extension_end_date", "updated_at"])
    _event(
        selection,
        "saved",
        user,
        revision=selection.revision,
        lines=[{"product_id": str(p.pk), "quantity": q, **snapshot} for p, q, snapshot in draft],
    )
    return selection


def _reverse_usage(selection, actor, reason):
    if not selection.current_usage_id:
        return
    usage = MembershipUsageOrder.objects.select_for_update().get(pk=selection.current_usage_id)
    if usage.status == "applied":
        for item in usage.lines.all():
            for allocation in item.allocations.select_related("cycle_quota").order_by(
                "cycle_quota_id"
            ):
                quota = MembershipCycleQuota.objects.select_for_update().get(
                    pk=allocation.cycle_quota_id
                )
                quota.remaining_units += allocation.units
                quota.save(update_fields=["remaining_units"])
        MembershipUsageReversal.objects.create(order=usage, actor=actor, reason=reason)
        usage.status = "reversed"
        usage.reversed_at = timezone.now()
        usage.save(update_fields=["status", "reversed_at"])
    selection.current_usage = None


def _consume(selection, actor):
    _reverse_usage(selection, actor, "CSA reselection")
    holds = list(
        selection.quota_holds.select_related("cycle_quota__product_snapshot").order_by(
            "cycle_quota_id"
        )
    )
    usage = MembershipUsageOrder.objects.create(
        membership=selection.membership,
        customer=selection.membership.customer,
        idempotency_key=uuid.uuid4(),
        csa_selection=selection,
        note=f"CSA {selection.round_id}; order {selection.order_id}",
        created_by=actor,
    )
    by_snapshot = defaultdict(list)
    for hold in holds:
        by_snapshot[hold.cycle_quota.product_snapshot_id].append(hold)
    for snapshot_id, group in by_snapshot.items():
        line = MembershipUsageLine.objects.create(
            order=usage, product_snapshot_id=snapshot_id, units=sum(h.units for h in group)
        )
        for hold in group:
            quota = MembershipCycleQuota.objects.select_for_update().get(pk=hold.cycle_quota_id)
            if quota.remaining_units < hold.units:
                _fail("csa_quota_insufficient")
            quota.remaining_units -= hold.units
            quota.save(update_fields=["remaining_units"])
            MembershipUsageAllocation.objects.create(line=line, cycle_quota=quota, units=hold.units)
    selection.current_usage = usage
    selection.quota_holds.all().delete()


def _write_order(selection):
    lines = list(selection.lines.all())
    if not lines:
        _fail("csa_selection_empty")
    order = selection.order
    if order is None:
        order = StoreOrder.objects.create(
            buyer_user=selection.membership.customer.user,
            create_idempotency_key=uuid.uuid4(),
            request_fingerprint=str(selection.pk),
            reference_code=_reference_code(),
            locale=selection.locale,
            status="pending_review",
            workflow_version=1,
            payment_status="not_required",
            subtotal_vnd=0,
            shipping_fee_vnd=0,
            total_vnd=0,
            expires_at=None,
            shipping_discount_reason="csa_round",
            **selection.address_snapshot,
        )
        selection.order = order
    else:
        order.locale = selection.locale
        order.save(update_fields=["locale", "updated_at"])
        order.lines.all().delete()
    for position, line in enumerate(lines):
        s = line.snapshot
        StoreOrderLine.objects.create(
            order=order,
            product_id=line.product_id,
            position=position,
            sku_snapshot=s["sku"],
            name_snapshot=s["name"],
            unit_size_snapshot=s["unit_size"],
            unit_label_snapshot=s["unit_label"],
            unit_price_vnd_snapshot=s["unit_price_vnd"],
            quantity=line.quantity,
            quota_group_id=s["quota_group_id"],
            quota_cost_snapshot=s["quota_cost"],
            quota_quantity=line.quantity,
            quota_units=Decimal(s["quota_cost"]) * line.quantity,
            cash_quantity=0,
            cash_subtotal_vnd=0,
        )


@transaction.atomic
def reopen_selection(*, selection_id, actor, deadline, now=None, expected_revision=None):
    supplied_now = now
    round_id = CSASelection.objects.values_list("round_id", flat=True).get(pk=selection_id)
    CSARound.objects.select_for_update().get(pk=round_id)
    selection = CSASelection.objects.select_related("round", "membership", "order").get(
        pk=selection_id
    )
    _lock_resources(selection, [])
    Membership.objects.select_for_update().get(pk=selection.membership_id)
    selection = (
        CSASelection.objects.select_for_update()
        .select_related("round", "order")
        .get(pk=selection_id)
    )
    now = supplied_now or timezone.now()
    if expected_revision is not None and expected_revision != selection.revision:
        _fail("csa_selection_stale")
    if not now < deadline < delivery_start(selection.round):
        _fail("csa_reselection_deadline_invalid")
    if (
        selection.state not in ("locked", "confirmed", "reselecting")
        or (selection.order.status != "pending_review")
        or (selection.state == "reselecting" and now >= selection.reopen_until)
    ):
        _fail("csa_selection_locked")
    if selection.membership.status == "revoked":
        _fail("csa_membership_unavailable")
    if selection.state != "reselecting":
        selection.lines.all().delete()
        selection.stock_holds.all().delete()
        selection.quota_holds.all().delete()
    selection.state = "reselecting"
    selection.reopen_until = deadline
    selection.revision += 1
    selection.save(update_fields=["state", "reopen_until", "revision", "updated_at"])
    _event(selection, "reopened", actor, deadline=deadline.isoformat(), revision=selection.revision)
    return selection


@transaction.atomic
def confirm_selection(*, selection_id, user, now=None, expected_revision=None):
    supplied_now = now
    round_id = CSASelection.objects.values_list("round_id", flat=True).get(pk=selection_id)
    CSARound.objects.select_for_update().get(pk=round_id)
    selection = CSASelection.objects.select_related("round", "membership__customer", "order").get(
        pk=selection_id, membership__customer__user=user
    )
    _lock_resources(selection, [])
    Membership.objects.select_for_update().get(pk=selection.membership_id)
    selection = CSASelection.objects.select_for_update().get(pk=selection_id)
    now = supplied_now or timezone.now()
    if selection.state == "confirmed":
        # A lost confirmation response may replay the pre-confirmation revision.
        if expected_revision is not None and expected_revision not in (
            selection.revision,
            selection.revision - 1,
        ):
            _fail("csa_selection_stale")
        return selection
    if selection.state != "reselecting" or now >= selection.reopen_until:
        _fail("csa_selection_locked")
    if expected_revision is not None and expected_revision != selection.revision:
        _fail("csa_selection_stale")
    if selection.membership.status == "revoked":
        _fail("csa_membership_unavailable")
    _write_order(selection)
    _consume(selection, user)
    selection.state = "confirmed"
    selection.revision += 1
    selection.save(update_fields=["state", "revision", "order", "current_usage", "updated_at"])
    _event(selection, "confirmed", user, revision=selection.revision)
    return selection


def _cancel_locked(selection, actor, reason):
    if selection.state in TERMINAL:
        return selection
    _reverse_usage(selection, actor, reason)
    selection.stock_holds.all().delete()
    selection.quota_holds.all().delete()
    selection.state = "cancelled"
    selection.save(update_fields=["state", "current_usage", "updated_at"])
    if selection.order_id:
        StoreOrder.objects.filter(pk=selection.order_id).update(
            status="cancelled",
            cancelled_at=timezone.now(),
            cancelled_by=actor,
            cancellation_reason=reason,
        )
    _event(selection, "cancelled", actor, reason=reason)
    return selection


@transaction.atomic
def cancel_selection(*, selection_id, actor, reason):
    selection = CSASelection.objects.select_related("order").get(pk=selection_id)
    _lock_resources(selection, [])
    Membership.objects.select_for_update().get(pk=selection.membership_id)
    selection = CSASelection.objects.select_for_update().get(pk=selection_id)
    if selection.order_id and selection.order.approved_at:
        _fail("csa_order_already_allocated")
    return _cancel_locked(selection, actor, reason)


@transaction.atomic
def _process_selection(selection_id, *, now=None):
    round_id = CSASelection.objects.values_list("round_id", flat=True).get(pk=selection_id)
    round_ = CSARound.objects.select_for_update().get(pk=round_id)
    if round_.published_at is None or round_.cancelled_at:
        return
    selection = CSASelection.objects.select_related("membership__customer", "order").get(
        pk=selection_id
    )
    _lock_resources(selection, [])
    member = Membership.objects.select_for_update().get(pk=selection.membership_id)
    selection = CSASelection.objects.select_for_update().get(pk=selection_id)
    now = now or timezone.now()
    if selection.state == "selecting" and now >= round_.closes_at:
        if not selection.lines.exists() or member.status == "revoked":
            _cancel_locked(selection, None, "empty_or_revoked")
            return
        _write_order(selection)
        _consume(selection, None)
        selection.state = "locked"
        selection.save(update_fields=["state", "order", "current_usage", "updated_at"])
        _event(selection, "closed")
    elif selection.state == "reselecting" and now >= selection.reopen_until:
        _cancel_locked(selection, None, "reselection_expired")


def process_round(round_id, *, now=None, user=None, selection_ids=None):
    round_ = CSARound.objects.get(pk=round_id)
    check_at = now or timezone.now()
    due = Q(state="reselecting", reopen_until__lte=check_at)
    if check_at >= round_.closes_at:
        due |= Q(state="selecting")
    selections = round_.selections.filter(due)
    if user is not None:
        selections = selections.filter(membership__customer__user=user)
    if selection_ids is not None:
        selections = selections.filter(pk__in=selection_ids)
    for pk in selections.values_list("pk", flat=True).iterator():
        # Commit each participant before taking the next one's product/member locks.
        _process_selection(pk, now=now)


@transaction.atomic
def cancel_round(*, round_id, actor, reason, version=None):
    round_ = CSARound.objects.select_for_update().get(pk=round_id)
    _check_schedule_version(round_, version)
    if round_.cancelled_at:
        return round_
    selections = round_.selections.exclude(state__in=TERMINAL)
    list(
        StoreOrder.objects.select_for_update()
        .filter(pk__in=selections.values("order_id"))
        .order_by("pk")
    )
    product_ids = set(
        CSASelectionLine.objects.filter(selection__in=selections).values_list(
            "product_id", flat=True
        )
    )
    product_ids.update(
        StoreOrderLine.objects.filter(order__csa_selection__in=selections).values_list(
            "product_id", flat=True
        )
    )
    list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))
    list(
        Membership.objects.select_for_update()
        .filter(pk__in=selections.values("membership_id"))
        .order_by("pk")
    )
    for selection in selections:
        cancel_selection(selection_id=selection.pk, actor=actor, reason=reason)
    round_.cancelled_at = timezone.now()
    round_.cancellation_reason = reason
    round_.cancelled_by = actor
    round_.version += 1
    round_.save(
        update_fields=[
            "cancelled_at",
            "cancellation_reason",
            "cancelled_by",
            "version",
            "updated_at",
        ]
    )
    return round_


def _due_recurrence_weeks(configuration, now, last_week):
    # Jump past closed weeks, including a week that closed exactly at the worker timestamp.
    first_unclosed_week = (now - configuration.closes_at) // WEEK + 1
    week = max(configuration.next_week, first_unclosed_week)
    if week > last_week:
        return range(week, week)
    due_weeks = (now - _recurrence_due_at(configuration, week)) // WEEK + 1
    count = min(max(0, due_weeks), last_week - week + 1, RECURRENCE_GENERATION_LIMIT)
    return range(week, week + count)


@transaction.atomic
def _process_recurrence(configuration_id, *, now=None):
    configuration = (
        CSARoundConfiguration.objects.select_for_update().filter(pk=configuration_id).first()
    )
    current_time = now or timezone.now()
    if (
        configuration is None
        or configuration.status != "published"
        or configuration.next_create_at is None
        or configuration.next_create_at > current_time
    ):
        return 0

    last_week = _last_recurrence_week(configuration.delivery_date)
    weeks = _due_recurrence_weeks(configuration, current_time, last_week)
    if weeks:
        # Allocate one contiguous range while the sequence lock and cursor share a transaction.
        sequence = _next_round_sequence()
        if now is None:
            # The sequence lock may have waited past the closing time of a due round.
            current_time = timezone.now()
            weeks = _due_recurrence_weeks(configuration, current_time, last_week)
        published_at = (
            current_time
            if configuration.recurrence_mode == CSARoundConfiguration.Mode.AUTO
            else None
        )
        for index, occurrence_week in enumerate(weeks):
            offset = WEEK * occurrence_week
            _create_round(
                name=configuration.name,
                opens_at=configuration.opens_at + offset,
                closes_at=configuration.closes_at + offset,
                delivery_date=configuration.delivery_date + offset,
                sequence=sequence + index,
                created_by_id=configuration.created_by_id,
                published_at=published_at,
                configuration=configuration,
                recurrence_week=occurrence_week,
            )
    configuration.next_week = weeks.stop
    if configuration.next_week <= last_week:
        configuration.next_create_at = _recurrence_due_at(configuration, configuration.next_week)
    else:
        configuration.next_create_at = None
        logger.warning(
            "CSA recurring configuration %s reached the maximum supported delivery date",
            configuration_id,
        )
    configuration.save(update_fields=["next_week", "next_create_at"])
    return len(weeks)


def process_round_recurrences(*, now=None):
    due_configurations = (
        CSARoundConfiguration.objects.filter(next_create_at__lte=now or timezone.now())
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    count = 0
    failed = 0
    # Page by a stable key so generation and cursor updates can commit between reads.
    page = list(due_configurations[:RECURRENCE_SCAN_PAGE_SIZE])
    while page:
        for configuration_id in page:
            try:
                count += _process_recurrence(configuration_id, now=now)
            except Exception:
                # The transaction rolled back this configuration. Leave it due for retry.
                failed += 1
                logger.exception(
                    "Could not generate CSA rounds for configuration %s", configuration_id
                )
        page = list(due_configurations.filter(pk__gt=page[-1])[:RECURRENCE_SCAN_PAGE_SIZE])
    return count, failed


def process_due_rounds(*, now=None, user=None):
    now = now or timezone.now()
    ids = (
        CSARound.objects.filter(
            published_at__isnull=False,
            cancelled_at__isnull=True,
            closes_at__lte=now,
            selections__state__in=["selecting", "reselecting"],
        )
        .values_list("pk", flat=True)
        .distinct()
    )
    if user is not None:
        ids = ids.filter(selections__membership__customer__user=user)
    count = 0
    for pk in ids.iterator():
        process_round(pk, now=now, user=user)
        count += 1
    from memberships.services import refresh_membership_statuses

    refresh_membership_statuses(as_of=now.astimezone(VIETNAM).date(), user=user)
    return count
