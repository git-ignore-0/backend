from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import (
    ShippingAddress,
    StoreOrder,
    StoreOrderAction,
    StoreOrderLine,
    StoreQuotaReservation,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockReservation,
)
from commerce.store_delivery import VIETNAM
from content.models import AdminCreationReceipt
from identity.models import AccountStatus, User
from memberships.models import (
    CouponUsage,
    CSACustomer,
    CSASelection,
    CSAStockHold,
    MembershipUsageOrder,
)
from referrals.models import PointTransaction, StoreReferralReward
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area
from tests.test_store_workflow_api import act, approve
from tests.test_store_workflow_api import admin as admin

pytestmark = pytest.mark.django_db

ORDERS = "/api/v1/admin/store-orders"
QUOTE = f"{ORDERS}/quote"


@pytest.fixture
def buyer(supported_area):
    user = User.objects.create_user(
        email="buyer@example.com",
        name="Người nhận",
        login_phone_number="0901234567",
        status=AccountStatus.ACTIVE,
    )
    ShippingAddress.objects.create(
        user=user,
        province_code=supported_area.province_code,
        ward_code=supported_area.ward_code,
        address="12 Nguyễn Huệ",
        recipient_name="Tên cũ",
        phone_number="+84999999999",
    )
    return user


def order_body(product, *, kind="new", buyer=None, quantity=2):
    today = timezone.now().astimezone(VIETNAM).date()
    body = {
        "kind": kind,
        "delivery_date": (today - timedelta(days=int(kind == "historical"))).isoformat(),
        "lines": [{"product_id": str(product.pk), "quantity": quantity}],
    }
    if buyer:
        body["buyer_user_id"] = str(buyer.pk)
    else:
        body["guest"] = guest_payload(product)["guest"]
    if kind == "historical":
        body["lines"][0]["unit_price_vnd"] = "34000"
        body["shipping_fee_vnd"] = "7000"
    return body


def quote_body(admin, body, *, code=200):
    response = admin.post(QUOTE, body, format="json")
    assert response.status_code == code, response.content
    return response.data


def confirm(admin, body):
    quote = quote_body(admin, body)["data"]
    return {**body, "confirmation": quote["confirmation"], "idempotency_key": str(uuid4())}


@pytest.mark.parametrize("kind", ["new", "historical"])
@pytest.mark.parametrize("registered", [True, False])
def test_creation_matrix(admin, buyer, store_product, payment_settings, kind, registered):
    body = order_body(store_product, kind=kind, buyer=buyer if registered else None)
    payload = confirm(admin, body)
    assert not StoreOrder.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    response = admin.post(ORDERS, payload, format="json")
    assert response.status_code == 201, response.data
    data = response.data["data"]
    saved = StoreOrder.objects.get(pk=data["id"])
    line = saved.lines.get()
    assert saved.buyer_user_id == (buyer.pk if registered else None)
    assert saved.recipient_name_snapshot == ("Người nhận" if registered else "Nguyễn Văn An")
    assert saved.phone_e164_snapshot == "+84901234567"
    assert saved.refund_status is None
    assert data["payment"] is None
    assert "guest_access_token" not in data
    assert data["creation"] == {
        "origin": "admin_historical" if kind == "historical" else "admin",
        "actor_id": str(AdminCreationReceipt.objects.get().actor_id),
    }
    assert response.data["meta"] == {
        "idempotent_replay": False,
        "action_receipt": {
            "action": "create_store_order",
            "resource_id": data["id"],
            "idempotency_key": payload["idempotency_key"],
        },
    }
    if kind == "historical":
        assert saved.status == "delivered"
        assert saved.payment_status == "received"
        assert (line.unit_price_vnd_snapshot, saved.shipping_fee_vnd, saved.total_vnd) == (
            34_000,
            7_000,
            75_000,
        )
        assert saved.created_at.astimezone(VIETNAM).date() > saved.delivery_date
        assert all(
            value is None
            for value in (
                saved.approved_at,
                saved.delivered_at,
                saved.payment_received_at,
                saved.transfer_reported_at,
                saved.transfer_content,
                saved.expires_at,
            )
        )
        assert data["allocation_suggestion"] == data["allocations"] == []
        assert data["source_inventory"] == {
            "refresh_required_before_approve": False,
            "lines": [
                {
                    "line_id": str(line.pk),
                    "product_id": str(line.product_id),
                    "required_quantity": 2,
                    "assigned_farm_id": None,
                    "suggested_farm_id": None,
                    "farms": [],
                }
            ],
            "candidates": [],
            "product_reservations": [],
        }
        assert not StoreStockReservation.objects.exists()
    else:
        assert saved.status == "pending_review"
        assert saved.workflow_version == 2
        assert saved.payment_status == "unconfirmed"
        assert saved.expires_at.astimezone(VIETNAM).date() == saved.delivery_date + timedelta(
            days=1
        )
        assert (line.unit_price_vnd_snapshot, saved.shipping_fee_vnd, saved.total_vnd) == (
            45_000,
            30_000,
            120_000,
        )
        assert StoreStockReservation.objects.get(line=line).quantity == 2
    for model in (
        StoreStockAllocation,
        StoreStockMovement,
        StoreOrderAction,
        StoreQuotaReservation,
        CouponUsage,
        StoreReferralReward,
        PointTransaction,
        CSACustomer,
        CSASelection,
        CSAStockHold,
        MembershipUsageOrder,
    ):
        assert not model.objects.exists()
    assert admin.get(ORDERS).data["data"][0]["creation"] == data["creation"]
    with CaptureQueriesContext(connection) as queries:
        detail = admin.get(f"{ORDERS}/{saved.pk}")
    assert detail.status_code == 200
    assert detail.data["data"]["creation"] == data["creation"]
    if kind == "historical":
        assert not any(
            name in query["sql"].lower()
            for query in queries
            for name in ("farmproduct", "stockreservation", "stockallocation", "stockmovement")
        )
    viewer = APIClient()
    if registered:
        viewer.force_authenticate(buyer, token=SimpleNamespace(scope="profile email"))
        history = viewer.get("/api/v1/store-orders").data["data"]
        assert history[0]["id"] == data["id"]
        public = viewer.get(f"/api/v1/store-orders/{saved.pk}")
    else:
        assert viewer.get(f"/api/v1/public/store-orders/{saved.pk}").status_code == 404
        public = viewer.post(
            "/api/v1/public/store-order-lookups",
            {"reference_code": data["reference_code"], "phone_number": "0901234567"},
            format="json",
        )
        assert viewer.get("/api/v1/public/store-order-lookups/current").status_code == 200
    assert public.status_code == 200
    assert public.data["data"]["id"] == data["id"]
    assert "creation" not in public.data["data"]
    assert "guest_access_token" not in public.data["data"]


def test_replay_recovery_and_identity_scope(admin, buyer, store_product, payment_settings):
    body = order_body(store_product)
    first_payload = confirm(admin, body)
    first = admin.post(ORDERS, first_payload, format="json")
    assert first.status_code == 201
    original = first.data["data"]
    assert approve(admin, original).status_code == 200
    key = first_payload["idempotency_key"]
    recovered = admin.get(ORDERS, {"creation_key": key})
    assert recovered.status_code == 200
    assert recovered.data["meta"]["action_receipt"] == first.data["meta"]["action_receipt"]
    assert recovered.data["data"]["status"] == "payment_pending"
    replay = admin.post(ORDERS, first_payload, format="json")
    assert replay.status_code == 200
    assert replay.data["meta"]["idempotent_replay"] is True
    assert replay.data["data"]["id"] == original["id"]
    assert StoreStockReservation.objects.count() == 1
    changed = deepcopy(first_payload)
    changed["lines"][0]["quantity"] = 1
    conflict = admin.post(ORDERS, changed, format="json")
    assert conflict.status_code == 409
    assert conflict.data["errors"][0]["code"] == "store_idempotency_conflict"

    other_admin = APIClient()
    buyer.groups.add(Group.objects.get(name="website_admin"))
    other_admin.force_authenticate(buyer, token=SimpleNamespace(scope="content:manage"))
    assert other_admin.get(ORDERS, {"creation_key": key}).status_code == 404
    assert other_admin.post(ORDERS, first_payload, format="json").status_code == 409
    public = APIClient().post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, idempotency_key=key, expected_available=8),
        format="json",
    )
    assert public.status_code == 201
    assert public.data["data"]["id"] != original["id"]


@pytest.mark.parametrize("kind", ["new", "historical"])
def test_changed_contact_requires_fresh_confirmation(
    admin, buyer, store_product, payment_settings, kind
):
    body = order_body(store_product, kind=kind, buyer=buyer)
    payload = confirm(admin, body)
    User.objects.filter(pk=buyer.pk).update(name="Người nhận mới")
    response = admin.post(ORDERS, payload, format="json")
    assert response.status_code == 409
    assert response.data["errors"][0]["code"] == "store_requote_required"
    assert not AdminCreationReceipt.objects.exists()
    assert not StoreOrder.objects.exists()


def test_historical_uses_catalog_history_without_shipping_or_payment_settings(
    admin,
    store_product,
    supported_area,
):
    store_product.status = "discontinued"
    store_product.is_published = False
    store_product.is_active = False
    store_product.save(update_fields=["status", "is_published", "is_active"])
    supported_area.delete()
    body = order_body(store_product, kind="historical", quantity=999)
    body["shipping_fee_vnd"] = body["lines"][0]["unit_price_vnd"] = "0"
    response = admin.post(ORDERS, confirm(admin, body), format="json")
    assert response.status_code == 201, response.data
    data = response.data["data"]
    assert data["total_vnd"] == "0"
    assert data["payment_status"] == "not_required"
    assert data["payment"] is None
    assert quote_body(admin, order_body(store_product), code=400)["errors"][0]["code"] == (
        "store_product_changed"
    )


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("cancel", {"reason": "Kiểm tra"}),
        ("deliver", {}),
        ("record-payment", {}),
        ("refund", {"amount_vnd": 75000, "note": "Kiểm tra"}),
        ("close-refund-request", {"reason": "Kiểm tra"}),
        ("restock", {"allocations": [{"allocation_id": str(uuid4()), "quantity": 1}]}),
        ("approve", {"sources": [{"line_id": str(uuid4()), "farm_id": str(uuid4())}]}),
    ],
)
def test_historical_is_terminal(admin, store_product, action, extra):
    body = order_body(store_product, kind="historical")
    order = admin.post(ORDERS, confirm(admin, body), format="json").data["data"]
    assert act(admin, order, action, **extra).status_code == 409
    assert not StoreOrderAction.objects.exists()


@pytest.mark.parametrize(
    ("now", "kind", "delivery", "valid"),
    [
        (datetime(2026, 12, 31, 16, 59, tzinfo=UTC), "historical", "2026-12-30", True),
        (datetime(2026, 12, 31, 16, 59, tzinfo=UTC), "historical", "2026-12-31", False),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "historical", "2026-12-31", True),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "historical", "2027-01-02", False),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "new", "2026-12-31", False),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "new", "2027-01-01", True),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "new", "2027-01-02", True),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "new", "2027-01-08", True),
        (datetime(2026, 12, 31, 17, 0, tzinfo=UTC), "new", "2027-01-09", False),
        (datetime(2026, 4, 30, 17, 0, tzinfo=UTC), "historical", "2026-04-30", True),
        (datetime(2026, 4, 30, 17, 0, tzinfo=UTC), "historical", "2026-04-31", False),
    ],
)
def test_vietnam_date_boundary(admin, store_product, monkeypatch, now, kind, delivery, valid):
    batch = store_product.farm_offers.get().stock_batches.get()
    type(batch).objects.filter(pk=batch.pk).update(
        start_date=now.date() - timedelta(days=2), end_date=now.date() + timedelta(days=10)
    )
    monkeypatch.setattr(timezone, "now", lambda: now)
    body = order_body(store_product, kind=kind)
    body["delivery_date"] = delivery
    quote_body(admin, body, code=200 if valid else 400)


def test_quote_create_and_recovery_require_admin_scope_and_group(
    admin,
    active_user,
    buyer,
    store_product,
):
    body = order_body(store_product)
    for user, scope in [(None, ""), (active_user, "profile email"), (buyer, "content:manage")]:
        client = APIClient()
        if user:
            client.force_authenticate(user, token=SimpleNamespace(scope=scope))
        assert client.post(QUOTE, body, format="json").status_code in {401, 403}
        assert client.post(ORDERS, body, format="json").status_code in {401, 403}
        assert client.get(ORDERS, {"creation_key": str(uuid4())}).status_code in {401, 403}

    assert admin.get(f"{ORDERS}?creation_key={uuid4()}&creation_key={uuid4()}").status_code == 400
    assert (
        admin.get(ORDERS, {"creation_key": str(uuid4()), "status": "delivered"}).status_code == 400
    )
    create = {**body, "actor_id": str(buyer.pk)}
    assert admin.post(ORDERS, create, format="json").status_code == 400


def test_admin_fields_rejected_at_public_checkout_boundary(store_product, payment_settings):
    response = APIClient().post(
        "/api/v1/public/store-checkouts",
        {**guest_payload(store_product), "kind": "new", "buyer_user_id": str(uuid4())},
        format="json",
    )
    assert response.status_code == 400
    assert "kind" in response.data and "buyer_user_id" in response.data
    assert not StoreOrder.objects.exists()


@pytest.mark.parametrize(
    ("change", "code", "field"),
    [
        ({"guest": None}, 400, "guest"),
        ({"guest": {}}, 400, "guest"),
        ({"guest": {"phone_number": "wrong"}}, 400, "guest"),
        ({"guest": {"ward_code": "00000"}}, 400, "guest"),
        ({"buyer_user_id": str(uuid4())}, 400, "non_field_errors"),
        ({"lines": []}, 400, "lines"),
        ({"lines": [{"quantity": 0}]}, 400, "lines"),
        ({"lines": [{"quantity": -1}]}, 400, "lines"),
        ({"lines": [{"quantity": 1.5}]}, 400, "lines"),
        ({"lines": [{"quantity": 1.0}]}, 400, "lines"),
        ({"lines": [{"quantity": "2"}]}, 400, "lines"),
        ({"lines": [{"quantity": 1000}]}, 400, "lines"),
        ({"lines": [{"farm_id": str(uuid4())}]}, 400, "lines"),
        ({"lines": [{"use_quota": False}]}, 400, "lines"),
        ({"lines": [{"unit_price_vnd": "1"}]}, 400, "lines"),
        ({"shipping_fee_vnd": "0"}, 400, "shipping_fee_vnd"),
        ({"voucher": "token"}, 400, "voucher"),
        ({"status": "delivered"}, 400, "status"),
        ({"buyer_user_id": None}, 400, "buyer_user_id"),
    ],
)
def test_strict_quote_body(admin, store_product, change, code, field):
    body = order_body(store_product)
    updates = deepcopy(change)
    if updates.get("lines"):
        updates["lines"] = [{**body["lines"][0], **updates["lines"][0]}]
    if isinstance(updates.get("guest"), dict) and updates["guest"]:
        updates["guest"] = {**body["guest"], **updates["guest"]}
    error = quote_body(admin, {**body, **updates}, code=code)
    assert field in error


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit_price_vnd", -1),
        ("unit_price_vnd", "1.2"),
        ("unit_price_vnd", 1.0),
        ("unit_price_vnd", "9007199254740992"),
        ("shipping_fee_vnd", "1e3"),
        ("shipping_fee_vnd", "9007199254740992"),
        ("shipping_fee_vnd", None),
    ],
)
def test_historical_money_fields(admin, store_product, field, value):
    body = order_body(store_product, kind="historical")
    target = body if field == "shipping_fee_vnd" else body["lines"][0]
    target[field] = value
    quote_body(admin, body, code=400)


def test_historical_total_overflow_and_maximum(admin, store_product):
    body = order_body(store_product, kind="historical", quantity=1)
    body["lines"][0]["unit_price_vnd"] = "9007199254740991"
    too_large = quote_body(admin, body, code=400)
    assert too_large["errors"][0]["code"] == "store_order_amount_out_of_range"
    body["shipping_fee_vnd"] = "0"
    response = admin.post(ORDERS, confirm(admin, body), format="json")
    assert response.status_code == 201, response.data
    assert response.data["data"]["total_vnd"] == "9007199254740991"


@pytest.mark.parametrize(
    ("condition", "code", "error"),
    [
        ("removed", 404, "store_buyer_not_found"),
        ("ineligible", 400, "store_buyer_ineligible"),
        ("missing_address", 400, "store_shipping_address_required"),
        ("invalid_contact", 400, "store_shipping_address_required"),
    ],
)
def test_system_buyer_eligibility(admin, buyer, store_product, condition, code, error):
    body = order_body(store_product, buyer=buyer)
    if condition == "removed":
        body["buyer_user_id"] = str(uuid4())
    elif condition == "ineligible":
        User.objects.filter(pk=buyer.pk).update(status=AccountStatus.SUSPENDED, is_active=False)
    elif condition == "missing_address":
        ShippingAddress.objects.filter(user=buyer).delete()
    else:
        User.objects.filter(pk=buyer.pk).update(login_phone_number=None)
    assert quote_body(admin, body, code=code)["errors"][0]["code"] == error


@pytest.mark.parametrize("kind", ["new", "historical"])
def test_user_address_cannot_be_overridden(admin, buyer, store_product, kind):
    body = order_body(store_product, buyer=buyer, kind=kind)
    body["guest"] = guest_payload(store_product)["guest"]
    quote_body(admin, body, code=400)
    del body["guest"]
    body["shipping_address"] = {"address": "Khác"}
    assert "shipping_address" in quote_body(admin, body, code=400)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("duplicate", "store_duplicate_product"),
        ("missing", "store_product_changed"),
        ("draft", "store_product_changed"),
        ("category_disabled", "store_product_changed"),
    ],
)
def test_catalog_eligibility(admin, store_product, change, error):
    body = order_body(store_product, kind="historical" if change == "draft" else "new")
    if change == "duplicate":
        body["lines"] *= 2
    elif change == "missing":
        body["lines"][0]["product_id"] = str(uuid4())
    elif change == "draft":
        type(store_product).objects.filter(pk=store_product.pk).update(
            status="draft", is_published=False
        )
    else:
        type(store_product.category).objects.filter(pk=store_product.category_id).update(
            is_active=False
        )
    assert quote_body(admin, body, code=400)["errors"][0]["code"] == error


@pytest.mark.parametrize("change", ["price", "availability", "shipping", "address", "eligibility"])
def test_new_order_rechecks_before_write(
    admin,
    buyer,
    store_product,
    payment_settings,
    supported_area,
    change,
):
    body = order_body(store_product, buyer=buyer)
    payload = confirm(admin, body)
    if change == "price":
        type(store_product).objects.filter(pk=store_product.pk).update(retail_price_vnd=47_000)
    elif change == "availability":
        store_product.farm_offers.get().stock_batches.update(quantity_units=8)
    elif change == "shipping":
        type(supported_area).objects.filter(pk=supported_area.pk).update(shipping_fee_vnd=40_000)
    elif change == "address":
        ShippingAddress.objects.filter(user=buyer).update(address="Địa chỉ mới")
    else:
        User.objects.filter(pk=buyer.pk).update(status=AccountStatus.SUSPENDED, is_active=False)
    response = admin.post(ORDERS, payload, format="json")
    if change == "eligibility":
        assert response.status_code == 400
        assert response.data["errors"][0]["code"] == "store_buyer_ineligible"
    else:
        assert response.status_code == 409
        assert response.data["errors"][0]["code"] == "store_requote_required"
    assert not StoreOrder.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    assert not StoreStockReservation.objects.exists()


def test_insufficient_stock_is_rejected_by_quote_and_submit(admin, store_product, payment_settings):
    body = order_body(store_product, quantity=8)
    payload = confirm(admin, body)
    store_product.farm_offers.get().stock_batches.update(quantity_units=5)
    assert quote_body(admin, body, code=409)["errors"][0]["code"] == "store_stock_changed"
    response = admin.post(ORDERS, payload, format="json")
    assert response.status_code == 409
    assert response.data["errors"][0]["code"] == "store_stock_changed"
    assert not AdminCreationReceipt.objects.exists()
    assert not StoreOrder.objects.exists()


@pytest.mark.parametrize("kind", ["new", "historical"])
def test_order_lines_reservations_and_receipt_rollback_together(
    admin,
    store_product,
    payment_settings,
    monkeypatch,
    kind,
):
    payload = confirm(admin, order_body(store_product, kind=kind))
    if kind == "historical":
        manager = StoreOrderLine.objects
        method = "bulk_create"
    else:
        manager = StoreStockReservation.objects
        method = "create"
    write = getattr(manager, method)

    def write_then_fail(*args, **kwargs):
        write(*args, **kwargs)
        assert StoreOrder.objects.count() == 1
        assert StoreOrderLine.objects.count() == 1
        raise RuntimeError("write failed before commit")

    with monkeypatch.context() as patch:
        patch.setattr(manager, method, write_then_fail)
        with pytest.raises(RuntimeError, match="write failed before commit"):
            admin.post(ORDERS, payload, format="json")
    for model in (StoreOrder, StoreOrderLine, StoreStockReservation, AdminCreationReceipt):
        assert not model.objects.exists()
    assert admin.get(ORDERS, {"creation_key": payload["idempotency_key"]}).status_code == 404
    assert admin.post(ORDERS, payload, format="json").status_code == 201


def test_recovery_after_response_failure(admin, store_product, payment_settings, monkeypatch):
    from commerce.store_views import AdminStoreOrderListView

    payload = confirm(admin, order_body(store_product))

    def fail_response(*args, **kwargs):
        raise RuntimeError("response lost after write")

    with monkeypatch.context() as patch:
        patch.setattr(AdminStoreOrderListView, "creation_response", fail_response)
        with pytest.raises(RuntimeError, match="response lost after write"):
            admin.post(ORDERS, payload, format="json")
    recovery = admin.get(ORDERS, {"creation_key": payload["idempotency_key"]})
    assert recovery.status_code == 200
    assert recovery.data["meta"]["action_receipt"]["resource_id"] == str(
        StoreOrder.objects.get().pk
    )
    assert StoreStockReservation.objects.count() == 1


@pytest.mark.parametrize("operation", ["cancel", "expiry", "deliver"])
def test_admin_new_order_uses_standard_lifecycle(admin, store_product, payment_settings, operation):
    payload = confirm(admin, order_body(store_product))
    order = admin.post(ORDERS, payload, format="json").data["data"]
    if operation == "cancel":
        assert act(admin, order, "cancel", reason="Kiểm tra").status_code == 200
    elif operation == "expiry":
        StoreOrder.objects.filter(pk=order["id"]).update(expires_at=timezone.now())
        detail = admin.get(f"{ORDERS}/{order['id']}")
        assert detail.data["data"]["status"] == "cancelled"
    else:
        saved = StoreOrder.objects.get(pk=order["id"])
        line = saved.lines.get()
        farm = store_product.farm_offers.get().farm
        approval = act(
            admin, order, "approve", sources=[{"line_id": str(line.pk), "farm_id": str(farm.pk)}]
        )
        assert approval.status_code == 200, approval.data
        assert StoreStockMovement.objects.get().quantity == 2
        assert act(admin, order, "record-payment").status_code == 200
        assert act(admin, order, "deliver").status_code == 200
        assert StoreOrder.objects.get().status == "delivered"
    assert StoreStockReservation.objects.get().released_at is not None


def test_historical_quote_product_queries_are_batched(admin, store_product):
    product_model = type(store_product)
    more = product_model.objects.bulk_create(
        [
            product_model(
                category=store_product.category,
                sku=f"SAMPLE-{index}",
                slug=f"sample-{index}",
                unit_size=1,
                unit_label_vi="kg",
                unit_label_en="kg",
                retail_price_vnd=1,
                status="active",
                is_published=True,
            )
            for index in range(49)
        ]
    )
    body = order_body(store_product, kind="historical")
    with CaptureQueriesContext(connection) as single:
        quote_body(admin, body)
    body["lines"].extend(
        {"product_id": str(product.pk), "quantity": 1, "unit_price_vnd": "1"} for product in more
    )
    with CaptureQueriesContext(connection) as batch:
        response = quote_body(admin, body)
    assert len(response["data"]["lines"]) == 50
    assert len(batch) == len(single)
    assert not any(
        "farmproduct" in query["sql"].lower() or "stockreservation" in query["sql"].lower()
        for query in batch
    )


def test_registered_buyer_cannot_mutate_historical_order(admin, buyer, store_product):
    payload = confirm(admin, order_body(store_product, kind="historical", buyer=buyer))
    order = admin.post(ORDERS, payload, format="json").data["data"]
    client = APIClient()
    client.force_authenticate(buyer, token=SimpleNamespace(scope="profile email"))
    for action in ("report-transfer", "cancel", "request-refund"):
        body = {} if action == "report-transfer" else {"idempotency_key": str(uuid4())}
        response = client.post(
            f"/api/v1/public/store-orders/{order['id']}/{action}", body, format="json"
        )
        assert response.status_code == 409
    assert not StoreOrderAction.objects.exists()
