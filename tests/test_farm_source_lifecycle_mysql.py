"""Force both acquisition orders at the actual Product mutex on MySQL."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.utils import timezone

from commerce.models import (
    StoreOrder,
    StoreOrderLine,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockReservation,
)
from commerce.store_orders import StoreOrderError, active_product_stock_totals, approve_store_order
from content.models import ContentAuditLog
from farms import services as lifecycle
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships import csa_rounds
from memberships.models import CSAQuotaHold, CSASelection, CSASelectionLine, CSAStockHold
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_product_lifecycle_locking import (
    assert_csa_holds,
    discontinue,
    run_in_lock_order,
)
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "mysql" or not connection.features.has_select_for_update,
        reason="requires MySQL with SELECT FOR UPDATE",
    ),
]


def source_command(source, actor):
    return {
        "farm_id": source.farm_id,
        "product_id": source.product_id,
        "expected_link_id": source.pk,
        "expected_version": source.version,
        "actor": actor,
    }


def source_events(source, action):
    return ContentAuditLog.objects.filter(
        entity_type="farmproduct", entity_id=str(source.pk), action=f"farm.product.{action}"
    ).count()


def assert_source_state(source, *, removed, version, generation=1):
    source.refresh_from_db()
    assert (source.removed_at is not None) is removed
    assert source.is_active is not removed
    assert (source.version, source.supply_generation) == (version, generation)


def batch_data(quantity):
    today = timezone.localdate()
    return {"start_date": today, "end_date": today + timedelta(days=2), "quantity_units": quantity}


@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "checkout-first"])
def test_remove_and_store_checkout_share_product_lock(
    store_product, active_user, checkout_operation, remove_first
):
    source = store_product.farm_offers.get()
    command = source_command(source, active_user)

    removing, checkout = run_in_lock_order(
        lambda: lifecycle.remove_farm_product(**command),
        checkout_operation,
        left_first=remove_first,
    )

    if remove_first:
        assert removing.result().pk == source.pk
        with pytest.raises(StoreOrderError) as error:
            checkout.result()
        assert error.value.code == "store_requote_required"
        assert error.value.context["available_quantity"] == 0
        assert not StoreOrder.objects.exists()
        assert not StoreOrderLine.objects.exists()
        assert not StoreStockReservation.objects.exists()
    else:
        order, _, created = checkout.result()
        assert created
        reservation = order.lines.get().stock_reservations.get()
        assert (reservation.quantity, reservation.released_at) == (2, None)
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_supply_shortfall$"):
            removing.result()
    assert_source_state(source, removed=remove_first, version=2 if remove_first else 1)
    assert source_events(source, "remove") == int(remove_first)
    assert not StoreStockAllocation.objects.exists()
    assert not StoreStockMovement.objects.exists()


@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "approve-first"])
def test_remove_and_approval_never_commit_a_sale_to_removed_source(
    store_product, active_user, checkout_operation, remove_first
):
    order, _, created = checkout_operation()
    assert created
    line = order.lines.get()
    source = store_product.farm_offers.get()
    batch = source.stock_batches.get()
    backup = Farm.objects.create(
        name_en="Backup", name_vi="Backup", location_en="Da Lat", location_vi="Da Lat"
    )
    backup_source = FarmProduct.objects.create(farm=backup, product=store_product)
    FarmProductStockBatch.objects.create(farm_product=backup_source, **batch_data(10))
    approval_key = uuid.uuid4()

    removing, approving = run_in_lock_order(
        lambda: lifecycle.remove_farm_product(**source_command(source, active_user)),
        lambda: approve_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=approval_key,
            allocations=[{"line_id": line.pk, "batch_id": batch.pk, "quantity": 2}],
        ),
        left_first=remove_first,
    )

    order.refresh_from_db()
    reservation = line.stock_reservations.get()
    if remove_first:
        assert removing.result().pk == source.pk
        with pytest.raises(StoreOrderError, match="^store_allocation_stale$"):
            approving.result()
        assert order.status == StoreOrderStatus.PENDING_REVIEW
        assert reservation.released_at is None
        assert not line.stock_allocations.exists()
        assert not StoreStockMovement.objects.filter(line=line).exists()
        assert not order.actions.filter(action="approve").exists()
    else:
        assert approving.result().pk == order.pk
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_has_open_allocations$"):
            removing.result()
        assert order.status == StoreOrderStatus.PAYMENT_PENDING
        assert reservation.released_at is not None
        allocation = line.stock_allocations.get()
        assert (allocation.batch_id, allocation.quantity) == (batch.pk, 2)
        assert list(allocation.movements.values_list("kind", "quantity")) == [("sale", 2)]
        assert order.actions.filter(action="approve", idempotency_key=approval_key).count() == 1
    assert_source_state(source, removed=remove_first, version=2 if remove_first else 1)
    assert source_events(source, "remove") == int(remove_first)


@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "hold-first"])
def test_remove_and_csa_save_keep_a_committed_stock_hold_covered(context, remove_first):
    actor, product, membership, round_, _ = context
    source = product.farm_offers.get()

    removing, selecting = run_in_lock_order(
        lambda: lifecycle.remove_farm_product(**source_command(source, actor)),
        lambda: save_selection(context),
        left_first=remove_first,
    )

    if remove_first:
        assert removing.result().pk == source.pk
        with pytest.raises(csa_rounds.CSARoundError, match="^csa_stock_insufficient$"):
            selecting.result()
        assert not CSASelection.objects.filter(round=round_, membership=membership).exists()
        assert not CSASelectionLine.objects.exists()
        assert not CSAStockHold.objects.exists()
        assert not CSAQuotaHold.objects.exists()
    else:
        selected = selecting.result()
        assert_csa_holds(selected, product, cost=Decimal("1"))
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_supply_shortfall$"):
            removing.result()
    assert not StoreOrder.objects.exists()
    assert_source_state(source, removed=remove_first, version=2 if remove_first else 1)
    assert source_events(source, "remove") == int(remove_first)


@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "batch-first"])
@pytest.mark.parametrize("operation", ["create", "edit", "cancel"])
def test_remove_serializes_with_an_ordinary_batch_write(
    store_product, active_user, operation, remove_first
):
    source = store_product.farm_offers.get()
    original = source.stock_batches.get()
    command = source_command(source, active_user)
    batch_command = {
        "farm_id": source.farm_id,
        "product_id": source.product_id,
        "batch_id": original.pk,
        "expected_version": original.version,
        "actor": active_user,
    }

    def write_batch():
        if operation == "create":
            return lifecycle.create_stock_batch(**command, data=batch_data(6))
        if operation == "edit":
            return lifecycle.update_stock_batch(**batch_command, data={"quantity_units": 9})
        return lifecycle.cancel_stock_batch(**batch_command)

    removing, writing = run_in_lock_order(
        lambda: lifecycle.remove_farm_product(**command),
        write_batch,
        left_first=remove_first,
    )

    original.refresh_from_db()
    if remove_first:
        assert removing.result().pk == source.pk
        code = "farm_product_version_conflict" if operation == "create" else "farm_product_removed"
        with pytest.raises(lifecycle.FarmConflict, match=f"^{code}$"):
            writing.result()
        assert (original.quantity_units, original.version, original.cancelled_at) == (10, 1, None)
        assert source.stock_batches.count() == 1
    elif operation == "create":
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_version_conflict$"):
            removing.result()
        created = writing.result()
        assert created.pk != original.pk
        assert (created.quantity_units, created.supply_generation) == (6, 1)
        assert source.stock_batches.count() == 2
    else:
        assert removing.result().pk == source.pk
        assert writing.result().pk == original.pk
        assert original.version == 2
        assert original.quantity_units == (9 if operation == "edit" else 10)
        assert (original.cancelled_at is not None) is (operation == "cancel")
        assert source.stock_batches.count() == 1
    removed = remove_first or operation != "create"
    assert_source_state(source, removed=removed, version=2)
    assert source_events(source, "remove") == int(removed)


@pytest.mark.parametrize("operation", ["remove", "readd"])
def test_concurrent_source_intents_apply_once(store_product, active_user, operation):
    source = store_product.farm_offers.get()
    original = source.stock_batches.get()
    if operation == "readd":
        source = lifecycle.remove_farm_product(**source_command(source, active_user))
    command = source_command(source, active_user)

    def apply_intent():
        if operation == "remove":
            return lifecycle.remove_farm_product(**command)
        return lifecycle.readd_farm_product(**command, data=batch_data(7))

    winning, stale = run_in_lock_order(apply_intent, apply_intent, left_first=True)

    result = winning.result()
    with pytest.raises(lifecycle.FarmConflict, match="^farm_product_version_conflict$"):
        stale.result()
    assert source_events(source, operation) == 1
    if operation == "remove":
        assert result.pk == source.pk
        assert_source_state(source, removed=True, version=2)
        assert source.stock_batches.count() == 1
        assert active_product_stock_totals([store_product.pk]).get(store_product.pk, 0) == 0
    else:
        readded, batch = result
        assert readded.pk == source.pk
        assert batch.pk != original.pk
        assert_source_state(source, removed=False, version=3, generation=2)
        assert source.stock_batches.count() == 2
        assert active_product_stock_totals([store_product.pk])[store_product.pk] == 7
    original.refresh_from_db()
    assert (original.quantity_units, original.supply_generation, original.cancelled_at) == (
        10,
        1,
        None,
    )


@pytest.mark.parametrize("readd_first", [True, False], ids=["readd-first", "discontinue-first"])
def test_readd_rechecks_product_status_after_waiting_for_lock(
    store_product, active_user, readd_first
):
    source = store_product.farm_offers.get()
    source = lifecycle.remove_farm_product(**source_command(source, active_user))
    command = source_command(source, active_user)

    readding, discontinuing = run_in_lock_order(
        lambda: lifecycle.readd_farm_product(**command, data=batch_data(7)),
        lambda: discontinue(store_product, active_user),
        left_first=readd_first,
    )

    discontinuing.result()
    store_product.refresh_from_db()
    assert store_product.status == "discontinued"
    if readd_first:
        readded, batch = readding.result()
        assert readded.pk == source.pk
        assert (batch.quantity_units, batch.supply_generation) == (7, 2)
        assert source.stock_batches.count() == 2
    else:
        with pytest.raises(lifecycle.FarmConflict, match="^product_not_selling$"):
            readding.result()
        assert source.stock_batches.count() == 1
    assert_source_state(
        source,
        removed=not readd_first,
        version=3 if readd_first else 2,
        generation=2 if readd_first else 1,
    )
    assert source_events(source, "readd") == int(readd_first)
