"""Read-only Phase 2.1 cutover checks frozen at the legacy writer boundary."""

from zoneinfo import ZoneInfo

from django.db import models
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Cast, Coalesce, Round
from django.utils import timezone

LEGACY_TARGETS = (
    ("farms", "0004_repair_missing_legacy_supply_table"),
    ("commerce", "0015_product_status_legacy_flags_match"),
    ("memberships", "0043_csa_customer_note"),
)
LIFECYCLE_MIGRATIONS = (
    ("farms", "0005_farm_product_lifecycle_expand"),
    ("farms", "0006_farm_product_lifecycle_backfill"),
    ("farms", "0007_farm_product_lifecycle_constraints"),
)
LIFECYCLE_SCHEMA = {
    "farms_farmproduct": (
        {"removed_at", "removed_by_id", "version", "supply_generation"},
        {
            "farms_product_lifecycle_state",
            "farms_product_version_positive",
            "farms_product_generation_positive",
        },
    ),
    "farms_farmproductstockbatch": (
        {"supply_generation"},
        {"farms_batch_generation_positive"},
    ),
}
TERMINAL = ("delivered", "cancelled")
STORE_WAITING = ("payment_pending", "pending_review", "payment_review")
VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")
SAMPLE_LIMIT = 10


class FarmProductLifecyclePreflightError(RuntimeError):
    pass


def _reject(queryset, reason, *fields):
    examples = list(queryset.order_by("pk").values_list(*fields or ("pk",))[:SAMPLE_LIMIT])
    if examples:
        identifiers = ", ".join("/".join(map(str, item)) for item in examples)
        raise FarmProductLifecyclePreflightError(f"{reason}: {identifiers}")


def _check_ledger(batch, line, allocation, movement):
    _reject(
        movement.exclude(
            Q(kind__in=("sale", "restock"), quantity__gt=0)
            & Q(batch_id=F("allocation__batch_id"), line_id=F("allocation__line_id"))
        ),
        "movement_scope_or_kind",
    )
    _reject(
        allocation.exclude(line__product_id=F("batch__farm_product__product_id")),
        "allocation_product_scope",
    )
    _reject(
        allocation.annotate(
            sale_count=Count("movements", filter=Q(movements__kind="sale")),
            sold=Sum("movements__quantity", filter=Q(movements__kind="sale"), default=0),
            restocked=Sum("movements__quantity", filter=Q(movements__kind="restock"), default=0),
        ).exclude(sale_count=1, sold=F("quantity"), restocked__lte=F("quantity")),
        "allocation_ledger",
    )
    _reject(
        line.annotate(allocated=Sum("stock_allocations__quantity", default=0)).exclude(
            Q(order__approved_at__isnull=False, allocated=F("quantity"))
            | Q(order__approved_at__isnull=True, allocated=0)
        ),
        "order_line_allocation_total",
    )
    _reject(
        batch.annotate(
            sold=Sum(
                "store_stock_movements__quantity",
                filter=Q(store_stock_movements__kind="sale"),
                default=0,
            ),
            restocked=Sum(
                "store_stock_movements__quantity",
                filter=Q(store_stock_movements__kind="restock"),
                default=0,
            ),
        ).filter(sold__gt=F("quantity_units") + F("restocked")),
        "batch_ledger_oversold",
    )
    _reject(
        allocation.filter(batch__farm_product__is_active=False).exclude(
            line__order__status__in=TERMINAL
        ),
        "disabled_source_open_allocation",
        "batch__farm_product_id",
        "line__order_id",
    )


def _check_demand(
    line, reservation, quota_reservation, selection, selection_line, hold, quota_hold, *, now
):
    _reject(
        reservation.exclude(product_id=F("line__product_id"), quantity=F("line__quantity")),
        "store_reservation_scope_or_quantity",
    )
    _reject(
        reservation.filter(released_at__isnull=True).exclude(
            line__order__status__in=STORE_WAITING,
            line__order__approved_at__isnull=True,
            line__order__expires_at__isnull=False,
            line__order__csa_selection__isnull=True,
        ),
        "store_unreleased_reservation_state",
    )
    current_store_lines = line.filter(
        order__status__in=STORE_WAITING,
        order__approved_at__isnull=True,
        order__expires_at__gt=now,
        order__csa_selection__isnull=True,
    )
    _reject(
        current_store_lines.exclude(
            Exists(reservation.filter(line_id=OuterRef("pk"), released_at__isnull=True))
        ),
        "store_missing_effective_reservation",
    )
    _reject(
        quota_reservation.filter(
            ~Q(
                cycle_quota__product_snapshot__product_id=F("line__quota_group_id"),
                cycle_quota__cycle__membership__customer__user_id=F("line__order__buyer_user_id"),
                line__quota_group_id__isnull=False,
                line__order__buyer_user_id__isnull=False,
            )
            | Q(released_at__isnull=False, consumed_at__isnull=False)
        ),
        "store_quota_reservation_scope_or_state",
    )
    _reject(
        current_store_lines.annotate(
            reserved_quota=Round(
                Sum(
                    "quota_reservations__units",
                    filter=Q(
                        quota_reservations__released_at__isnull=True,
                        quota_reservations__consumed_at__isnull=True,
                    ),
                    default=0,
                ),
                precision=2,
            )
        ).exclude(reserved_quota=F("quota_units")),
        "store_quota_reservation_total",
    )

    _reject(
        selection.exclude(state__in=("selecting", "locked", "reselecting", "confirmed", *TERMINAL))
        | selection.filter(state="reselecting", reopen_until__isnull=True),
        "csa_selection_state_or_deadline",
    )
    _reject(
        selection.filter(current_usage_id__isnull=False).exclude(
            current_usage__status="applied",
            current_usage__membership_id=F("membership_id"),
        )
        | selection.filter(current_usage__csa_selection_id__isnull=False).exclude(
            pk=F("current_usage__csa_selection_id")
        ),
        "csa_current_usage_scope_or_state",
    )
    _reject(
        hold.exclude(
            Exists(
                selection_line.filter(
                    selection_id=OuterRef("selection_id"),
                    product_id=OuterRef("product_id"),
                    quantity=OuterRef("quantity"),
                )
            )
        ),
        "csa_stock_hold_scope_or_quantity",
    )
    effective_selections = (
        selection.exclude(state__in=TERMINAL)
        .filter(round__cancelled_at__isnull=True)
        .exclude(state="reselecting", reopen_until__lte=now)
    )
    _reject(
        hold.filter(
            selection__in=effective_selections, selection__order__approved_at__isnull=False
        ),
        "csa_hold_after_sale",
    )
    _reject(
        selection_line.filter(
            selection__in=effective_selections, selection__order__approved_at__isnull=True
        ).exclude(
            Exists(
                hold.filter(
                    selection_id=OuterRef("selection_id"), product_id=OuterRef("product_id")
                )
            )
        ),
        "csa_missing_effective_stock_hold",
    )
    _reject(
        selection_line.filter(selection__in=effective_selections).exclude(
            snapshot__has_keys=("quota_group_id", "quota_cost"),
        ),
        "csa_stock_hold_snapshot_missing",
    )
    _reject(
        selection_line.filter(
            selection__state__in=("locked", "confirmed"), selection__order_id__isnull=False
        ).exclude(
            Exists(
                line.filter(
                    order_id=OuterRef("selection__order_id"),
                    product_id=OuterRef("product_id"),
                    quantity=OuterRef("quantity"),
                )
            )
        ),
        "csa_order_line_scope_or_quantity",
    )
    _reject(
        quota_hold.exclude(
            Q(selection__membership_id=F("cycle_quota__cycle__membership_id"))
            & Q(selection__membership_id=F("cycle_quota__product_snapshot__membership_id"))
            & Q(selection__state__in=("selecting", "reselecting"))
        ),
        "csa_quota_hold_scope_or_state",
    )
    return (
        reservation.filter(
            released_at__isnull=True,
            line__order__status__in=STORE_WAITING,
            line__order__expires_at__gt=now,
        ),
        hold.filter(selection__in=effective_selections),
    )


def _sum_by(queryset, key, expression):
    totals = queryset.order_by().values(key).annotate(total=Sum(expression)).values("total")
    return Coalesce(Subquery(totals, output_field=models.BigIntegerField()), Value(0))


def _check_coverage(product, batch, movement, retail, csa, *, today):
    eligible = batch.filter(
        farm_product__product_id=OuterRef("pk"),
        farm_product__is_active=True,
        farm_product__farm__is_active=True,
        cancelled_at__isnull=True,
        start_date__lte=today,
        end_date__gte=today,
    )
    if "supply_generation" in {field.name for field in batch.model._meta.concrete_fields}:
        eligible = eligible.filter(supply_generation=F("farm_product__supply_generation"))
    units = Cast("quantity", models.BigIntegerField())
    net_sold = _sum_by(
        movement.filter(batch_id=OuterRef("pk")),
        "batch_id",
        models.Case(models.When(kind="sale", then=units), default=-units),
    )
    stock = _sum_by(
        eligible,
        "farm_product__product_id",
        Cast("quantity_units", models.BigIntegerField()) - net_sold,
    )
    demand = _sum_by(retail.filter(product_id=OuterRef("pk")), "product_id", "quantity") + _sum_by(
        csa.filter(product_id=OuterRef("pk")),
        "product_id",
        "quantity",
    )
    deficits = product.annotate(available=stock, outstanding=demand).filter(
        available__lt=F("outstanding")
    )
    examples = list(
        deficits.order_by("pk").values_list("pk", "available", "outstanding")[:SAMPLE_LIMIT]
    )
    if examples:
        bounds = ", ".join(
            f"{pk} available={stock} demand={demand}" for pk, stock, demand in examples
        )
        raise FarmProductLifecyclePreflightError(f"coverage_deficit: {bounds}")


def _check_schema(connection, querysets):
    recorder = MigrationRecorder(connection)
    applied = recorder.applied_migrations()
    missing = [f"{app}.{name}" for app, name in LEGACY_TARGETS if (app, name) not in applied]
    if missing:
        raise FarmProductLifecyclePreflightError(f"missing_migration: {', '.join(missing)}")

    introspection = connection.introspection
    with connection.cursor() as cursor:
        tables = set(introspection.table_names(cursor))
        if "farms_farmweeklysupply" in tables:
            raise FarmProductLifecyclePreflightError(
                "legacy_archive_rename_incomplete: farms_farmweeklysupply"
            )
        for rows in querysets:
            meta = rows.model._meta
            table = meta.db_table
            if table not in tables:
                raise FarmProductLifecyclePreflightError(f"missing_table: {table}")
            columns = {column.name for column in introspection.get_table_description(cursor, table)}
            expected_columns = {field.column for field in meta.concrete_fields}
            missing = expected_columns - columns
            if missing:
                raise FarmProductLifecyclePreflightError(
                    f"missing_column: {table}.{','.join(sorted(missing))}"
                )
            constraints = introspection.get_constraints(cursor, table)
            lifecycle_columns, lifecycle_constraints = LIFECYCLE_SCHEMA.get(table, (set(), set()))
            unexpected = (lifecycle_columns & columns) - expected_columns
            unexpected.update(
                (lifecycle_constraints & constraints.keys())
                - {item.name for item in meta.constraints}
            )
            if unexpected:
                raise FarmProductLifecyclePreflightError(
                    f"unrecorded_lifecycle_schema: {table}.{','.join(sorted(unexpected))}"
                )
            for constraint in meta.constraints:
                actual = constraints.get(constraint.name, {})
                if isinstance(constraint, models.CheckConstraint):
                    valid = actual.get("check")
                else:
                    expected = [meta.get_field(field).column for field in constraint.fields]
                    valid = actual.get("unique") and list(actual["columns"]) == expected
                if not valid:
                    raise FarmProductLifecyclePreflightError(
                        f"missing_constraint: {table}.{constraint.name}"
                    )
            for field in meta.concrete_fields:
                key = "primary_key" if field.primary_key else "unique"
                if field.unique and not any(
                    item.get(key) and list(item["columns"]) == [field.column]
                    for item in constraints.values()
                ):
                    raise FarmProductLifecyclePreflightError(
                        f"missing_unique_key: {table}.{field.column}"
                    )
                if field.is_relation and field.db_constraint:
                    target = (field.related_model._meta.db_table, field.target_field.column)
                    if not any(
                        item.get("foreign_key") == target
                        and list(item["columns"]) == [field.column]
                        for item in constraints.values()
                    ):
                        raise FarmProductLifecyclePreflightError(
                            f"missing_foreign_key: {table}.{field.column}"
                        )
                    related = field.related_model._base_manager.using(connection.alias)
                    _reject(
                        rows.filter(**{f"{field.attname}__isnull": False}).exclude(
                            Exists(
                                related.filter(
                                    **{field.target_field.attname: OuterRef(field.attname)}
                                )
                            )
                        ),
                        f"orphaned_foreign_key: {table}.{field.column}",
                    )
    return LIFECYCLE_MIGRATIONS[-1] in applied


def validate_lifecycle_preflight(apps, connection, *, now=None):
    """Validate both the pre-expand and expanded schemas without changing persisted data."""

    def rows(app, model):
        return apps.get_model(app, model).objects.using(connection.alias)

    now = now or timezone.now()
    farm, source, batch, archive = (
        rows("farms", name)
        for name in ("Farm", "FarmProduct", "FarmProductStockBatch", "LegacyFarmWeeklySupply")
    )
    product, order, line, reservation, quota_reservation, allocation, movement = (
        rows("commerce", name)
        for name in (
            "Product",
            "StoreOrder",
            "StoreOrderLine",
            "StoreStockReservation",
            "StoreQuotaReservation",
            "StoreStockAllocation",
            "StoreStockMovement",
        )
    )
    round_rows, selection, selection_line, hold, quota_hold = (
        rows("memberships", name)
        for name in (
            "CSARound",
            "CSASelection",
            "CSASelectionLine",
            "CSAStockHold",
            "CSAQuotaHold",
        )
    )
    finalized = _check_schema(
        connection,
        (
            farm,
            source,
            batch,
            archive,
            product,
            order,
            line,
            reservation,
            quota_reservation,
            allocation,
            movement,
            round_rows,
            selection,
            selection_line,
            hold,
            quota_hold,
        ),
    )
    compatible = (
        Q(status="draft", is_active=True, is_published=False)
        | Q(status="active", is_active=True, is_published=True)
        | Q(status="discontinued", is_active=False, is_published=False)
    )
    _reject(product.exclude(compatible), "product_status_legacy_flags")
    _reject(archive.filter(quantity_units__lt=0), "legacy_archive_negative_quantity")
    matching_batch = batch.filter(pk=OuterRef("pk"))
    _reject(
        archive.filter(Exists(matching_batch)).filter(
            Q(quantity_units=0)
            | ~Exists(matching_batch.filter(farm_product_id=OuterRef("farm_product_id")))
        ),
        "legacy_archive_batch_collision",
    )
    _reject(
        batch.filter(
            Q(quantity_units__lte=0) | Q(start_date__gt=F("end_date")) | Q(version__lte=0)
        ),
        "stock_batch_quantity_dates_or_version",
    )
    if "supply_generation" in {field.name for field in batch.model._meta.concrete_fields}:
        if not finalized:
            _reject(source.exclude(version=1, supply_generation=1), "unexpected_source_at_cutover")
            _reject(batch.exclude(supply_generation=1), "unexpected_batch_generation_at_cutover")
        _reject(
            source.filter(Q(supply_generation__lt=1) | Q(version__lt=1)), "source_lifecycle_version"
        )
        _reject(
            batch.filter(
                Q(supply_generation__lt=1)
                | Q(supply_generation__gt=F("farm_product__supply_generation"))
            ),
            "stock_batch_generation",
        )
    _check_ledger(batch, line, allocation, movement)
    retail, csa = _check_demand(
        line, reservation, quota_reservation, selection, selection_line, hold, quota_hold, now=now
    )
    _check_coverage(
        product, batch, movement, retail, csa, today=timezone.localdate(now, timezone=VIETNAM)
    )
