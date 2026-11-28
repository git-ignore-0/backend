"""Bulk removal races through real row locks on isolated MySQL."""

from decimal import Decimal
from functools import partial
from uuid import uuid4

import pytest
from django.db import connection

from commerce.models import StoreOrder, StoreOrderStatus, StoreStockMovement, StoreStockReservation
from commerce.store_orders import StoreOrderError, active_product_stock_totals, approve_store_order
from content.creation_receipts import CreationIdempotencyConflict
from content.models import AdminCreationReceipt
from farms import services
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships import csa_rounds
from memberships.models import CSAQuotaHold, CSASelection, CSAStockHold
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_bulk_supply_mysql import run_at_receipt_insert
from tests.test_farm_source_lifecycle_mysql import (
    assert_source_state,
    batch_data,
    source_command,
    source_events,
)
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_product_lifecycle_locking import assert_csa_holds, run_in_lock_order
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "mysql" or not connection.features.has_select_for_update,
        reason="Requires isolated MySQL row locks",
    ),
]


def make_source(farm, sku="BULK-REMOVE-OTHER"):
    return FarmProduct.objects.create(farm=farm, product=make_product(sku))


def remove_operation(sources, actor, *, key=None):
    return partial(
        services.remove_farm_products_bulk,
        farm_id=sources[0].farm_id,
        actor=actor,
        idempotency_key=key or uuid4(),
        items=[
            {
                "product_id": source.product_id,
                "expected_link_id": source.pk,
                "expected_version": source.version,
            }
            for source in sources
        ],
    )


def removal_receipts():
    return AdminCreationReceipt.objects.filter(
        action=AdminCreationReceipt.Action.REMOVE_FARM_PRODUCTS
    )


def assert_committed(result, farm_id, count):
    receipt, created = result.result()
    assert created is True
    assert (receipt.resource_id, receipt.created_count) == (farm_id, count)
    assert removal_receipts().get().pk == receipt.pk
    return receipt


def assert_item_conflict(result, code, source, *, index):
    with pytest.raises(services.FarmConflict, match=f"^{code}$") as caught:
        result.result()
    context = caught.value.context
    assert (context["item_index"], context["product_id"]) == (index, str(source.product_id))
    return context


def assert_removal(source, *, removed, version):
    assert_source_state(source, removed=removed, version=version)
    assert source_events(source, "remove") == int(removed)


@pytest.mark.parametrize("left_first", [True, False], ids=["left-first", "right-first"])
@pytest.mark.parametrize("race", ["duplicate", "same-sources", "overlap"])
def test_concurrent_bulk_removals_apply_only_the_winning_set(active_user, left_first, race):
    farm = make_farm()
    first, second = [make_source(farm, f"SHARED-{index}") for index in range(2)]
    left_sources, right_sources = [first, second], [second, first]
    if race == "overlap":
        left_sources.insert(0, make_source(farm, "LEFT"))
        right_sources.insert(0, make_source(farm, "RIGHT"))
    replay_key = uuid4() if race == "duplicate" else None
    left = remove_operation(left_sources, active_user, key=replay_key)
    right = remove_operation(right_sources, active_user, key=replay_key)

    results = run_in_lock_order(left, right, left_first=left_first)

    winner, loser = results if left_first else reversed(results)
    winning_sources = left_sources if left_first else right_sources
    receipt = assert_committed(winner, farm.pk, len(winning_sources))
    if race == "duplicate":
        replay, created = loser.result()
        assert created is False
        assert replay.pk == receipt.pk
    else:
        assert_item_conflict(
            loser,
            "farm_product_version_conflict",
            second if left_first else first,
            index=int(race == "overlap"),
        )
    for source in winning_sources:
        assert_removal(source, removed=True, version=2)
    if race == "overlap":
        untouched = right_sources[0] if left_first else left_sources[0]
        assert_removal(untouched, removed=False, version=1)


def test_same_key_disjoint_bulk_removals_contend_at_receipt_insert(active_user):
    farm = make_farm()
    sources = [make_source(farm, f"DISJOINT-{index}") for index in range(2)]
    key = uuid4()
    left, right = [remove_operation([source], active_user, key=key) for source in sources]

    results = run_at_receipt_insert(left, right)

    committed = []
    for source, result in zip(sources, results, strict=True):
        try:
            assert_committed(result, farm.pk, 1)
        except CreationIdempotencyConflict:
            assert_removal(source, removed=False, version=1)
        else:
            committed.append(source.pk)
            assert_removal(source, removed=True, version=2)
    assert len(committed) == 1


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "single-first"])
def test_bulk_and_single_remove_serialize_on_shared_source(active_user, bulk_first):
    farm = make_farm()
    source, other = make_source(farm, "SINGLE"), make_source(farm)
    bulk, single = run_in_lock_order(
        remove_operation([other, source], active_user),
        partial(services.remove_farm_product, **source_command(source, active_user)),
        left_first=bulk_first,
    )

    if bulk_first:
        assert_committed(bulk, farm.pk, 2)
        with pytest.raises(services.FarmConflict, match="^farm_product_version_conflict$"):
            single.result()
    else:
        assert single.result().pk == source.pk
        assert_item_conflict(bulk, "farm_product_version_conflict", source, index=1)
        assert not removal_receipts().exists()
    assert_removal(source, removed=True, version=2)
    assert_removal(other, removed=bulk_first, version=2 if bulk_first else 1)


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "readd-first"])
def test_bulk_remove_rechecks_source_state_during_readd(store_product, active_user, bulk_first):
    source = store_product.farm_offers.get()
    batch_id = source.stock_batches.get().pk
    source = services.remove_farm_product(**source_command(source, active_user))
    other = make_source(source.farm)

    bulk, readd = run_in_lock_order(
        remove_operation([other, source], active_user),
        partial(
            services.readd_farm_product,
            **source_command(source, active_user),
            idempotency_key=uuid4(),
        ),
        left_first=bulk_first,
    )

    code = "farm_product_removed" if bulk_first else "farm_product_version_conflict"
    assert_item_conflict(bulk, code, source, index=1)
    receipt, created = readd.result()
    assert created is True
    assert receipt.resource_id == source.pk
    assert_source_state(source, removed=False, version=3, generation=2)
    assert source.stock_batches.get().pk == batch_id
    assert active_product_stock_totals([source.product_id]).get(source.product_id, 0) == 0
    assert source_events(source, "readd") == source_events(source, "remove") == 1
    assert_removal(other, removed=False, version=1)
    assert not removal_receipts().exists()


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "batch-first"])
@pytest.mark.parametrize("operation", ["create", "update", "cancel"])
def test_bulk_remove_serializes_with_stock_batch_writers(
    store_product, active_user, bulk_first, operation
):
    source = store_product.farm_offers.get()
    other = make_source(source.farm)
    batch = source.stock_batches.get()
    batch_command = {
        "farm_id": source.farm_id,
        "product_id": source.product_id,
        "batch_id": batch.pk,
        "expected_version": batch.version,
        "actor": active_user,
    }
    if operation == "create":
        write = partial(
            services.create_stock_batch, **source_command(source, active_user), data=batch_data(6)
        )
    elif operation == "update":
        write = partial(services.update_stock_batch, **batch_command, data={"quantity_units": 9})
    else:
        write = partial(services.cancel_stock_batch, **batch_command)

    bulk, batch_write = run_in_lock_order(
        remove_operation([other, source], active_user), write, left_first=bulk_first
    )

    if bulk_first:
        code = "farm_product_version_conflict" if operation == "create" else "farm_product_removed"
        with pytest.raises(services.FarmConflict, match=f"^{code}$"):
            batch_write.result()
    else:
        written = batch_write.result()
        if operation == "create":
            assert written.pk != batch.pk
            assert (written.quantity_units, written.supply_generation) == (6, 1)
        else:
            assert written.pk == batch.pk
    created_before_remove = not bulk_first and operation == "create"
    if created_before_remove:
        assert_item_conflict(bulk, "farm_product_version_conflict", source, index=1)
        assert not removal_receipts().exists()
    else:
        assert_committed(bulk, source.farm_id, 2)
    batch.refresh_from_db()
    assert (batch.quantity_units, batch.version, batch.cancelled_at is not None) == (
        9 if not bulk_first and operation == "update" else 10,
        2 if not bulk_first and operation != "create" else 1,
        not bulk_first and operation == "cancel",
    )
    assert source.stock_batches.count() == (2 if created_before_remove else 1)
    assert_removal(source, removed=not created_before_remove, version=2)
    assert_removal(
        other, removed=not created_before_remove, version=1 if created_before_remove else 2
    )


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "checkout-first"])
def test_bulk_remove_preserves_concurrent_checkout_reservations(
    store_product, active_user, checkout_operation, bulk_first
):
    source = store_product.farm_offers.get()
    other = make_source(source.farm)
    bulk, checkout = run_in_lock_order(
        remove_operation([other, source], active_user), checkout_operation, left_first=bulk_first
    )

    if bulk_first:
        assert_committed(bulk, source.farm_id, 2)
        with pytest.raises(StoreOrderError, match="^store_requote_required$") as caught:
            checkout.result()
        assert caught.value.context["available_quantity"] == 0
        assert not StoreOrder.objects.exists()
        assert not StoreStockReservation.objects.exists()
    else:
        order, _, created = checkout.result()
        assert created is True
        reservation = order.lines.get().stock_reservations.get()
        assert (reservation.farm_id, reservation.quantity, reservation.released_at) == (
            source.farm_id,
            2,
            None,
        )
        assert_item_conflict(bulk, "farm_product_supply_shortfall", source, index=1)
        assert not removal_receipts().exists()
    for candidate in (source, other):
        assert_removal(candidate, removed=bulk_first, version=2 if bulk_first else 1)
    assert not StoreStockMovement.objects.exists()


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "approval-first"])
def test_bulk_remove_preserves_assigned_then_allocated_stock(
    store_product, active_user, checkout_operation, bulk_first
):
    order, _, created = checkout_operation()
    assert created is True
    line = order.lines.get()
    source = store_product.farm_offers.get()
    other = make_source(source.farm)
    batch = source.stock_batches.get()
    backup = FarmProduct.objects.create(farm=make_farm("Backup"), product=store_product)
    FarmProductStockBatch.objects.create(farm_product=backup, **batch_data(10))
    approval_key = uuid4()

    bulk, approval = run_in_lock_order(
        remove_operation([other, source], active_user),
        partial(
            approve_store_order,
            order_id=order.pk,
            actor=active_user,
            idempotency_key=approval_key,
            allocations=[{"line_id": line.pk, "batch_id": batch.pk, "quantity": 2}],
        ),
        left_first=bulk_first,
    )

    code = "farm_product_supply_shortfall" if bulk_first else "farm_product_has_open_allocations"
    blocker = assert_item_conflict(bulk, code, source, index=1)
    if bulk_first:
        assert blocker["shortfall_units"] == 2
    else:
        assert blocker["orders"] == [{"id": str(order.pk), "reference_code": order.reference_code}]
    assert approval.result().status == StoreOrderStatus.PAYMENT_PENDING
    assert line.stock_reservations.get().released_at is not None
    allocation = line.stock_allocations.get()
    assert (allocation.batch_id, allocation.quantity) == (batch.pk, 2)
    assert list(allocation.movements.values_list("kind", "quantity")) == [("sale", 2)]
    assert order.actions.filter(action="approve", idempotency_key=approval_key).count() == 1
    for candidate in (source, other, backup):
        assert_removal(candidate, removed=False, version=1)
    assert not removal_receipts().exists()


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "hold-first"])
def test_bulk_remove_preserves_concurrent_csa_holds(context, bulk_first):
    actor, product, _, _, _ = context
    source = product.farm_offers.get()
    other = make_source(source.farm)
    bulk, selection = run_in_lock_order(
        remove_operation([other, source], actor),
        partial(save_selection, context),
        left_first=bulk_first,
    )

    if bulk_first:
        assert_committed(bulk, source.farm_id, 2)
        with pytest.raises(csa_rounds.CSARoundError, match="^csa_stock_insufficient$"):
            selection.result()
        assert not CSASelection.objects.exists()
        assert not CSAStockHold.objects.exists()
        assert not CSAQuotaHold.objects.exists()
    else:
        selected = selection.result()
        assert_csa_holds(selected, product, cost=Decimal("1"))
        assert selected.stock_holds.get(product=product).farm_id == source.farm_id
        assert_item_conflict(bulk, "farm_product_supply_shortfall", source, index=1)
        assert not removal_receipts().exists()
    for candidate in (source, other):
        assert_removal(candidate, removed=bulk_first, version=2 if bulk_first else 1)


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "deactivate-first"])
def test_bulk_remove_and_farm_deactivation_remain_independent(
    store_product, active_user, bulk_first
):
    source = store_product.farm_offers.get()
    farm = source.farm
    other = make_source(farm)
    bulk, deactivation = run_in_lock_order(
        remove_operation([other, source], active_user),
        partial(
            services.update_farm,
            farm_id=farm.pk,
            actor=active_user,
            expected_version=farm.version,
            data={"is_active": False},
        ),
        left_first=bulk_first,
    )

    assert_committed(bulk, farm.pk, 2)
    assert deactivation.result().is_active is False
    farm.refresh_from_db()
    assert (farm.is_active, farm.version) == (False, 2)
    for candidate in (source, other):
        assert_removal(candidate, removed=True, version=2)
    assert source.stock_batches.count() == 1
    assert active_product_stock_totals([source.product_id]).get(source.product_id, 0) == 0


@pytest.mark.parametrize("bulk_first", [True, False], ids=["bulk-first", "delete-first"])
def test_farm_delete_rechecks_sources_after_bulk_remove(active_user, bulk_first):
    farm = make_farm()
    sources = [make_source(farm, f"DELETE-{index}") for index in range(2)]
    bulk, deletion = run_in_lock_order(
        remove_operation(sources, active_user),
        partial(services.delete_farm, farm_id=farm.pk, actor=active_user),
        left_first=bulk_first,
    )

    assert_committed(bulk, farm.pk, 2)
    if bulk_first:
        deletion.result()
        assert not Farm.objects.filter(pk=farm.pk).exists()
        assert not FarmProduct.objects.filter(farm_id=farm.pk).exists()
        for source in sources:
            assert source_events(source, "remove") == 1
    else:
        with pytest.raises(services.FarmConflict, match="^farm_in_use$"):
            deletion.result()
        assert Farm.objects.filter(pk=farm.pk).exists()
        for source in sources:
            assert_removal(source, removed=True, version=2)
