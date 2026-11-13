import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import (
    ShippingAddress,
    StoreOrder,
    StoreQuotaReservation,
    StoreStockReservation,
)
from memberships.models import CouponUsage, CSACoupon, MembershipCycle
from tests.test_store_orders import (
    PROVINCE,
    WARD,
    attach_active_membership,
)
from tests.test_store_orders import (
    payment_settings as payment_settings,
)
from tests.test_store_orders import (
    store_product as store_product,
)
from tests.test_store_orders import (
    supported_area as supported_area,
)
from tests.test_store_orders import zero_shipping_fee as zero_shipping_fee

pytestmark = pytest.mark.django_db


@pytest.fixture
def context(active_user, access_token, store_product, payment_settings, supported_area):
    ShippingAddress.objects.create(
        user=active_user,
        recipient_name="Owner",
        phone_number="0901234567",
        province_code=PROVINCE,
        ward_code=WARD,
        address="12 Nguyen Hue",
    )
    voucher = CSACoupon.objects.create(
        code="SHARED30",
        application_scope="both",
        discount_type="fixed",
        discount_value=30_000,
        usage_limit=3,
        starts_at=timezone.now() - timedelta(days=1),
        expires_at=timezone.now() + timedelta(days=1),
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    preview_input = {
        "delivery_date": timezone.localdate(),
        "coupon_code": voucher.code,
        "lines": [{"product_id": str(store_product.pk), "quantity": 2, "use_quota": False}],
    }
    return client, voucher, preview_input


def checkout_body(quote, code):
    return {
        "delivery_date": timezone.localdate(),
        "idempotency_key": str(uuid.uuid4()),
        "locale": "vi",
        "coupon_code": code,
        "expected_voucher_quote": quote["voucher_quote"],
        "lines": quote["lines"],
        "shipping_address_updated_at": quote["shipping_address_updated_at"],
        "expected_shipping_fee_vnd": int(quote["shipping_fee_vnd"]),
        "expected_total_vnd": int(quote["total_vnd"]),
    }


def preview(context):
    client, voucher, payload = context
    response = client.post("/api/v1/store-voucher-preview", payload, format="json")
    assert response.status_code == 200, response.data
    return response.json()["data"]


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"application_scope": "csa"}, "coupon_scope_mismatch"),
        ({"is_active": False}, "coupon_inactive"),
        ({"starts_at": timezone.now() + timedelta(hours=1)}, "coupon_not_started"),
        ({"expires_at": timezone.now() - timedelta(seconds=1)}, "coupon_expired"),
        ({"used_count": 3}, "coupon_usage_exhausted"),
        ({"minimum_amount": 100_000}, "coupon_minimum_amount_not_met"),
        ({"discount_value": 90_001}, "coupon_invalid_value"),
    ],
)
def test_store_voucher_validation_is_read_only(context, changes, code):
    client, voucher, payload = context
    CSACoupon.objects.filter(pk=voucher.pk).update(**changes)
    response = client.post("/api/v1/store-voucher-preview", payload, format="json")
    assert response.status_code == 400, response.data
    assert response.json()["errors"] == [{"code": code}]
    assert not StoreOrder.objects.exists()
    assert not CouponUsage.objects.exists()
    assert not StoreStockReservation.objects.exists()


def test_client_cannot_supply_price_quota_balance_or_discount(context):
    client, _, payload = context
    for field in ("discount_amount_vnd", "quota_balance", "subtotal_vnd"):
        response = client.post(
            "/api/v1/store-voucher-preview", {**payload, field: 0}, format="json"
        )
        assert response.status_code == 400
    response = client.post(
        "/api/v1/store-voucher-preview",
        {**payload, "lines": [{**payload["lines"][0], "price": 1}]},
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "changes",
    [{"discount_value": 20_000}, {"minimum_amount": 1}, {"usage_limit": 4}, {"code": "RENAMED"}],
)
def test_checkout_rechecks_changed_voucher_before_writes(context, changes):
    client, voucher, _ = context
    quote = preview(context)
    CSACoupon.objects.filter(pk=voucher.pk).update(**changes)
    response = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert response.status_code in (400, 409), response.data
    assert response.json()["errors"][0]["code"] in {"coupon_invalid", "store_requote_required"}
    assert not StoreOrder.objects.exists()
    assert not CouponUsage.objects.exists()


def test_zero_goods_still_pays_shipping_and_uuid_cannot_reuse(context):
    client, voucher, payload = context
    CSACoupon.objects.filter(pk=voucher.pk).update(discount_value=90_000)
    quote = preview(context)
    assert quote["total_vnd"] == "30000"
    created = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert created.status_code == 201, created.data
    assert client.post("/api/v1/store-voucher-preview", payload, format="json").json()[
        "errors"
    ] == [{"code": "coupon_already_used"}]
    from memberships.admin_coupons import save_admin_coupon
    from memberships.coupons import CouponError
    from memberships.voucher_usage import release_coupon

    order = StoreOrder.objects.get()
    release_coupon(store_order=order, reason="cancelled")
    with pytest.raises(CouponError, match="coupon_cannot_edit_used"):
        save_admin_coupon(coupon_id=voucher.pk, data={"discount_value": Decimal(1)})


def test_mixed_quota_discount_and_zero_total_use_existing_workflow(
    zero_shipping_fee, context, active_user, store_product
):
    client, voucher, payload = context
    ShippingAddress.objects.filter(user=active_user).delete()
    attach_active_membership(active_user, store_product)
    payload["lines"][0].update(quantity=3, use_quota=True)
    CSACoupon.objects.filter(pk=voucher.pk).update(discount_value=45_000)
    before = (MembershipCycle.objects.count(), StoreQuotaReservation.objects.count())
    quote = preview(context)
    assert before == (MembershipCycle.objects.count(), StoreQuotaReservation.objects.count())
    assert quote["lines"][0]["expected_quota_quantity"] == 2
    assert quote["subtotal_vnd"] == "45000"
    assert quote["discount_amount_vnd"] == "45000"
    assert quote["shipping_fee_vnd"] == "0"
    assert quote["total_vnd"] == "0"
    response = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert response.status_code == 201, response.data
    assert response.json()["data"]["status"] == "pending_review"
    assert response.json()["data"]["payment_status"] == "not_required"
    assert response.json()["data"]["payment"] is None


def test_preview_projects_due_cycles_without_materializing(context, active_user, store_product):
    client, voucher, payload = context
    ShippingAddress.objects.filter(user=active_user).delete()
    membership = attach_active_membership(active_user, store_product)
    # This fixture has one cycle. Removing it models a due but unmaterialized cycle.
    from memberships.models import MembershipCycleQuota

    MembershipCycleQuota.objects.filter(cycle__membership=membership).delete()
    MembershipCycle.objects.filter(membership=membership).delete()
    payload["lines"][0].update(quantity=3, use_quota=True)
    quote = preview(context)
    assert not MembershipCycle.objects.filter(membership=membership).exists()
    assert quote["lines"][0]["expected_quota_quantity"] == 2
    response = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert response.status_code == 201, response.data
    assert MembershipCycle.objects.filter(membership=membership).exists()


def test_usage_rollback_rolls_back_order_and_reservations(context, monkeypatch):
    client, voucher, _ = context
    quote = preview(context)
    from commerce import store_orders

    real_consume = store_orders.consume_coupon

    def fail_after_usage(**kwargs):
        real_consume(**kwargs)
        raise RuntimeError("rollback probe")

    monkeypatch.setattr(store_orders, "consume_coupon", fail_after_usage)
    with pytest.raises(RuntimeError, match="rollback probe"):
        client.post(
            "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
        )
    assert not StoreOrder.objects.exists()
    assert not StoreStockReservation.objects.exists()
    assert not CouponUsage.objects.exists()
    voucher.refresh_from_db()
    assert voucher.used_count == 0


def test_preview_permissions_and_admin_alias_contract(context, active_user, access_token):
    client, voucher, payload = context
    assert (
        APIClient().post("/api/v1/store-voucher-preview", payload, format="json").status_code == 401
    )
    access_token.scope = "profile"
    access_token.save(update_fields=["scope"])
    assert client.post("/api/v1/store-voucher-preview", payload, format="json").status_code == 403
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    assert client.get("/api/v1/admin/vouchers").status_code == 403
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    response = client.get("/api/v1/admin/vouchers?application_scope=both")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == str(voucher.pk)
    assert (
        client.patch(
            f"/api/v1/admin/vouchers/{voucher.pk}", {"used_count": 1}, format="json"
        ).status_code
        == 400
    )


def test_admin_store_preview_uses_owner_cart_and_keeps_csa_conditions_off_store(
    context, active_user, access_token
):
    client, voucher, payload = context
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    body = {
        "delivery_date": timezone.localdate(),
        "code": "DRAFT",
        "application_scope": "store",
        "discount_type": "fixed",
        "discount_value": "30000",
        "starts_at": voucher.starts_at.isoformat(),
        "expires_at": voucher.expires_at.isoformat(),
        "target": "store",
        "store_user_id": str(active_user.pk),
        "lines": payload["lines"],
    }
    response = client.post("/api/v1/admin/vouchers/preview", body, format="json")
    assert response.status_code == 200, response.data
    assert response.json()["data"]["total_vnd"] == "90000"
    assert CSACoupon.objects.count() == 1
    assert not StoreOrder.objects.exists()
    invalid = {**body, "payment_types": ["full"]}
    response = client.post(
        "/api/v1/admin/vouchers",
        {
            key: value
            for key, value in invalid.items()
            if key not in {"target", "lines", "store_user_id", "delivery_date"}
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "coupon_csa_conditions_not_allowed"}]


def test_expired_voucher_replay_checks_owner_before_live_validation(
    context, active_user, access_token
):
    client, voucher, _ = context
    quote = preview(context)
    body = checkout_body(quote, voucher.code)
    assert client.post("/api/v1/public/store-checkouts", body, format="json").status_code == 201
    CSACoupon.objects.filter(pk=voucher.pk).update(is_active=False)
    # Same key and payload under a different owner cannot retrieve the saved snapshot.
    from oauth2_provider.models import AccessToken

    from identity.models import User

    other = User.objects.create_user(email="other-voucher@example.com", name="Other")
    token = AccessToken.objects.create(
        user=other,
        application=access_token.application,
        token="other-voucher-token",
        expires=access_token.expires,
        scope="profile email",
    )
    other_client = APIClient()
    other_client.force_authenticate(user=other, token=token)
    response = other_client.post("/api/v1/public/store-checkouts", body, format="json")
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "store_idempotency_conflict"
    assert CouponUsage.objects.count() == 1


def test_preview_projects_scheduled_membership_that_has_started(
    context, active_user, store_product
):
    from memberships.models import Membership, MembershipStatus

    ShippingAddress.objects.filter(user=active_user).delete()
    membership = attach_active_membership(active_user, store_product)
    Membership.objects.filter(pk=membership.pk).update(status=MembershipStatus.SCHEDULED)
    context[2]["lines"][0].update(quantity=3, use_quota=True)
    quote = preview(context)
    assert quote["lines"][0]["expected_quota_quantity"] == 2
    assert quote["shipping_fee_vnd"] == "30000"
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.SCHEDULED
    response = context[0].post(
        "/api/v1/public/store-checkouts", checkout_body(quote, context[1].code), format="json"
    )
    assert response.status_code == 201, response.data


def test_legacy_duplicate_uuid_still_blocks_and_returns_its_count(context):
    client, voucher, payload = context
    quote = preview(context)
    assert (
        client.post(
            "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
        ).status_code
        == 201
    )
    usage = CouponUsage.objects.get()
    CouponUsage.objects.filter(pk=usage.pk).update(active_slot=None, legacy_duplicate=True)
    response = client.post("/api/v1/store-voucher-preview", payload, format="json")
    assert response.json()["errors"] == [{"code": "coupon_already_used"}]
    from memberships.voucher_usage import release_coupon

    assert release_coupon(store_order=StoreOrder.objects.get(), reason="cancelled")
    assert not release_coupon(store_order=StoreOrder.objects.get(), reason="refunded")
    voucher.refresh_from_db()
    assert voucher.used_count == 0
    assert client.post("/api/v1/store-voucher-preview", payload, format="json").status_code == 200


def test_voucher_approval_cancel_refund_and_buy_again_keep_snapshots(context, active_user):
    from commerce.store_orders import (
        cancel_store_order,
        record_store_payment,
        refund_store_order,
        report_store_transfer,
    )
    from tests.test_store_orders import approve_review_order

    client, voucher, _ = context
    quote = preview(context)
    created = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert created.status_code == 201
    order = StoreOrder.objects.get()
    report_store_transfer(order_id=order.pk, user=active_user)
    approve_review_order({"id": order.pk}, active_user)
    order.refresh_from_db()
    assert order.status == "pending_delivery"
    voucher.refresh_from_db()
    assert voucher.used_count == 1
    buy_again = client.get(
        f"/api/v1/store-orders/{order.pk}/buy-again", {"locale": "vi"}, format="json"
    )
    assert buy_again.status_code == 200, buy_again.data
    assert set(buy_again.json()["data"]) == {"lines"}
    snapshot = order.coupon_snapshot
    record_store_payment(order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4())
    cancel_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="after approval"
    )
    refund_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        amount_vnd=order.total_vnd,
        note="actual paid",
    )
    order.refresh_from_db()
    voucher.refresh_from_db()
    assert voucher.used_count == 0
    assert order.coupon_snapshot == snapshot
    assert order.refund_amount_vnd == 90_000


@pytest.mark.parametrize("transition", ["cancel", "expiry", "cancel_refund"])
def test_store_usage_release_survives_deleted_csa_catalog(context, active_user, transition):
    from commerce.store_orders import (
        cancel_pending_store_order,
        cancel_store_order,
        expire_order_if_needed,
        record_store_payment,
        refund_store_order,
    )
    from memberships.models import MembershipPackage, QuotaPolicy

    client, voucher, _ = context
    package = MembershipPackage.objects.create(
        name="CSA only scope", quota_policy=QuotaPolicy.EXPIRE
    )
    voucher.package_ids = [str(package.pk)]
    voucher.save()
    quote = preview(context)
    response = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert response.status_code == 201, response.data
    order = StoreOrder.objects.get()
    snapshot = order.coupon_snapshot
    package.delete()
    if transition == "cancel":
        key = uuid.uuid4()
        for _ in range(2):
            cancel_pending_store_order(
                order_id=order.pk, user=active_user, guest_token="", idempotency_key=key
            )
    elif transition == "expiry":
        StoreOrder.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        expire_order_if_needed(order.pk)
        expire_order_if_needed(order.pk)
    else:
        record_store_payment(order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4())
        cancel_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            reason="catalog removed",
        )
        refund_key = uuid.uuid4()
        for _ in range(2):
            refund_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=refund_key,
                amount_vnd=order.total_vnd,
                note="paid total",
            )
    order.refresh_from_db()
    voucher.refresh_from_db()
    assert order.status == "cancelled"
    assert order.coupon_snapshot == snapshot
    assert voucher.used_count == 0
    assert CouponUsage.objects.get().released_at is not None
    assert not StoreStockReservation.objects.filter(released_at__isnull=True).exists()


def test_store_can_consume_both_voucher_after_csa_scope_is_deleted(context):
    from django.core.exceptions import ValidationError

    from memberships.admin_coupons import save_admin_coupon
    from memberships.coupons import CouponError
    from memberships.models import MembershipPackage, QuotaPolicy

    client, voucher, _ = context
    package = MembershipPackage.objects.create(
        name="CSA only scope", quota_policy=QuotaPolicy.EXPIRE
    )
    voucher.package_ids = [str(package.pk)]
    voucher.save()
    package.delete()
    # Configuration validation remains strict; only counter updates avoid live catalog checks.
    with pytest.raises(ValidationError, match="coupon_not_applicable"):
        voucher.full_clean()
    with pytest.raises(CouponError, match="coupon_not_applicable"):
        save_admin_coupon(coupon_id=voucher.pk, data={"usage_limit": 4})
    quote = preview(context)
    response = client.post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert response.status_code == 201, response.data
    voucher.refresh_from_db()
    assert voucher.used_count == 1
    assert CouponUsage.objects.get().store_order_id == StoreOrder.objects.get().pk


def test_admin_can_deactivate_used_voucher_with_deleted_csa_scope(context):
    from memberships.admin_coupons import set_admin_coupon_active
    from memberships.models import MembershipPackage, QuotaPolicy

    client, voucher, _ = context
    package = MembershipPackage.objects.create(
        name="CSA only scope", quota_policy=QuotaPolicy.EXPIRE
    )
    voucher.package_ids = [str(package.pk)]
    voucher.save()
    quote = preview(context)
    assert (
        client.post(
            "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
        ).status_code
        == 201
    )
    package.delete()
    set_admin_coupon_active(coupon_id=voucher.pk, active=False)
    voucher.refresh_from_db()
    assert not voucher.is_active
    assert voucher.used_count == 1


def test_store_preview_rejects_percentage_rounded_to_zero(context, store_product):
    client, coupon, payload = context
    coupon.discount_type = "percent"
    coupon.discount_value = Decimal("0.01")
    coupon.save()
    store_product.retail_price_vnd = 1
    store_product.save(update_fields=["retail_price_vnd"])
    response = client.post("/api/v1/store-voucher-preview", payload, format="json")
    assert response.status_code == 400
    assert response.data["errors"][0]["code"] == "coupon_invalid_value"
    coupon.refresh_from_db()
    assert coupon.used_count == 0


def test_preview_does_not_fetch_unused_farm_lock_rows(context):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        preview(context)
    sql = [item["sql"].lower() for item in captured]
    assert not any(
        'from "farms_farmproduct"' in query or "from `farms_farmproduct`" in query for query in sql
    )
    assert not any('from "farms_farm"' in query or "from `farms_farm`" in query for query in sql)
