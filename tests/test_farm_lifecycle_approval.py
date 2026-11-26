import uuid
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import (
    Product,
    ShippingAddress,
    StoreOrder,
    StoreOrderAction,
    StoreOrderStatus,
    StoreQuotaReservation,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockReservation,
)
from commerce.store_orders import StoreOrderError, approve_store_order
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships import csa_rounds
from memberships.models import MembershipCycleQuota, MembershipUsageOrder
from tests.test_store_orders import attach_active_membership, guest_payload, quota_checkout_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area
from tests.test_store_orders import zero_shipping_fee as zero_shipping_fee

pytestmark = pytest.mark.django_db


@pytest.fixture
def inventory(store_product, monkeypatch):
    now = datetime.combine(timezone.localdate(), time(23, 58), ZoneInfo("Asia/Ho_Chi_Minh"))
    clock = SimpleNamespace(now=now, next_day=now + timedelta(minutes=2))
    monkeypatch.setattr(timezone, "now", lambda: clock.now)
    ending = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    ending.end_date = now.date()
    ending.save(update_fields=["end_date"])
    remaining = FarmProductStockBatch.objects.create(
        farm_product=ending.farm_product,
        start_date=now.date(),
        end_date=now.date() + timedelta(days=2),
        quantity_units=10,
    )
    return clock, remaining


def _place_order(payload, *, buyer=None):
    client = APIClient()
    if buyer is not None:
        client.force_authenticate(buyer, token=SimpleNamespace(scope="profile email"))
    payload["delivery_date"] = timezone.localdate() + timedelta(days=1)
    response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.json()
    return StoreOrder.objects.get(pk=response.json()["data"]["id"])


def _second_product(original, batch):
    product = Product.objects.create(
        category=original.category,
        sku="APPROVAL-CARROT",
        slug="approval-carrot",
        unit_size=original.unit_size,
        unit_label_vi=original.unit_label_vi,
        unit_label_en=original.unit_label_en,
        retail_price_vnd=original.retail_price_vnd,
        status="active",
        is_published=True,
    )
    offer = FarmProduct.objects.create(farm_id=batch.farm_product.farm_id, product=product)
    return FarmProductStockBatch.objects.create(
        farm_product=offer,
        start_date=batch.start_date,
        end_date=batch.end_date,
        quantity_units=10,
    )


def _allocation(order, batch, *, quantity=10):
    return {
        "line_id": order.lines.get(product_id=batch.farm_product.product_id).pk,
        "batch_id": batch.pk,
        "quantity": quantity,
    }


def _assert_pending_without_sales(order):
    order.refresh_from_db()
    assert order.status == StoreOrderStatus.PENDING_REVIEW
    assert order.approved_at is None
    assert not StoreOrderAction.objects.filter(order=order, action="approve").exists()
    assert not StoreStockAllocation.objects.filter(line__order=order).exists()
    assert not StoreStockMovement.objects.filter(line__order=order).exists()


def test_approval_preserves_other_store_reservation_after_crossing_vietnam_midnight(
    active_user, store_product, inventory, payment_settings, monkeypatch
):
    clock, remaining = inventory
    other_batch = _second_product(store_product, remaining)
    payload = guest_payload(store_product, quantity=10, expected_available=20)
    payload["lines"].insert(
        0,
        {
            **payload["lines"][0],
            "product_id": str(other_batch.farm_product.product_id),
            "expected_available_quantity": 10,
        },
    )
    payload["expected_total_vnd"] = 930_000
    order = _place_order(payload)
    other = _place_order(guest_payload(store_product, quantity=10))
    real_fetch = QuerySet._fetch_all

    def fetch_after_wait(queryset):
        result = real_fetch(queryset)
        if queryset.model is FarmProductStockBatch and queryset.query.select_for_update:
            clock.now = clock.next_day
        return result

    allocations = [_allocation(order, other_batch), _allocation(order, remaining)]
    key = uuid.uuid4()
    with monkeypatch.context() as patch:
        patch.setattr(QuerySet, "_fetch_all", fetch_after_wait)
        with pytest.raises(StoreOrderError, match="store_allocation_stale") as failure:
            approve_store_order(
                order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
            )

    assert failure.value.context == {"product_id": str(store_product.pk), "available_quantity": 0}
    _assert_pending_without_sales(order)
    assert (
        StoreStockReservation.objects.filter(
            line__order__in=[order, other], released_at__isnull=True
        ).count()
        == 3
    )

    # The unprocessed, expired Store demand still has a reservation row.
    StoreOrder.objects.filter(pk=other.pk).update(expires_at=clock.now)
    approved = approve_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
    )
    assert approved.approved_at == clock.next_day
    assert StoreStockMovement.objects.filter(line__order=order).count() == 2
    assert not StoreStockReservation.objects.filter(line__order=order, released_at=None).exists()
    assert StoreStockReservation.objects.filter(line__order=other, released_at=None).exists()

    clock.now += timedelta(days=3)
    replay = approve_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
    )
    assert replay.approved_at == approved.approved_at
    assert StoreOrderAction.objects.filter(order=order, action="approve").count() == 1
    assert StoreStockMovement.objects.filter(line__order=order).count() == 2


@pytest.fixture
def membership(active_user, store_product, inventory):
    membership = attach_active_membership(active_user, store_product)
    store_product.csa_quota_cost = 1
    store_product.save(update_fields=["csa_quota_cost"])
    MembershipCycleQuota.objects.filter(cycle__membership=membership).update(
        granted_units=40, remaining_units=40
    )
    return membership


def _select_csa(user, product):
    now = timezone.now()
    configuration = csa_rounds.create_round_configuration(
        name="CSA coverage",
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(minutes=1),
        delivery_date=timezone.localdate(now) + timedelta(days=2),
        actor=user,
    )
    round_ = csa_rounds.publish_round_configuration(configuration_id=configuration.pk).rounds.get()
    return csa_rounds.save_selection(
        round_id=round_.pk, user=user, lines=[{"product_id": product.pk, "quantity": 10}]
    )


@pytest.mark.parametrize("other_hold", ["active", "expired", "released"])
def test_store_approval_only_respects_effective_csa_holds(
    active_user, store_product, inventory, membership, zero_shipping_fee, other_hold
):
    clock, remaining = inventory
    selection = _select_csa(active_user, store_product)
    order = _place_order(
        quota_checkout_payload(
            store_product, ShippingAddress.objects.get(user=active_user), quantity=10
        ),
        buyer=active_user,
    )
    if other_hold == "expired":
        close = selection.round.closes_at
        csa_rounds.process_round(selection.round_id, now=close)
        csa_rounds.reopen_selection(
            selection_id=selection.pk, actor=active_user, deadline=clock.next_day, now=close
        )
        csa_rounds.save_selection(
            round_id=selection.round_id,
            user=active_user,
            lines=[{"product_id": store_product.pk, "quantity": 10}],
            now=close,
        )
    elif other_hold == "released":
        csa_rounds.cancel_selection(selection_id=selection.pk, actor=active_user, reason="Released")

    clock.now = clock.next_day
    change_quota = StoreQuotaReservation.objects.get(line__order=order)
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    allocations = [_allocation(order, remaining)]
    key = uuid.uuid4()
    if other_hold == "active":
        with pytest.raises(StoreOrderError, match="store_allocation_stale"):
            approve_store_order(
                order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
            )
        _assert_pending_without_sales(order)
        change_quota.refresh_from_db()
        quota.refresh_from_db()
        assert change_quota.consumed_at is None and change_quota.released_at is None
        assert quota.remaining_units == 40
        assert not MembershipUsageOrder.objects.filter(store_order=order).exists()
        assert StoreStockReservation.objects.get(line__order=order).released_at is None
        assert selection.stock_holds.get().quantity == 10
        return

    approved = approve_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
    )
    assert approved.status == StoreOrderStatus.PENDING_DELIVERY
    assert StoreStockMovement.objects.get(line__order=order).quantity == 10
    assert selection.stock_holds.exists() is (other_hold == "expired")


def test_csa_approval_protects_other_csa_hold_and_releases_its_own(
    active_user, store_product, inventory, membership, payment_settings
):
    clock, remaining = inventory
    remaining.quantity_units = 20
    remaining.save(update_fields=["quantity_units"])
    previous = _place_order(guest_payload(store_product, quantity=10, expected_available=30))
    approve_store_order(
        order_id=previous.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        allocations=[_allocation(previous, remaining)],
    )
    first = _select_csa(active_user, store_product)
    second = _select_csa(active_user, store_product)
    for selection in (first, second):
        csa_rounds.process_round(selection.round_id, now=selection.round.closes_at)
        selection.refresh_from_db()

    clock.now = clock.next_day
    order = first.order
    key = uuid.uuid4()
    allocations = [_allocation(order, remaining)]
    with pytest.raises(StoreOrderError, match="store_allocation_stale"):
        approve_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
        )
    _assert_pending_without_sales(order)
    assert first.stock_holds.get().quantity == second.stock_holds.get().quantity == 10

    remaining.quantity_units = 30
    remaining.save(update_fields=["quantity_units"])
    approved = approve_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
    )
    assert approved.status == StoreOrderStatus.PENDING_DELIVERY
    assert not first.stock_holds.exists()
    assert second.stock_holds.get().quantity == 10

    # Approval of outstanding demand can use the exact balance after a previous SALE.
    approve_store_order(
        order_id=second.order_id,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        allocations=[_allocation(second.order, remaining)],
    )
    assert not second.stock_holds.exists()
    assert list(StoreStockMovement.objects.values_list("quantity", flat=True)) == [10, 10, 10]


def test_approval_rejects_batch_outside_locked_products_before_locking_sources(
    active_user, store_product, inventory, payment_settings, monkeypatch
):
    _, remaining = inventory
    foreign = _second_product(store_product, remaining)
    order = _place_order(guest_payload(store_product, quantity=10, expected_available=20))
    allocation = {**_allocation(order, remaining), "batch_id": foreign.pk}
    source_locks = []
    real_fetch = QuerySet._fetch_all

    def record_source_locks(queryset):
        if queryset.query.select_for_update and queryset.model in (
            FarmProduct,
            Farm,
            FarmProductStockBatch,
        ):
            source_locks.append(queryset.model)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", record_source_locks)
    with pytest.raises(StoreOrderError, match="store_allocation_stale"):
        approve_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            allocations=[allocation],
        )

    assert not source_locks
    _assert_pending_without_sales(order)
