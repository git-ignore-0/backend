"""Force settlement, CSA and Farm status races against source lifecycle writes."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.db import connection

from commerce.models import Product, StoreOrder, StoreOrderStatus, StorePaymentStatus
from commerce.store_orders import (
    StoreOrderError,
    active_product_stock_totals,
    cancel_store_order,
    deliver_store_order,
    record_store_payment,
    restock_store_order,
)
from farms import services as lifecycle
from farms.models import Farm
from memberships import csa_rounds
from memberships.models import CSASelection, MembershipCycleQuota
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_lifecycle_locking import allocated_selection as allocated_selection
from tests.test_farm_lifecycle_locking import cancel_native_order
from tests.test_farm_source_lifecycle_mysql import (
    assert_source_state,
    batch_data,
    source_command,
    source_events,
)
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_product_lifecycle_locking import run_in_lock_order
from tests.test_store_orders import approve_review_order
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "mysql", reason="requires real MySQL row locks"),
]


@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "csa-first"])
@pytest.mark.parametrize("operation", ["reopen", "extend", "confirm"])
def test_remove_and_csa_reselection_decisions_keep_holds_covered(context, operation, remove_first):
    actor, product, membership, round_, _ = context
    source = product.farm_offers.get()
    selection = save_selection(context, quantity=2)
    now = round_.closes_at
    csa_rounds.process_round(round_.pk, now=now)
    selection.refresh_from_db()
    previous_usage = selection.current_usage
    deadline = csa_rounds.delivery_start(round_) - timedelta(hours=1)
    if operation != "reopen":
        csa_rounds.reopen_selection(
            selection_id=selection.pk, actor=actor, deadline=deadline - timedelta(hours=1), now=now
        )
        selection = save_selection(context, quantity=1, now=now)

    def change_selection():
        if operation == "confirm":
            return csa_rounds.confirm_selection(
                selection_id=selection.pk, user=actor, now=now, expected_revision=selection.revision
            )
        return csa_rounds.reopen_selection(
            selection_id=selection.pk,
            actor=actor,
            deadline=deadline,
            now=now,
            expected_revision=selection.revision,
        )

    removing, changing = run_in_lock_order(
        lambda: lifecycle.remove_farm_product(**source_command(source, actor)),
        change_selection,
        left_first=remove_first,
    )

    changed = changing.result()
    assert changed.pk == selection.pk
    assert changed.revision == selection.revision + 1
    assert changed.order.status == StoreOrderStatus.PENDING_REVIEW
    assert not changed.order.lines.get().stock_allocations.exists()
    removed = operation == "reopen" and not remove_first
    if removed:
        assert removing.result().pk == source.pk
    else:
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_supply_shortfall$"):
            removing.result()
    assert_source_state(source, removed=removed, version=2 if removed else 1)
    assert source_events(source, "remove") == int(removed)
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    previous_usage.refresh_from_db()
    if operation == "reopen":
        assert not changed.stock_holds.exists()
    else:
        assert changed.stock_holds.get(product=product).quantity == 1
    if operation == "confirm":
        assert changed.state == CSASelection.State.CONFIRMED
        assert changed.order.lines.get().quantity == 1
        assert changed.current_usage_id != previous_usage.pk
        assert previous_usage.status == "reversed"
        assert not changed.quota_holds.exists()
        assert quota.remaining_units == 3
    else:
        assert changed.state == CSASelection.State.RESELECTING
        assert changed.reopen_until == deadline
        assert changed.order.lines.get().quantity == 2
        assert changed.current_usage_id == previous_usage.pk
        assert previous_usage.status == "applied"
        assert quota.remaining_units == 2


@pytest.fixture
def paid_order(checkout_operation, active_user):
    order, _, created = checkout_operation()
    assert created
    approve_review_order({"id": order.pk}, active_user)
    return record_store_payment(order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4())


@pytest.mark.parametrize("remove_first", [True, False], ids=["remove-first", "cancel-first"])
def test_remove_and_store_cancellation_serialize_open_allocation(
    paid_order, active_user, remove_first
):
    allocation = paid_order.lines.get().stock_allocations.get()
    source = allocation.batch.farm_product

    removing, cancelling = run_in_lock_order(
        lambda: lifecycle.remove_farm_product(**source_command(source, active_user)),
        lambda: cancel_store_order(
            order_id=paid_order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            reason="Cancelled",
        ),
        left_first=remove_first,
    )

    cancelled = cancelling.result()
    assert cancelled.status == StoreOrderStatus.CANCELLED
    assert cancelled.payment_status == StorePaymentStatus.REFUND_REQUIRED
    assert cancelled.actions.filter(action="cancel").count() == 1
    assert sorted(allocation.movements.values_list("kind", "quantity")) == [
        ("restock", 2),
        ("sale", 2),
    ]
    if remove_first:
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_has_open_allocations$"):
            removing.result()
    else:
        assert removing.result().pk == source.pk
    assert_source_state(source, removed=not remove_first, version=1 if remove_first else 2)
    assert source_events(source, "remove") == int(not remove_first)


@pytest.mark.parametrize("source_first", [True, False], ids=["source-first", "restock-first"])
@pytest.mark.parametrize("transition", ["remove", "readd"])
def test_source_transition_and_manual_restock_keep_original_batch_scope(
    context, allocated_selection, transition, source_first
):
    actor, product, *_ = context
    cancelled = cancel_native_order(allocated_selection, actor)
    allocation = cancelled.lines.get().stock_allocations.get()
    original = allocation.batch
    source = original.farm_product
    if transition == "readd":
        source = lifecycle.remove_farm_product(**source_command(source, actor))

    def change_source():
        if transition == "readd":
            return lifecycle.readd_farm_product(**source_command(source, actor), data=batch_data(7))
        return lifecycle.remove_farm_product(**source_command(source, actor))

    changing, restocking = run_in_lock_order(
        change_source,
        lambda: restock_store_order(
            order_id=cancelled.pk,
            actor=actor,
            idempotency_key=uuid.uuid4(),
            allocations=[{"allocation_id": allocation.pk, "quantity": 2}],
        ),
        left_first=source_first,
    )

    if transition == "readd":
        readded, batch = changing.result()
        assert readded.pk == source.pk
        assert batch.pk != original.pk
        assert not batch.store_stock_movements.exists()
        assert active_product_stock_totals([product.pk])[product.pk] == 7
        assert_source_state(source, removed=False, version=3, generation=2)
    else:
        assert changing.result().pk == source.pk
        assert active_product_stock_totals([product.pk]).get(product.pk, 0) == 0
        assert_source_state(source, removed=True, version=2)
    accepted = transition == "remove" and not source_first
    if accepted:
        assert restocking.result().pk == cancelled.pk
        assert allocation.movements.get(kind="restock").batch_id == original.pk
    else:
        with pytest.raises(StoreOrderError, match="^store_restock_invalid$") as error:
            restocking.result()
        expected = (
            "batch_historical" if transition == "readd" and source_first else "offer_inactive"
        )
        assert error.value.context["reason"] == expected
        assert not allocation.movements.filter(kind="restock").exists()
    assert cancelled.actions.filter(action="restock").count() == int(accepted)


@pytest.mark.parametrize(
    "remove_first", [True, False], ids=["product-lock-first", "delivery-write-first"]
)
def test_remove_and_delivery_use_only_committed_terminal_state(
    paid_order, active_user, remove_first
):
    allocation = paid_order.lines.get().stock_allocations.get()
    source = allocation.batch.farm_product
    paused, completed = Event(), Event()

    def remove():
        return lifecycle.remove_farm_product(**source_command(source, active_user))

    def deliver():
        return deliver_store_order(
            order_id=paid_order.pk, actor=active_user, idempotency_key=uuid.uuid4()
        )

    def run(operation, *, first):
        product_table = connection.ops.quote_name(Product._meta.db_table).upper()
        order_table = connection.ops.quote_name(StoreOrder._meta.db_table).upper()

        def hold_after_write(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            sql = sql.upper()
            at_barrier = (
                f"FROM {product_table}" in sql and "FOR UPDATE" in sql
                if remove_first
                else sql.startswith(f"UPDATE {order_table} SET")
            )
            if at_barrier and not paused.is_set():
                paused.set()
                assert completed.wait(10)
            return result

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 10")
            if first:
                with connection.execute_wrapper(hold_after_write):
                    return operation()
            assert paused.wait(10)
            return operation()
        finally:
            if not first:
                completed.set()
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        removing = pool.submit(run, remove, first=remove_first)
        delivering = pool.submit(run, deliver, first=not remove_first)
        assert delivering.result(timeout=25).status == StoreOrderStatus.DELIVERED
        if remove_first:
            assert removing.result(timeout=25).pk == source.pk
        else:
            with pytest.raises(lifecycle.FarmConflict, match="^farm_product_has_open_allocations$"):
                removing.result(timeout=25)
    assert_source_state(source, removed=remove_first, version=2 if remove_first else 1)
    assert paid_order.actions.filter(action="deliver").count() == 1
    assert list(allocation.movements.values_list("kind", "quantity")) == [("sale", 2)]


@pytest.mark.parametrize("source_first", [True, False], ids=["source-first", "farm-first"])
@pytest.mark.parametrize("transition", ["remove", "readd"])
def test_farm_deactivation_and_source_transition_keep_availability_derived(
    store_product, active_user, transition, source_first
):
    source = store_product.farm_offers.get()
    if transition == "readd":
        source = lifecycle.remove_farm_product(**source_command(source, active_user))
    farm = source.farm

    def change_source():
        if transition == "readd":
            return lifecycle.readd_farm_product(
                **source_command(source, active_user), data=batch_data(7)
            )
        return lifecycle.remove_farm_product(**source_command(source, active_user))

    changing, deactivating = run_in_lock_order(
        change_source,
        lambda: lifecycle.update_farm(
            farm_id=farm.pk,
            actor=active_user,
            expected_version=farm.version,
            data={"is_active": False},
        ),
        left_first=source_first,
    )

    changing.result()
    deactivated = deactivating.result()
    assert deactivated.pk == farm.pk
    assert deactivated.is_active is False
    assert active_product_stock_totals([store_product.pk]).get(store_product.pk, 0) == 0
    lifecycle.update_farm(
        farm_id=farm.pk,
        actor=active_user,
        expected_version=deactivated.version,
        data={"is_active": True},
    )
    readded = transition == "readd"
    assert_source_state(
        source, removed=not readded, version=3 if readded else 2, generation=2 if readded else 1
    )
    assert active_product_stock_totals([store_product.pk]).get(store_product.pk, 0) == (
        7 if readded else 0
    )


@pytest.mark.parametrize("add_first", [True, False], ids=["add-first", "farm-first"])
def test_farm_deactivation_rechecks_sources_attached_while_waiting(
    store_product, active_user, add_first
):
    farm = Farm.objects.create(name_en="Second farm", name_vi="Nông trại thứ hai")

    adding, deactivating = run_in_lock_order(
        lambda: lifecycle.add_farm_product(
            farm_id=farm.pk, product_id=store_product.pk, actor=active_user, **batch_data(7)
        ),
        lambda: lifecycle.update_farm(
            farm_id=farm.pk,
            actor=active_user,
            expected_version=farm.version,
            data={"is_active": False},
        ),
        left_first=add_first,
        lock_model=Farm,
    )

    source, batch = adding.result()
    if add_first:
        with pytest.raises(lifecycle.FarmConflict, match="^farm_product_changed$"):
            deactivating.result()
    else:
        assert deactivating.result().is_active is False
    farm.refresh_from_db()
    assert farm.is_active is add_first
    assert source.farm_id == farm.pk
    assert source.is_active
    assert (batch.farm_product_id, batch.quantity_units) == (source.pk, 7)
    assert active_product_stock_totals([store_product.pk])[store_product.pk] == (
        17 if add_first else 10
    )
