"""Supply approval precedes money; cancellation preserves auditable ledgers."""

import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from commerce import store_orders as services
from commerce.models import StoreOrder, StoreOrderAction, StoreStockMovement
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db
VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")


def checkout(product, *, days=1):
    payload = guest_payload(product)
    payload["delivery_date"] = timezone.localdate(timezone=VIETNAM) + timedelta(days=days)
    response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.data
    data = response.data["data"]
    return StoreOrder.objects.get(pk=data["id"]), data["guest_access_token"]


def approve(order, actor):
    line = order.lines.get()
    return services.approve_store_order(
        order_id=order.pk,
        actor=actor,
        idempotency_key=uuid.uuid4(),
        allocations=[
            {
                "line_id": line.pk,
                "batch_id": line.product.farm_offers.get().stock_batches.get().pk,
                "quantity": line.quantity,
            }
        ],
    )


def test_new_checkout_waits_for_goods_until_delivery_end(store_product, payment_settings):
    order, _ = checkout(store_product)
    assert order.status == "pending_review"
    assert order.workflow_version == 2
    assert order.expires_at == datetime.combine(
        order.delivery_date + timedelta(days=1), time.min, VIETNAM
    )


def test_goods_approval_never_confirms_money(store_product, payment_settings, active_user):
    order, _ = checkout(store_product)
    approved = approve(order, active_user)
    assert approved.status == "payment_pending"
    assert approved.payment_status == "unconfirmed"
    assert approved.payment_received_at is None
    assert approved.expires_at == approved.approved_at + timedelta(hours=12)
    assert StoreStockMovement.objects.filter(line__order=order, kind="sale").count() == 1


def test_report_is_forbidden_before_goods(store_product, payment_settings):
    order, token = checkout(store_product)
    with pytest.raises(services.StoreOrderError, match="store_order_state_conflict"):
        services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)


def test_report_moves_to_payment_review_without_receipt_and_retry_keeps_deadline(
    store_product, payment_settings, active_user
):
    order, token = checkout(store_product)
    approve(order, active_user)
    result = services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
    assert result.status == "payment_review"
    assert result.payment_status == "unconfirmed"
    assert result.expires_at == result.transfer_reported_at + timedelta(hours=12)
    repeated = services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
    assert repeated.expires_at == result.expires_at
    assert repeated.transfer_reported_at == result.transfer_reported_at


@pytest.mark.parametrize("report", [False, True])
def test_admin_receipt_advances_approved_order_with_or_without_report(
    store_product, payment_settings, active_user, report
):
    order, token = checkout(store_product)
    approve(order, active_user)
    if report:
        services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
    key = uuid.uuid4()
    paid = services.record_store_payment(order_id=order.pk, actor=active_user, idempotency_key=key)
    assert paid.status == "pending_delivery"
    assert paid.payment_status == "received"
    assert paid.payment_received_by == active_user
    assert paid.expires_at is None
    retried = services.record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=key
    )
    assert retried.payment_received_at == paid.payment_received_at
    services.deliver_store_order(order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4())
    order.refresh_from_db()
    assert order.status == "delivered"


def test_admin_cannot_receive_before_goods(store_product, payment_settings, active_user):
    order, _ = checkout(store_product)
    with pytest.raises(services.StoreOrderError, match="store_payment_state_conflict"):
        services.record_store_payment(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
        )


def test_legacy_cancelled_order_can_request_refund_without_goods_approval(
    store_product, payment_settings
):
    order, token = checkout(store_product)
    StoreOrder.objects.filter(pk=order.pk).update(workflow_version=1, status="payment_pending")
    services.cancel_pending_store_order(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
    )
    result = services.request_store_refund(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
    )
    assert result.approved_at is None
    assert result.refund_status == "pending"


def test_preapproval_cancellation_blocks_new_receipt_but_preserves_existing_money(
    store_product, payment_settings, active_user
):
    order, token = checkout(store_product)
    services.cancel_pending_store_order(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
    )
    with pytest.raises(services.StoreOrderError, match="store_payment_state_conflict"):
        services.record_store_payment(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
        )
    # Existing verified receipt from the previous contract must remain refundable.
    StoreOrder.objects.filter(pk=order.pk).update(
        payment_received_at=timezone.now(), payment_status="refund_required"
    )
    order.refresh_from_db()
    result = services.refund_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        amount_vnd=order.total_vnd,
        note="Returned existing verified receipt",
    )
    assert result.refund_status == "refunded"


@pytest.mark.parametrize(
    "state,refund",
    [
        ("pending_review", False),
        ("payment_pending", False),
        ("payment_pending", True),
        ("payment_review", True),
        ("payment_review", False),
    ],
)
def test_buyer_cancel_releases_product_and_batch_once_without_inventing_receipt(
    store_product, payment_settings, active_user, state, refund
):
    order, token = checkout(store_product)
    if state != "pending_review":
        approve(order, active_user)
    if state == "payment_review":
        services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
    key = uuid.uuid4()
    result = services.cancel_pending_store_order(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=key, request_refund=refund
    )
    assert result.status == "cancelled"
    assert result.payment_status == "unconfirmed"
    assert result.refund_status == ("pending" if state != "pending_review" else None)
    assert not result.lines.get().stock_reservations.filter(released_at__isnull=True).exists()
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 10
    result2 = services.cancel_pending_store_order(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=key, request_refund=refund
    )
    assert result2.cancelled_at == result.cancelled_at
    assert StoreStockMovement.objects.filter(line__order=order, kind="restock").count() == int(
        state != "pending_review"
    )


@pytest.mark.parametrize("state", ["pending_delivery", "delivered", "cancelled"])
def test_buyer_cannot_regular_cancel_in_forbidden_states(store_product, payment_settings, state):
    order, token = checkout(store_product)
    StoreOrder.objects.filter(pk=order.pk).update(status=state)
    with pytest.raises(services.StoreOrderError, match="store_order_state_conflict"):
        services.cancel_pending_store_order(
            order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
        )


def test_admin_cancel_requires_reason_and_received_money_requires_refund(
    store_product, payment_settings, active_user
):
    order, _ = checkout(store_product)
    with pytest.raises(services.StoreOrderError, match="store_cancellation_reason_required"):
        services.cancel_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason=" "
        )
    approve(order, active_user)
    services.record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
    )
    result = services.cancel_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        reason="Insufficient supply",
    )
    assert result.refund_status == "pending"
    assert result.payment_status == "refund_required"
    assert result.cancelled_by == active_user
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 10
    with pytest.raises(services.StoreOrderError, match="store_refund_state_conflict"):
        services.close_store_refund_request(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="No money"
        )


def test_cancelled_reconciliation_receipt_is_refunded_exactly_once(
    store_product, payment_settings, active_user
):
    order, token = checkout(store_product)
    approve(order, active_user)
    services.cancel_pending_store_order(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
    )
    order.refresh_from_db()
    assert order.refund_status == "pending"
    assert order.payment_received_at is None
    with pytest.raises(services.StoreOrderError, match="store_refund_state_conflict"):
        services.request_store_refund(
            order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
        )
    paid = services.record_store_payment(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        note="Late bank transfer",
    )
    assert paid.status == "cancelled"
    assert paid.refund_status == "pending"
    refund_key = uuid.uuid4()
    refunded = services.refund_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=refund_key,
        amount_vnd=order.total_vnd,
        note="Bank refund complete",
    )
    assert refunded.refund_status == "refunded"
    assert refunded.status == "cancelled"
    repeated = services.refund_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=refund_key,
        amount_vnd=order.total_vnd,
        note="Bank refund complete",
    )
    assert repeated.refunded_at == refunded.refunded_at
    with pytest.raises(services.StoreOrderError, match="store_refund_state_conflict"):
        services.refund_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            amount_vnd=order.total_vnd,
            note="Again",
        )


@pytest.mark.parametrize(
    "state,reason",
    [
        ("pending_review", "delivery_date_expired"),
        ("payment_pending", "payment_expired"),
        ("payment_review", "payment_review_expired"),
    ],
)
def test_expiry_uses_actual_first_deadline_even_when_worker_runs_after_delivery(
    store_product, payment_settings, active_user, monkeypatch, state, reason
):
    order, token = checkout(store_product)
    if state != "pending_review":
        approve(order, active_user)
    if state == "payment_review":
        services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
    order.refresh_from_db()
    deadline = order.expires_at
    late = datetime.combine(order.delivery_date + timedelta(days=2), time.min, VIETNAM)
    monkeypatch.setattr(timezone, "now", lambda: late)
    assert services.expire_store_orders() == 1
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert order.cancellation_reason == reason
    assert services.expire_store_orders() == 0
    # The delayed worker ran after this batch expired: its available stock stays zero,
    # while the audit ledger reverses every committed sale and Product holds end.
    assert not order.lines.get().stock_reservations.filter(released_at__isnull=True).exists()
    movements = list(StoreStockMovement.objects.filter(line__order=order))
    assert sum(m.quantity if m.kind == "sale" else -m.quantity for m in movements) == 0
    assert order.cancelled_at >= deadline


@pytest.mark.parametrize("state", ["payment_pending", "payment_review"])
def test_delivery_end_wins_equal_deadline(
    store_product, payment_settings, active_user, monkeypatch, state
):
    order, token = checkout(store_product)
    delivery_end = datetime.combine(order.delivery_date + timedelta(days=1), time.min, VIETNAM)
    monkeypatch.setattr(timezone, "now", lambda: delivery_end - timedelta(hours=12))
    approve(order, active_user)
    if state == "payment_review":
        services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
    monkeypatch.setattr(timezone, "now", lambda: delivery_end)
    services.expire_order_if_needed(order.pk)
    order.refresh_from_db()
    assert order.cancellation_reason == "delivery_date_expired"


def test_late_payment_confirmation_cancels_before_recording_and_never_resurrects(
    store_product, payment_settings, active_user, monkeypatch
):
    order, _ = checkout(store_product)
    approved = approve(order, active_user)
    monkeypatch.setattr(timezone, "now", lambda: approved.expires_at)
    result = services.record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
    )
    assert result.status == "cancelled"
    assert result.payment_status == "refund_required"
    assert result.refund_status == "pending"
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 10


def test_cancel_resources_rollback_with_action_failure(
    store_product, payment_settings, active_user, monkeypatch
):
    order, token = checkout(store_product)
    approve(order, active_user)

    def fail(*args, **kwargs):
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(services, "_record_action", fail)
    with pytest.raises(RuntimeError, match="injected ledger failure"):
        services.cancel_pending_store_order(
            order_id=order.pk,
            user=None,
            guest_token=token,
            idempotency_key=uuid.uuid4(),
            request_refund=True,
        )
    order.refresh_from_db()
    assert order.status == "payment_pending"
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 8
    assert not StoreStockMovement.objects.filter(line__order=order, kind="restock").exists()


def member_checkout(user, token, product, *, quantity=3, coupon=None):
    from commerce.models import ShippingAddress

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    body = {
        "delivery_date": (timezone.localdate(timezone=VIETNAM) + timedelta(days=1)).isoformat(),
        "lines": [{"product_id": str(product.pk), "quantity": quantity, "use_quota": True}],
    }
    if coupon:
        body["coupon_code"] = coupon.code
    response = client.post("/api/v1/public/store-quotes", body, format="json")
    assert response.status_code == 200, response.data
    quote = response.data["data"]
    checkout_body = {
        "delivery_date": body["delivery_date"],
        "idempotency_key": str(uuid.uuid4()),
        "locale": "vi",
        "lines": quote["lines"],
        "shipping_address_updated_at": ShippingAddress.objects.get(
            user=user
        ).updated_at.isoformat(),
        "expected_shipping_fee_vnd": quote["shipping_fee_vnd"],
        "expected_total_vnd": quote["total_vnd"],
    }
    if coupon:
        checkout_body.update(coupon_code=coupon.code, expected_voucher_quote=quote["voucher_quote"])
    response = client.post("/api/v1/public/store-checkouts", checkout_body, format="json")
    assert response.status_code == 201, response.data
    return StoreOrder.objects.get(pk=response.data["data"]["id"])


@pytest.mark.parametrize("quantity,coupon_zero", [(2, False), (3, False), (3, True)])
def test_quota_and_voucher_commit_at_goods_then_reverse_once_on_cancel(
    store_product,
    payment_settings,
    active_user,
    access_token,
    supported_area,
    quantity,
    coupon_zero,
):
    from memberships.models import (
        CouponUsage,
        CSACoupon,
        MembershipCycleQuota,
        MembershipUsageReversal,
    )
    from tests.test_store_orders import attach_active_membership

    attach_active_membership(active_user, store_product)
    supported_area.shipping_fee_vnd = 0
    supported_area.save(update_fields=["shipping_fee_vnd"])
    coupon = None
    if coupon_zero:
        coupon = CSACoupon.objects.create(
            code="FULLCASH",
            application_scope="store",
            discount_type="fixed",
            discount_value=45_000,
            starts_at=timezone.now() - timedelta(days=1),
            expires_at=timezone.now() + timedelta(days=1),
        )
    order = member_checkout(
        active_user, access_token, store_product, quantity=quantity, coupon=coupon
    )
    assert order.status == "pending_review"
    assert MembershipCycleQuota.objects.get().remaining_units == 4
    assert order.lines.get().quota_reservations.get().released_at is None
    approved = approve(order, active_user)
    assert approved.status == (
        "pending_delivery" if quantity == 2 or coupon_zero else "payment_pending"
    )
    assert MembershipCycleQuota.objects.get().remaining_units == 0
    assert order.lines.get().quota_reservations.get().consumed_at is not None
    key = uuid.uuid4()
    services.cancel_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, reason="Supply withdrawn"
    )
    services.cancel_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, reason="Supply withdrawn"
    )
    assert MembershipCycleQuota.objects.get().remaining_units == 4
    assert MembershipUsageReversal.objects.count() == 1
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 10
    if coupon:
        coupon.refresh_from_db()
        assert coupon.used_count == 0
        assert CouponUsage.objects.get(store_order=order).released_at is not None


def test_consumed_expired_quota_reverses_to_expired_ledger_once(
    store_product,
    payment_settings,
    active_user,
    access_token,
    supported_area,
    monkeypatch,
):
    from memberships.models import MembershipCycleQuota, MembershipUsageReversal
    from tests.test_store_orders import attach_active_membership

    attach_active_membership(active_user, store_product)
    order = member_checkout(active_user, access_token, store_product)
    approve(order, active_user)
    quota = MembershipCycleQuota.objects.get()
    now = datetime.combine(quota.cycle.end_date, time.min, VIETNAM)
    monkeypatch.setattr(timezone, "now", lambda: now)
    assert services.expire_order_if_needed(order.pk)[1]
    assert not services.expire_order_if_needed(order.pk)[1]
    quota.refresh_from_db()
    assert quota.remaining_units == 0
    assert quota.expired_units == 4
    assert MembershipUsageReversal.objects.count() == 1


@pytest.mark.parametrize("action", ["receipt", "refund"])
def test_payment_and_refund_rollback_when_action_write_fails(
    store_product,
    payment_settings,
    active_user,
    monkeypatch,
    action,
):
    order, _ = checkout(store_product)
    approve(order, active_user)
    if action == "refund":
        services.record_store_payment(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
        )
        services.cancel_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="Cancelled"
        )

    def fail(*args, **kwargs):
        raise RuntimeError("action write failed")

    monkeypatch.setattr(services, "_record_action", fail)
    with pytest.raises(RuntimeError, match="action write failed"):
        if action == "receipt":
            services.record_store_payment(
                order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
            )
        else:
            services.refund_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                amount_vnd=order.total_vnd,
                note="Refunded",
            )
    order.refresh_from_db()
    assert order.payment_status == ("unconfirmed" if action == "receipt" else "refund_required")
    assert order.refunded_at is None
    if action == "receipt":
        assert order.status == "payment_pending"
        assert order.payment_received_at is None


@pytest.mark.parametrize("action", ["report", "approve", "receipt", "cancel", "expire"])
def test_waiting_for_resource_lock_rechecks_clock(
    store_product,
    payment_settings,
    active_user,
    monkeypatch,
    action,
):
    order, token = checkout(store_product)
    if action != "approve":
        approve(order, active_user)
    order.refresh_from_db()
    old_clock = timezone.now()
    new_clock = order.expires_at
    clock = [old_clock]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    original = services._lock_order_memberships

    def delayed_lock(order):
        original(order)
        clock[0] = new_clock

    monkeypatch.setattr(services, "_lock_order_memberships", delayed_lock)
    if action in {"report", "approve", "cancel"}:
        with pytest.raises(services.StoreOrderError, match="store_order_expired"):
            if action == "report":
                services.report_store_transfer(order_id=order.pk, user=None, guest_token=token)
            elif action == "approve":
                approve(order, active_user)
            else:
                services.cancel_pending_store_order(
                    order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
                )
    elif action == "receipt":
        services.record_store_payment(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
        )
    else:
        services.expire_order_if_needed(order.pk, now=old_clock)
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert order.payment_status == ("refund_required" if action == "receipt" else "unconfirmed")


@pytest.mark.parametrize("action", ["cancel", "request"])
def test_wrong_guest_and_wrong_owner_cannot_mutate(
    store_product, payment_settings, active_user, action
):
    order, token = checkout(store_product)
    if action == "request":
        services.cancel_pending_store_order(
            order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
        )
    method = (
        services.cancel_pending_store_order if action == "cancel" else services.request_store_refund
    )
    with pytest.raises(services.StoreOrderError, match="store_order_not_found"):
        method(order_id=order.pk, user=None, guest_token="wrong", idempotency_key=uuid.uuid4())
    StoreOrder.objects.filter(pk=order.pk).update(buyer_user=active_user)
    with pytest.raises(services.StoreOrderError, match="store_order_not_found"):
        method(order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4())


def test_pending_delivery_has_no_automatic_delivery_or_expiry(
    store_product, payment_settings, active_user, monkeypatch
):
    order, _ = checkout(store_product)
    approve(order, active_user)
    services.record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
    )
    monkeypatch.setattr(
        timezone,
        "now",
        lambda: datetime.combine(order.delivery_date + timedelta(days=3), time.min, VIETNAM),
    )
    assert services.expire_store_orders() == 0
    order.refresh_from_db()
    assert order.status == "pending_delivery"


def test_duplicate_refund_request_key_cannot_reopen_after_admin_closes(
    store_product,
    payment_settings,
    active_user,
):
    order, token = checkout(store_product)
    approve(order, active_user)
    services.cancel_pending_store_order(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
    )
    # Accepted by the earlier v2 contract: replay must remain safe after rollout.
    duplicate_key = uuid.uuid4()
    StoreOrderAction.objects.create(
        order=order,
        action="request_refund",
        idempotency_key=duplicate_key,
        request_fingerprint=services.request_fingerprint({}),
        context={"opened": False},
    )
    services.close_store_refund_request(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        reason="No received funds",
    )
    result = services.request_store_refund(
        order_id=order.pk, user=None, guest_token=token, idempotency_key=duplicate_key
    )
    assert result.refund_status is None
    assert result.refund_request_closed_reason == "No received funds"


def test_duplicate_receipt_key_is_durably_bound_to_exact_payload(
    store_product,
    payment_settings,
    active_user,
):
    order, _ = checkout(store_product)
    approve(order, active_user)
    services.record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
    )
    duplicate_key = uuid.uuid4()
    services.record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=duplicate_key, note="Already received"
    )
    with pytest.raises(services.StoreOrderError, match="store_idempotency_conflict"):
        services.record_store_payment(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=duplicate_key,
            note="Different request",
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "race", ["report_receipt", "approve_cancel", "expiry_receipt", "refund_refund"]
)
def test_native_row_lock_races_serialize_transitions_and_ledgers(
    store_product,
    payment_settings,
    active_user,
    race,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connection
    from django.db.models import Sum

    if not connection.features.has_select_for_update:
        pytest.skip("Requires native row locks (MySQL/PostgreSQL); SQLite cannot prove this race")
    order, token = checkout(store_product)
    if race != "approve_cancel":
        approve(order, active_user)
    if race == "expiry_receipt":
        StoreOrder.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    if race == "refund_refund":
        services.record_store_payment(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
        )
        services.cancel_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="Cancelled"
        )
    barrier = Barrier(2)

    def invoke(index):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            if race == "report_receipt":
                if index == 0:
                    return services.report_store_transfer(
                        order_id=order.pk, user=None, guest_token=token
                    )
                return services.record_store_payment(
                    order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
                )
            if race == "approve_cancel":
                if index == 0:
                    return approve(order, active_user)
                return services.cancel_pending_store_order(
                    order_id=order.pk, user=None, guest_token=token, idempotency_key=uuid.uuid4()
                )
            if race == "expiry_receipt":
                if index == 0:
                    return services.expire_order_if_needed(order.pk)
                return services.record_store_payment(
                    order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4()
                )
            return services.refund_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                amount_vnd=order.total_vnd,
                note="Actual full refund",
            )
        except services.StoreOrderError as error:
            return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(invoke, (0, 1)))
    assert all(
        not isinstance(outcome, str)
        or outcome in {"store_order_state_conflict", "store_refund_state_conflict"}
        for outcome in outcomes
    )
    order.refresh_from_db()
    if race == "report_receipt":
        assert order.status == "pending_delivery"
        assert order.payment_status == "received"
    elif race == "approve_cancel":
        assert order.status == "cancelled"
    elif race == "expiry_receipt":
        assert order.status == "cancelled"
        assert order.payment_status == "refund_required"
    else:
        assert order.refund_status == "refunded"
        assert StoreOrderAction.objects.filter(order=order, action="refund").count() == 1
    if order.status == "cancelled":
        sold = (
            StoreStockMovement.objects.filter(line__order=order, kind="sale").aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )
        restored = (
            StoreStockMovement.objects.filter(line__order=order, kind="restock").aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )
        assert restored == sold


def test_legacy_reported_order_can_cancel_with_refund_without_changing_legacy_deadline_rules(
    store_product,
    payment_settings,
):
    order, token = checkout(store_product)
    legacy_deadline = timezone.now() + timedelta(hours=8)
    StoreOrder.objects.filter(pk=order.pk).update(
        workflow_version=1,
        status="pending_review",
        transfer_reported_at=timezone.now(),
        expires_at=legacy_deadline,
    )
    result = services.cancel_pending_store_order(
        order_id=order.pk,
        user=None,
        guest_token=token,
        idempotency_key=uuid.uuid4(),
        request_refund=True,
    )
    assert result.status == "cancelled"
    assert result.workflow_version == 1
    assert result.refund_status == "pending"
    assert result.payment_status == "unconfirmed"
    assert StoreOrderAction.objects.get(order=order, action="buyer_cancel").context == {
        "request_refund": True
    }


def test_zero_total_order_cannot_request_refund_after_cancellation(
    store_product,
    active_user,
    access_token,
    supported_area,
):
    from tests.test_store_orders import attach_active_membership

    attach_active_membership(active_user, store_product)
    supported_area.shipping_fee_vnd = 0
    supported_area.save(update_fields=["shipping_fee_vnd"])
    order = member_checkout(active_user, access_token, store_product, quantity=2)
    services.cancel_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="No delivery"
    )
    with pytest.raises(services.StoreOrderError, match="store_refund_state_conflict"):
        services.request_store_refund(
            order_id=order.pk, user=active_user, guest_token="", idempotency_key=uuid.uuid4()
        )
    assert StoreOrder.objects.get(pk=order.pk).refund_status is None


def test_coupon_lock_wait_cannot_overwrite_expiry_with_buyer_cancel_reason(
    store_product,
    payment_settings,
    active_user,
    access_token,
    supported_area,
    monkeypatch,
):
    from memberships.models import CSACoupon
    from tests.test_store_orders import attach_active_membership

    attach_active_membership(active_user, store_product)
    coupon = CSACoupon.objects.create(
        code="LOCKWAIT",
        application_scope="store",
        discount_type="fixed",
        discount_value=1000,
        starts_at=timezone.now() - timedelta(days=1),
        expires_at=timezone.now() + timedelta(days=1),
    )
    order = member_checkout(active_user, access_token, store_product, coupon=coupon)
    approve(order, active_user)
    order.refresh_from_db()
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    original = CSACoupon.objects.select_for_update

    def delayed_coupon_lock(*args, **kwargs):
        clock[0] = order.expires_at
        return original(*args, **kwargs)

    monkeypatch.setattr(CSACoupon.objects, "select_for_update", delayed_coupon_lock)
    with pytest.raises(services.StoreOrderError, match="store_order_expired"):
        services.cancel_pending_store_order(
            order_id=order.pk, user=active_user, guest_token="", idempotency_key=uuid.uuid4()
        )
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert order.cancellation_reason == "payment_expired"
    coupon.refresh_from_db()
    assert coupon.used_count == 0


def test_goods_approval_rechecks_clock_after_waiting_for_actor_identity_lock(
    store_product,
    payment_settings,
    active_user,
    monkeypatch,
):
    order, _ = checkout(store_product)
    clock = [timezone.now()]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    original = services._lock_checkout_identity

    def delayed_actor_lock(actor):
        original(actor)
        clock[0] = order.expires_at

    monkeypatch.setattr(services, "_lock_checkout_identity", delayed_actor_lock)
    with pytest.raises(services.StoreOrderError, match="store_order_expired"):
        approve(order, active_user)
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert order.approved_at is None
    assert not StoreStockMovement.objects.filter(line__order=order).exists()


def test_new_legacy_retail_cancellation_reverses_allocated_batch_immediately_once(
    store_product,
    payment_settings,
    active_user,
):
    order, token = checkout(store_product)
    # A legacy order that already reached its old review step must retain that
    # meaning; its future cancellation still releases all committed resources.
    StoreOrder.objects.filter(pk=order.pk).update(
        workflow_version=1,
        status="pending_review",
        transfer_reported_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=8),
    )
    approved = approve(order, active_user)
    assert approved.status == "pending_delivery"
    assert approved.payment_status == "received"
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 8
    key = uuid.uuid4()
    result = services.cancel_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=key,
        reason="Supply withdrawn",
    )
    assert result.status == "cancelled"
    assert result.payment_status == "refund_required"
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 10
    services.cancel_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=key,
        reason="Supply withdrawn",
    )
    assert StoreStockMovement.objects.filter(line__order=order, kind="restock").count() == 1
    assert order.lines.get().stock_allocations.count() == 1


def test_historical_cancelled_legacy_retail_is_not_retroactively_restocked(
    store_product,
    payment_settings,
    active_user,
):
    order, _ = checkout(store_product)
    StoreOrder.objects.filter(pk=order.pk).update(
        workflow_version=1,
        status="pending_review",
        transfer_reported_at=timezone.now(),
        expires_at=timezone.now() + timedelta(hours=8),
    )
    approve(order, active_user)
    # Represents an already-cancelled pre-deployment record, without executing
    # the new cancellation service against that historical event.
    StoreOrder.objects.filter(pk=order.pk).update(
        status="cancelled",
        cancelled_at=timezone.now(),
        cancellation_reason="Historical cancellation",
        payment_status="refund_required",
        expires_at=None,
    )
    assert not services.expire_order_if_needed(order.pk)[1]
    assert services.expire_store_orders() == 0
    with pytest.raises(services.StoreOrderError, match="store_order_state_conflict"):
        services.cancel_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            reason="Retry with different key",
        )
    assert services.active_product_stock([store_product.pk])[0][store_product.pk] == 8
    assert not StoreStockMovement.objects.filter(line__order=order, kind="restock").exists()
