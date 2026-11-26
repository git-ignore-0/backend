import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from commerce.models import (
    StoreOrder,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockMovementKind,
)
from commerce.serializers import StoreCheckoutSerializer
from commerce.store_orders import approve_store_order, create_store_checkout
from farms.models import FarmProduct, FarmProductStockBatch, LegacyFarmWeeklySupply
from tests.test_farm_lifecycle_approval import _allocation, _select_csa
from tests.test_farm_lifecycle_approval import inventory as inventory
from tests.test_farm_lifecycle_approval import membership as membership
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def _place_guest_order(product):
    payload = guest_payload(product, quantity=10)
    payload["delivery_date"] += timedelta(days=1)
    serializer = StoreCheckoutSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    order, _, created = create_store_checkout(user=None, payload=serializer.validated_data)
    assert created
    return order


def test_preflight_accepts_historical_terminal_allocation_and_zero_weekly_archive(
    active_user, store_product, payment_settings
):
    from farms.migrations._lifecycle_preflight import validate_lifecycle_preflight

    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    order = _place_guest_order(store_product)
    approve_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        allocations=[_allocation(order, batch)],
    )
    StoreOrder.objects.filter(pk=order.pk).update(
        status="cancelled", workflow_version=1, expires_at=None
    )
    FarmProduct.objects.filter(pk=batch.farm_product_id).update(
        is_active=False, removed_at=timezone.now()
    )
    legacy = LegacyFarmWeeklySupply.objects.create(
        farm_product=batch.farm_product, week_start=batch.start_date, quantity_units=0
    )
    validate_lifecycle_preflight(apps, connection)
    assert LegacyFarmWeeklySupply.objects.get(pk=legacy.pk).quantity_units == 0
    assert StoreStockMovement.objects.get(batch=batch).kind == StoreStockMovementKind.SALE


def test_preflight_reports_expired_source_coverage_deficit_without_repair(
    store_product, payment_settings
):
    from farms.migrations._lifecycle_preflight import (
        FarmProductLifecyclePreflightError,
        validate_lifecycle_preflight,
    )

    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    order = _place_guest_order(store_product)
    FarmProductStockBatch.objects.filter(pk=batch.pk).update(
        start_date=timezone.localdate() - timedelta(days=2),
        end_date=timezone.localdate() - timedelta(days=1),
    )
    with pytest.raises(
        FarmProductLifecyclePreflightError,
        match=rf"coverage_deficit.*{store_product.pk}.*available=0.*demand=10",
    ):
        validate_lifecycle_preflight(apps, connection)
    assert order.lines.get().stock_reservations.get().released_at is None
    assert not StoreStockAllocation.objects.exists()


def test_preflight_rejects_quantity_overrestock_without_mutating_ledger(
    active_user, store_product, payment_settings
):
    from farms.migrations._lifecycle_preflight import (
        FarmProductLifecyclePreflightError,
        validate_lifecycle_preflight,
    )

    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    order = _place_guest_order(store_product)
    approve_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        allocations=[_allocation(order, batch)],
    )
    allocation = StoreStockAllocation.objects.get()
    reversal = StoreStockMovement.objects.create(
        allocation=allocation,
        batch=batch,
        line=allocation.line,
        kind=StoreStockMovementKind.RESTOCK,
        idempotency_key=uuid.uuid4(),
        quantity=11,
    )
    with pytest.raises(
        FarmProductLifecyclePreflightError, match=rf"allocation_ledger.*{allocation.pk}"
    ):
        validate_lifecycle_preflight(apps, connection)
    assert StoreStockMovement.objects.get(pk=reversal.pk).quantity == 11


def test_preflight_rejects_an_unrecorded_legacy_schema_migration():
    from farms.migrations._lifecycle_preflight import (
        FarmProductLifecyclePreflightError,
        validate_lifecycle_preflight,
    )

    MigrationRecorder(connection).record_unapplied(
        "farms", "0004_repair_missing_legacy_supply_table"
    )
    with pytest.raises(
        FarmProductLifecyclePreflightError, match="0004_repair_missing_legacy_supply_table"
    ):
        validate_lifecycle_preflight(apps, connection)


def test_preflight_command_runs_using_selected_alias_and_reports_write_fence():
    out = StringIO()
    call_command("check_farm_product_lifecycle", database=connection.alias, stdout=out)
    assert f"Database {connection.alias} passed" in out.getvalue()
    assert "write fence" in out.getvalue()


def test_preflight_command_requires_schema_matching_recorded_lifecycle_stage():
    from farms.migrations._lifecycle_preflight import LIFECYCLE_MIGRATIONS

    recorder = MigrationRecorder(connection)
    for app, migration in LIFECYCLE_MIGRATIONS:
        recorder.record_unapplied(app, migration)
    with pytest.raises(CommandError, match="unrecorded_lifecycle_schema.*farms_farmproduct"):
        call_command("check_farm_product_lifecycle", database=connection.alias)


def test_preflight_aggregates_live_store_reservation_and_csa_hold_at_same_clock(
    active_user, store_product, inventory, membership, payment_settings
):
    from farms.migrations._lifecycle_preflight import (
        FarmProductLifecyclePreflightError,
        validate_lifecycle_preflight,
    )

    clock, _ = inventory
    selection = _select_csa(active_user, store_product)
    order = _place_guest_order(store_product)
    validate_lifecycle_preflight(apps, connection)

    clock.now = clock.next_day
    with pytest.raises(
        FarmProductLifecyclePreflightError,
        match=rf"coverage_deficit.*{store_product.pk}.*available=10.*demand=20",
    ):
        validate_lifecycle_preflight(apps, connection)

    StoreOrder.objects.filter(pk=order.pk).update(expires_at=clock.now)
    validate_lifecycle_preflight(apps, connection)
    assert order.lines.get().stock_reservations.get().released_at is None
    assert selection.stock_holds.get().quantity == 10
