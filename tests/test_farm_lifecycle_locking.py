import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest
from django.db import connection
from django.db.models.query import QuerySet

from commerce import store_orders
from commerce.models import (
    CatalogTreeLock,
    Product,
    ShippingAddress,
    StoreOrder,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
)
from commerce.serializers import StoreCheckoutSerializer
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships import csa_rounds
from memberships.models import CSASelection, Membership, MembershipCycleQuota
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import approve_review_order, quota_checkout_payload
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area
from tests.test_store_orders import zero_shipping_fee as zero_shipping_fee

pytestmark = pytest.mark.django_db


@pytest.fixture
def allocated_selection(context):
    user, _, membership, round_, _ = context
    selection = save(context, quantity=2)
    csa_rounds.process_round(round_.pk, now=round_.closes_at)
    selection.refresh_from_db()
    approve_review_order({"id": selection.order_id}, user)
    assert MembershipCycleQuota.objects.get(cycle__membership=membership).remaining_units == 2
    assert not selection.order.lines.get().quota_reservations.exists()
    return selection


def record_resource_locks(monkeypatch):
    locks = []
    fetch = QuerySet._fetch_all

    def record(rows):
        should_record = rows._result_cache is None and rows.query.select_for_update
        fetch(rows)
        if should_record and rows._result_cache:
            locks.append((rows.model, [row.pk for row in rows._result_cache], rows.query))

    monkeypatch.setattr(QuerySet, "_fetch_all", record)
    return locks


def cancel_native_order(selection, user, *, key=None):
    return store_orders.cancel_store_order(
        order_id=selection.order_id,
        actor=user,
        idempotency_key=key or uuid.uuid4(),
        reason="No harvest",
    )


def assert_native_cancellation(selection, usage):
    selection.refresh_from_db()
    assert selection.state == CSASelection.State.CANCELLED
    assert selection.order.status == StoreOrderStatus.CANCELLED
    assert selection.current_usage_id is None
    assert not selection.stock_holds.exists()
    assert not selection.quota_holds.exists()
    assert selection.events.filter(action="cancelled").count() == 1
    usage.refresh_from_db()
    assert usage.status == "reversed"
    assert usage.reversal.reason == "No harvest"
    assert selection.order.actions.filter(action="cancel").count() == 1
    assert list(
        StoreStockMovement.objects.filter(line__order=selection.order).values_list(
            "kind", "quantity"
        )
    ) == [("sale", 2)]
    quota = MembershipCycleQuota.objects.get(cycle__membership=selection.membership)
    assert quota.remaining_units == 4


def test_native_csa_cancellation_locks_membership_before_allocated_sources(
    context, allocated_selection, monkeypatch
):
    user, product, membership, _, _ = context
    allocation = allocated_selection.order.lines.get().stock_allocations.get()
    batch = allocation.batch
    usage = allocated_selection.current_usage
    locks = record_resource_locks(monkeypatch)
    key = uuid.uuid4()

    cancel_native_order(allocated_selection, user, key=key)

    assert [(model, ids) for model, ids, _ in locks[:6]] == [
        (StoreOrder, [allocated_selection.order_id]),
        (Product, [product.pk]),
        (Membership, [membership.pk]),
        (FarmProduct, [batch.farm_product_id]),
        (Farm, [batch.farm_product.farm_id]),
        (FarmProductStockBatch, [batch.pk]),
    ]
    assert not any(model is Membership for model, _, _ in locks[6:])
    cancel_native_order(allocated_selection, user, key=key)
    assert_native_cancellation(allocated_selection, usage)


def test_manual_restock_locks_product_before_sources_and_replays(
    context, allocated_selection, monkeypatch
):
    user, product, _, _, _ = context
    cancelled = cancel_native_order(allocated_selection, user)
    allocation = cancelled.lines.get().stock_allocations.get()
    batch = allocation.batch
    locks = record_resource_locks(monkeypatch)
    arguments = {
        "order_id": cancelled.pk,
        "actor": user,
        "idempotency_key": uuid.uuid4(),
        "allocations": [{"allocation_id": allocation.pk, "quantity": 2}],
    }

    store_orders.restock_store_order(**arguments)

    assert [(model, ids) for model, ids, _ in locks[:5]] == [
        (StoreOrder, [cancelled.pk]),
        (Product, [product.pk]),
        (FarmProduct, [batch.farm_product_id]),
        (Farm, [batch.farm_product.farm_id]),
        (FarmProductStockBatch, [batch.pk]),
    ]
    assert all(len(query.alias_map) == 1 for _, _, query in locks)
    store_orders.restock_store_order(**arguments)
    with pytest.raises(store_orders.StoreOrderError, match="store_restock_invalid"):
        store_orders.restock_store_order(**{**arguments, "idempotency_key": uuid.uuid4()})
    assert StoreStockMovement.objects.get(allocation=allocation, kind="restock").quantity == 2
    assert cancelled.actions.filter(action="restock").count() == 1


def second_product_at_same_farm(product):
    first_batch = FarmProductStockBatch.objects.get(farm_product__product=product)
    farm = first_batch.farm_product.farm
    other = Product.objects.get(pk=product.pk)
    other.pk = uuid.uuid4()
    other.sku = "CARROTS"
    other.slug = "carrots"
    other.save(force_insert=True)
    offer = FarmProduct.objects.create(farm=farm, product=other)
    return FarmProductStockBatch.objects.create(
        farm_product=offer,
        start_date=first_batch.start_date,
        end_date=first_batch.end_date,
        quantity_units=10,
    )


def test_manual_restock_rejects_mismatched_source_before_locking_it(
    context, allocated_selection, monkeypatch
):
    user, product, _, _, _ = context
    foreign_batch = second_product_at_same_farm(product)
    cancelled = cancel_native_order(allocated_selection, user)
    line = cancelled.lines.get()
    original = line.stock_allocations.get()
    mismatched = StoreStockAllocation.objects.create(line=line, batch=foreign_batch, quantity=1)
    locks = record_resource_locks(monkeypatch)

    with pytest.raises(store_orders.StoreOrderError, match="store_restock_invalid"):
        store_orders.restock_store_order(
            order_id=cancelled.pk,
            actor=user,
            idempotency_key=uuid.uuid4(),
            allocations=[
                {"allocation_id": original.pk, "quantity": 1},
                {"allocation_id": mismatched.pk, "quantity": 1},
            ],
        )

    assert not any(model in {FarmProduct, Farm, FarmProductStockBatch} for model, _, _ in locks)
    assert not StoreStockMovement.objects.filter(line=line, kind="restock").exists()
    assert not cancelled.actions.filter(action="restock").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
@pytest.mark.parametrize("winner", ["cancel", "checkout"])
def test_mysql_native_csa_cancel_and_store_checkout_serialize_on_common_membership(
    context, allocated_selection, zero_shipping_fee, winner
):
    user, product, _, round_, _ = context
    other = second_product_at_same_farm(product).farm_product.product
    usage = allocated_selection.current_usage
    CatalogTreeLock.objects.get_or_create(pk=1)
    payload = StoreCheckoutSerializer(
        data=quota_checkout_payload(other, ShippingAddress.objects.get(user=user), quantity=1)
    )
    payload.is_valid(raise_exception=True)
    assert payload.validated_data["delivery_date"] != round_.delivery_date

    winner_locked = Event()
    membership_barrier = Barrier(2)
    membership_table = connection.ops.quote_name(Membership._meta.db_table)

    def run(operation):
        at_first_membership_lock = True

        def synchronize_membership_lock(execute, sql, params, many, execution_context):
            nonlocal at_first_membership_lock
            if (
                not at_first_membership_lock
                or f"FROM {membership_table}" not in sql
                or "FOR UPDATE" not in sql
            ):
                return execute(sql, params, many, execution_context)
            at_first_membership_lock = False
            if operation == winner:
                result = execute(sql, params, many, execution_context)
                winner_locked.set()
                membership_barrier.wait(timeout=10)
                return result
            membership_barrier.wait(timeout=10)
            return execute(sql, params, many, execution_context)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 10")
                cursor.execute("SELECT CONNECTION_ID()")
                session_id = cursor.fetchone()[0]
            if operation != winner:
                assert winner_locked.wait(timeout=10)
            with connection.execute_wrapper(synchronize_membership_lock):
                if operation == "cancel":
                    order = cancel_native_order(allocated_selection, user)
                else:
                    order, _, created = store_orders.create_store_checkout(
                        user=user, payload=payload.validated_data
                    )
                    assert created
            return session_id, order.pk
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation = pool.submit(run, "cancel")
        purchase = pool.submit(run, "checkout")
        cancel_session, cancelled_id = cancellation.result(timeout=30)
        checkout_session, checkout_id = purchase.result(timeout=30)

    assert cancel_session != checkout_session
    assert cancelled_id == allocated_selection.order_id
    assert_native_cancellation(allocated_selection, usage)
    checkout = StoreOrder.objects.get(pk=checkout_id)
    assert checkout.status == StoreOrderStatus.PENDING_REVIEW
    line = checkout.lines.get(product=other)
    hold = line.stock_reservations.get()
    assert hold.quantity == 1
    assert hold.released_at is None
    reserved_quota = line.quota_reservations.get()
    assert reserved_quota.units == 1
    assert reserved_quota.consumed_at is None
    assert reserved_quota.released_at is None
    assert not line.stock_allocations.exists()
