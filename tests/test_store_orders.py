import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from decimal import Decimal
from threading import Barrier
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import (
    Category,
    CategoryTranslation,
    Product,
    ProductTranslation,
    ShippingAddress,
    ShippingArea,
    StoreOrder,
    StoreOrderAction,
    StoreOrderLine,
    StoreOrderStatus,
    StorePaymentStatus,
    StoreQuotaReservation,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockReservation,
)
from content.models import MediaAsset
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships.models import (
    CSACustomer,
    Membership,
    MembershipCycle,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    MembershipProductSnapshot,
    MembershipStatus,
    QuotaPolicy,
)
from memberships.models import (
    Product as QuotaGroup,
)
from memberships.services import InsufficientQuota, create_usage_order, ensure_due_cycles

PROVINCE = "66"
WARD = "22015"


def assert_admin_action_receipt(response, order_id, action, key):
    assert response.status_code == 200, response.json()
    assert response["Cache-Control"] == "private, no-store"
    body = response.json()
    assert body["data"]["id"] == str(order_id)
    assert body["meta"]["action_receipt"] == {
        "order_id": str(order_id),
        "action": action,
        "idempotency_key": str(key),
    }
    assert (
        StoreOrderAction.objects.filter(
            order_id=order_id, action=action, idempotency_key=key
        ).count()
        == 1
    )


@pytest.fixture(autouse=True)
def supported_area(db):
    return ShippingArea.objects.create(
        province_code=PROVINCE,
        ward_code=WARD,
        shipping_fee_vnd=30_000,
        is_active=True,
    )


@pytest.fixture
def zero_shipping_fee(supported_area):
    """Quota/payment workflow tests explicitly use a zero-fee delivery area."""
    supported_area.shipping_fee_vnd = 0
    supported_area.save(update_fields=["shipping_fee_vnd"])


@pytest.fixture
def payment_settings(monkeypatch):
    monkeypatch.setattr(
        "memberships.banks.get_transfer_banks",
        lambda: [{"bin": "970436", "name": "Vietcombank"}],
    )
    return MembershipPaymentSettings.objects.create(
        direct_transfer_enabled=True,
        bank_bin="970436",
        bank_name="Vietcombank",
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
    )


@pytest.fixture
def store_product():
    category = Category.objects.create(slug="vegetables", is_active=True)
    CategoryTranslation.objects.bulk_create(
        [
            CategoryTranslation(category=category, locale="vi", name="Rau"),
            CategoryTranslation(category=category, locale="en", name="Vegetables"),
        ]
    )
    image = MediaAsset.objects.create(
        file="products/tomato.webp",
        variants=[],
        width=800,
        height=600,
        original_name="tomato.webp",
    )
    product = Product.objects.create(
        category=category,
        sku="TOMATO",
        slug="tomato",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=45_000,
        primary_image=image,
        status="active",
        is_published=True,
    )
    ProductTranslation.objects.bulk_create(
        [
            ProductTranslation(product=product, locale="vi", name="Cà chua"),
            ProductTranslation(product=product, locale="en", name="Tomato"),
        ]
    )
    farm = Farm.objects.create(
        name_vi="Nông trại A",
        name_en="Farm A",
        location_vi="Đắk Lắk",
        location_en="Dak Lak",
    )
    offer = FarmProduct.objects.create(farm=farm, product=product)
    FarmProductStockBatch.objects.create(
        farm_product=offer,
        start_date=timezone.localdate() - timedelta(days=1),
        end_date=timezone.localdate() + timedelta(days=2),
        quantity_units=10,
    )
    return product


def guest_payload(product, *, quantity=2, idempotency_key=None, expected_available=10):
    return {
        "delivery_date": timezone.localdate(),
        "idempotency_key": str(idempotency_key or uuid.uuid4()),
        "locale": "vi",
        "lines": [
            {
                "product_id": str(product.pk),
                "quantity": quantity,
                "use_quota": False,
                "expected_unit_price_vnd": 45_000,
                "expected_available_quantity": expected_available,
                "expected_quota_quantity": 0,
            }
        ],
        "expected_shipping_fee_vnd": 30_000,
        "expected_total_vnd": quantity * 45_000 + 30_000,
        "guest": {
            "recipient_name": "Nguyễn Văn An",
            "phone_number": "0901234567",
            "province_code": PROVINCE,
            "ward_code": WARD,
            "address": "Số 12 Nguyễn Huệ",
        },
    }


def legacy_checkout_snapshot(data):
    """Represent an existing v1 row, without changing checkout request identity.

    Only compatibility tests call this explicitly. New checkout tests use v2.
    """
    order = StoreOrder.objects.get(pk=data["id"])
    StoreOrder.objects.filter(pk=order.pk).update(
        workflow_version=1,
        status="payment_pending" if order.total_vnd else "pending_review",
        expires_at=order.created_at + timedelta(minutes=5)
        if order.total_vnd
        else order.created_at + timedelta(hours=8),
    )
    return data


def historical_cancelled_snapshot(order_id, actor):
    """A previously cancelled v1 order whose batches await manual restock.

    New cancellation now reverses all retail stock, including v1. This fixture
    represents historical rows; it must not call today's cancellation service.
    """
    from commerce.models import StoreOrderAction
    from commerce.store_orders import request_fingerprint

    order = StoreOrder.objects.get(pk=order_id)
    assert order.workflow_version == 1
    assert order.status == StoreOrderStatus.PENDING_DELIVERY
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancelled_by = actor
    order.cancellation_reason = "Historical cancellation awaiting restock"
    order.expires_at = None
    if order.payment_status == StorePaymentStatus.RECEIVED:
        order.payment_status = StorePaymentStatus.REFUND_REQUIRED
    order.save()
    StoreOrderAction.objects.create(
        order=order,
        action="cancel",
        idempotency_key=uuid.uuid4(),
        actor=actor,
        request_fingerprint=request_fingerprint({"reason": order.cancellation_reason}),
        context={"reason": order.cancellation_reason},
    )
    return order


@pytest.mark.django_db
def test_guest_checkout_snapshots_price_shipping_bank_and_reserves_stock(
    store_product, payment_settings, active_user
):
    response = APIClient().post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )

    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["status"] == "pending_review"
    assert data["workflow_version"] == 2
    assert data["payment"] is None
    assert data["subtotal_vnd"] == "90000"
    assert data["shipping_fee_vnd"] == "30000"
    assert data["total_vnd"] == "120000"
    order = StoreOrder.objects.get(pk=data["id"])
    assert order.bank_bin_snapshot == "970436"
    assert order.account_number_snapshot == "123456789"
    assert order.account_name_snapshot == "NATURAL FARMING VIETNAM"
    assert order.transfer_content
    assert order.payment_received_at is None
    assert data["expires_at"] is not None
    assert data["server_time"] is not None
    order = StoreOrder.objects.get(pk=data["id"])
    assert order.lines.get().stock_reservations.get().quantity == 2
    from commerce.store_views import order_payload

    approved = approve_review_order(data, active_user)
    assert order_payload(approved)["payment"]["qr_payload"] == {
        "acqId": "970436",
        "accountNo": "123456789",
        "accountName": "NATURAL FARMING VIETNAM",
        "amount": 120000,
        "addInfo": order.transfer_content,
    }
    assert approved.payment_received_at is None


@pytest.mark.django_db
def test_order_images_use_current_primary_image_without_changing_order_snapshot(
    store_product, payment_settings
):
    client = APIClient()
    response = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["lines"][0]["image_url"] == (
        "http://testserver" + store_product.primary_image.file.url
    )
    from rest_framework.test import APIRequestFactory

    from commerce.store_views import order_payload

    store_product.primary_image = None
    store_product.save(update_fields=["primary_image"])
    refreshed = order_payload(
        StoreOrder.objects.get(pk=data["id"]), request=APIRequestFactory().get("/")
    )
    assert refreshed["lines"][0]["image_url"] is None
    assert refreshed["lines"][0]["name"] == data["lines"][0]["name"]
    assert refreshed["lines"][0]["unit_price_vnd"] == data["lines"][0]["unit_price_vnd"]


@pytest.mark.django_db
def test_create_checkout_idempotency_replays_without_extending_deadline(
    store_product, payment_settings
):
    key = uuid.uuid4()
    client = APIClient()
    first = client.post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, idempotency_key=key),
        format="json",
    )
    second = client.post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, idempotency_key=key),
        format="json",
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["data"]["id"] == first.json()["data"]["id"]
    assert second.json()["data"]["expires_at"] == first.json()["data"]["expires_at"]
    assert second.json()["data"]["guest_access_token"] == first.json()["data"]["guest_access_token"]
    assert StoreOrder.objects.count() == 1


@pytest.mark.django_db
def test_checkout_requires_requote_when_price_changes(store_product, payment_settings):
    payload = guest_payload(store_product)
    store_product.retail_price_vnd = 50_000
    store_product.save(update_fields=["retail_price_vnd", "updated_at"])

    response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")

    assert response.status_code == 409
    assert response.json()["errors"] == [
        {
            "code": "store_requote_required",
            "context": {
                "reason": "price_changed",
                "product_id": str(store_product.pk),
                "unit_price_vnd": 50_000,
            },
        }
    ]
    assert not StoreOrder.objects.exists()


@pytest.mark.django_db
def test_legacy_paid_order_report_transfer_is_idempotent_and_starts_eight_hour_deadline(
    store_product, payment_settings
):
    created = (
        APIClient()
        .post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product),
            format="json",
        )
        .json()["data"]
    )
    legacy_checkout_snapshot(created)
    client = APIClient()
    first = client.post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )
    second = client.post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )

    assert first.status_code == 200
    assert first.json()["data"]["status"] == "pending_review"
    reported = StoreOrder.objects.get(pk=created["id"])
    assert reported.workflow_version == 1
    assert reported.expires_at == reported.transfer_reported_at + timedelta(hours=8)
    assert second.status_code == 200
    assert second.json()["data"]["expires_at"] == first.json()["data"]["expires_at"]


def attach_active_membership(user, product):
    if not user.login_phone_number:
        user.login_phone_number = "0901234567"
        user.save(update_fields=["login_phone_number"])
    group = QuotaGroup.objects.create(
        name="Rau", unit_size=Decimal("1.000"), unit_label_vi="kg", unit_label_en="kg"
    )
    product.csa_quota_group = group
    product.csa_quota_cost = 2
    product.save(update_fields=["csa_quota_group", "csa_quota_cost", "updated_at"])
    customer = CSACustomer.objects.create(
        user=user,
        name=user.name,
        phone_e164=user.login_phone_number,
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    package = MembershipPackage.objects.create(name="CSA", quota_policy=QuotaPolicy.EXPIRE)
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=1, monthly_price_vnd=1_000_000
    )
    today = timezone.localdate()
    membership = Membership.objects.create(
        customer=customer,
        package=package,
        price_option=option,
        package_name_snapshot="CSA",
        monthly_price_vnd_snapshot=1_000_000,
        total_price_vnd_snapshot=1_000_000,
        duration_months_snapshot=1,
        quota_policy_snapshot=QuotaPolicy.EXPIRE,
        farmbrite_order_reference=f"TEST-{uuid.uuid4()}",
        farmbrite_order_reference_key=str(uuid.uuid4()),
        start_date=today,
        end_date=today + timedelta(days=30),
        status=MembershipStatus.ACTIVE,
    )
    snapshot = MembershipProductSnapshot.objects.create(
        membership=membership,
        product=group,
        product_name="Rau",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        quota_units_per_cycle=4,
    )
    cycle = MembershipCycle.objects.create(
        membership=membership,
        sequence=1,
        start_date=today,
        end_date=today + timedelta(days=30),
    )
    MembershipCycleQuota.objects.create(
        cycle=cycle,
        product_snapshot=snapshot,
        granted_units=4,
        remaining_units=4,
    )
    ShippingAddress.objects.create(
        user=user,
        recipient_name=user.name,
        phone_number=user.login_phone_number,
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    return membership


@pytest.mark.django_db
def test_membership_and_csa_cart_alone_do_not_waive_shipping(
    active_user, access_token, store_product, payment_settings
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 3,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 30_000,
            "expected_total_vnd": 75_000,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["lines"][0]["quota_quantity"] == 2
    assert data["lines"][0]["cash_quantity"] == 1
    assert data["shipping_fee_vnd"] == "30000"
    assert data["shipping_discount_reason"] is None
    assert data["total_vnd"] == "75000"


@pytest.mark.django_db
def test_zero_total_checkout_skips_qr_and_enters_pending_review(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    assert response.json()["data"]["status"] == "pending_review"
    assert response.json()["data"]["payment"] is None
    assert response.json()["data"]["total_vnd"] == "0"


@pytest.mark.django_db
def test_expiry_command_releases_stock_and_blocks_late_report(store_product, payment_settings):
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())

    response = APIClient().post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "store_order_expired"
    order = StoreOrder.objects.get(pk=created["id"])
    assert order.status == StoreOrderStatus.CANCELLED
    assert not order.lines.get().stock_reservations.filter(released_at__isnull=True).exists()


@pytest.mark.django_db
def test_admin_approval_uses_fefo_and_is_replay_safe(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    created = (
        APIClient()
        .post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, quantity=3),
            format="json",
        )
        .json()["data"]
    )
    first_batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    first_batch.quantity_units = 2
    first_batch.save(update_fields=["quantity_units", "updated_at"])
    second_batch = FarmProductStockBatch.objects.create(
        farm_product=first_batch.farm_product,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=5),
        quantity_units=5,
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    detail = admin.get(f"/api/v1/admin/store-orders/{created['id']}")
    suggestion = detail.json()["data"]["allocation_suggestion"]
    assert suggestion == [
        {
            "line_id": detail.json()["data"]["lines"][0]["id"],
            "batch_id": str(first_batch.pk),
            "quantity": 2,
        },
        {
            "line_id": detail.json()["data"]["lines"][0]["id"],
            "batch_id": str(second_batch.pk),
            "quantity": 1,
        },
    ]
    payload = {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion}
    approved = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve", payload, format="json"
    )
    replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve", payload, format="json"
    )

    assert approved.status_code == 200, approved.json()
    assert approved.json()["data"]["status"] == "payment_pending"
    for response in (approved, replay):
        assert_admin_action_receipt(response, created["id"], "approve", payload["idempotency_key"])
    assert StoreOrder.objects.get(pk=created["id"]).payment_status == StorePaymentStatus.UNCONFIRMED


@pytest.mark.django_db
@pytest.mark.parametrize("has_email", [True, False], ids=["email", "no-email"])
def test_user_list_is_owner_scoped(
    active_user, access_token, store_product, payment_settings, has_email, mailoutbox
):
    active_user.login_phone_number = "0901234567" if has_email else "0987654321"
    if not has_email:
        active_user.emailaddress_set.all().delete()
        active_user.email = None
    active_user.save(update_fields=["email", "login_phone_number"])
    ShippingAddress.objects.create(
        user=active_user,
        recipient_name=active_user.name,
        phone_number=active_user.login_phone_number,
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    member = APIClient()
    member.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = member.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 1,
                    "use_quota": False,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 0,
                }
            ],
            "expected_shipping_fee_vnd": 30_000,
            "expected_total_vnd": 75_000,
            "shipping_address_updated_at": ShippingAddress.objects.get(
                user=active_user
            ).updated_at.isoformat(),
        },
        format="json",
    )
    assert created.status_code == 201, created.json()

    order = StoreOrder.objects.get(pk=created.json()["data"]["id"])
    assert order.buyer_user_id == active_user.pk
    assert order.phone_e164_snapshot == ("+84901234567" if has_email else "+84987654321")
    assert not mailoutbox

    listed = member.get("/api/v1/store-orders")

    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [created.json()["data"]["id"]]


@pytest.mark.django_db
def test_public_available_quantity_subtracts_live_reservation_and_approved_sale(
    active_user, access_token, store_product, payment_settings
):
    created = (
        APIClient()
        .post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, quantity=3),
            format="json",
        )
        .json()["data"]
    )
    listed = APIClient().get("/api/v1/public/store-products?locale=vi")
    assert listed.json()["data"][0]["available_quantity"] == 7

    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    approved = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    )
    assert approved.status_code == 200, approved.json()

    after = APIClient().get("/api/v1/public/store-products?locale=vi")
    assert after.json()["data"][0]["available_quantity"] == 7


@pytest.mark.django_db
@pytest.mark.parametrize("lookup_phone", ["0901234567", "+84901234567"])
def test_guest_lookup_uses_generic_failure_and_http_only_session(
    store_product, payment_settings, lookup_phone
):
    payload = guest_payload(store_product)
    payload["guest"]["phone_number"] = "+84901234567"
    created = (
        APIClient().post("/api/v1/public/store-checkouts", payload, format="json").json()["data"]
    )
    assert created["shipping_address"]["phone_number"] == "+84901234567"
    client = APIClient()
    wrong_code = client.post(
        "/api/v1/public/store-order-lookups",
        {"reference_code": "SO-NOT-FOUND", "phone_number": "0901234567"},
        format="json",
    )
    wrong_phone = client.post(
        "/api/v1/public/store-order-lookups",
        {"reference_code": created["reference_code"], "phone_number": "0909999999"},
        format="json",
    )
    assert wrong_code.status_code == wrong_phone.status_code == 404
    assert (
        wrong_code.json() == wrong_phone.json() == {"errors": [{"code": "store_order_not_found"}]}
    )

    found = client.post(
        "/api/v1/public/store-order-lookups",
        {"reference_code": created["reference_code"], "phone_number": lookup_phone},
        format="json",
    )
    assert found.status_code == 200
    cookie = found.cookies["nfv_store_lookup"]
    assert cookie["httponly"]
    current = client.get("/api/v1/public/store-order-lookups/current")
    assert current.status_code == 200
    assert current.json()["data"]["id"] == created["id"]


@pytest.mark.django_db
def test_admin_order_search_matches_domestic_phone_and_business_fields(
    active_user, access_token, store_product, payment_settings
):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = APIClient().post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )
    assert response.status_code == 201
    order = response.json()["data"]
    path = "/api/v1/admin/store-orders"
    for value in ("0901234567", "090", "4567", "Nguyễn Văn An", order["reference_code"]):
        result = admin.get(path, {"search": value, "page_size": 1})
        assert result.status_code == 200
        assert result.json()["meta"]["total"] == 1
        assert [row["id"] for row in result.json()["data"]] == [order["id"]]
    for value in ("84901234567", "+84901234567", "8490"):
        assert admin.get(path, {"search": value}).json()["data"] == []


@pytest.mark.django_db
def test_legacy_cancelled_paid_late_order_can_be_refunded_idempotently(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    # Legacy paid orders could receive funds before supply approval.
    StoreOrder.objects.filter(pk=created["id"]).update(
        workflow_version=1,
        status=StoreOrderStatus.PAYMENT_PENDING,
        expires_at=timezone.now() + timedelta(minutes=5),
    )
    cancel_key = str(uuid.uuid4())
    cancelled = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/cancel",
        {
            "idempotency_key": cancel_key,
            "reason": "Không thấy tiền",
            "reconciliation_note": "Kiểm tra 2 lần",
        },
        format="json",
    )
    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["data"]["status"] == "cancelled"
    assert_admin_action_receipt(cancelled, created["id"], "cancel", cancel_key)
    assert (
        not StoreOrder.objects.get(pk=created["id"])
        .lines.get()
        .stock_reservations.filter(released_at__isnull=True)
        .exists()
    )

    payment_key = str(uuid.uuid4())
    received = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/record-payment",
        {"idempotency_key": payment_key, "note": "Tiền đến muộn"},
        format="json",
    )
    replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/record-payment",
        {"idempotency_key": payment_key, "note": "Tiền đến muộn"},
        format="json",
    )
    assert received.json()["data"]["payment_status"] == "refund_required"
    for response in (received, replay):
        assert_admin_action_receipt(response, created["id"], "record_payment", payment_key)

    refund_key = str(uuid.uuid4())
    refunded = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/refund",
        {"idempotency_key": refund_key, "amount_vnd": 120000, "note": "Đã chuyển trả"},
        format="json",
    )
    refund_replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/refund",
        {"idempotency_key": refund_key, "amount_vnd": 120000, "note": "Đã chuyển trả"},
        format="json",
    )
    assert refunded.status_code == 200, refunded.json()
    assert refunded.json()["data"]["payment_status"] == "refunded"
    for response in (refunded, refund_replay):
        assert_admin_action_receipt(response, created["id"], "refund", refund_key)
    later_payment_replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/record-payment",
        {"idempotency_key": payment_key, "note": "Tiền đến muộn"},
        format="json",
    )
    assert_admin_action_receipt(later_payment_replay, created["id"], "record_payment", payment_key)
    assert later_payment_replay.json()["data"]["payment_status"] == "refunded"
    cancel_replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/cancel",
        {
            "idempotency_key": cancel_key,
            "reason": "Không thấy tiền",
            "reconciliation_note": "Kiểm tra 2 lần",
        },
        format="json",
    )
    assert_admin_action_receipt(cancel_replay, created["id"], "cancel", cancel_key)
    assert cancel_replay.json()["data"]["payment_status"] == "refunded"


@pytest.mark.django_db
def test_cancel_after_approval_reverses_quota_and_batch_ledger(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    member = APIClient()
    member.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = member.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    ).json()["data"]
    suggestion = member.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    member.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    )
    quota = MembershipCycleQuota.objects.get(cycle__membership__customer__user=active_user)
    quota.refresh_from_db()
    assert quota.remaining_units == 0

    cancelled = member.post(
        f"/api/v1/admin/store-orders/{created['id']}/cancel",
        {
            "idempotency_key": str(uuid.uuid4()),
            "reason": "Hàng hỏng",
            "reconciliation_note": "Không nhập kho lại",
        },
        format="json",
    )
    assert cancelled.status_code == 200, cancelled.json()
    quota.refresh_from_db()
    assert quota.remaining_units == 4
    assert StoreStockMovement.objects.filter(kind="restock").count() == 1


@pytest.mark.django_db
def test_stock_batch_cannot_be_cancelled_while_product_reservation_depends_on_it(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    batch.quantity_units = 2
    batch.save(update_fields=["quantity_units", "updated_at"])
    created = APIClient().post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, quantity=2, expected_available=2),
        format="json",
    )
    assert created.status_code == 201
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = admin.post(
        f"/api/v1/admin/farms/{batch.farm_product.farm_id}/products/{store_product.pk}/batches/{batch.pk}/cancel",
        {"version": batch.version},
        format="json",
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "stock_reserved"


@pytest.mark.django_db
def test_expiry_command_is_repeatable(store_product, payment_settings, monkeypatch):
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    deadline = StoreOrder.objects.get(pk=created["id"]).expires_at
    monkeypatch.setattr(timezone, "now", lambda: deadline)

    call_command("expire_store_orders", batch_size=50)
    call_command("expire_store_orders", batch_size=50)

    order = StoreOrder.objects.get(pk=created["id"])
    assert order.status == StoreOrderStatus.CANCELLED
    assert order.cancellation_reason == "delivery_date_expired"


@pytest.mark.django_db
def test_guest_can_cancel_while_waiting_for_goods(store_product, payment_settings):
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    client = APIClient()
    cancelled = client.post(
        f"/api/v1/public/store-orders/{created['id']}/cancel",
        {"access_token": created["guest_access_token"], "idempotency_key": str(uuid.uuid4())},
        format="json",
    )
    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["data"]["status"] == "cancelled"


@pytest.mark.django_db
def test_admin_can_mark_approved_order_delivered(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    approve_key = str(uuid.uuid4())
    approve_body = {"idempotency_key": approve_key, "allocations": suggestion}
    approved = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        approve_body,
        format="json",
    )
    assert_admin_action_receipt(approved, created["id"], "approve", approve_key)
    payment = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/record-payment",
        {"idempotency_key": str(uuid.uuid4()), "note": "Bank received"},
        format="json",
    )
    assert payment.status_code == 200, payment.json()
    key = str(uuid.uuid4())
    delivered = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/deliver",
        {"idempotency_key": key},
        format="json",
    )
    replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/deliver",
        {"idempotency_key": key},
        format="json",
    )
    for response in (delivered, replay):
        assert_admin_action_receipt(response, created["id"], "deliver", key)
    assert delivered.json()["data"]["status"] == "delivered"
    later_approval_replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        approve_body,
        format="json",
    )
    assert_admin_action_receipt(later_approval_replay, created["id"], "approve", approve_key)
    assert later_approval_replay.json()["data"]["status"] == "delivered"
    assert StoreStockAllocation.objects.filter(line__order_id=created["id"]).count() == 1
    assert StoreStockMovement.objects.filter(line__order_id=created["id"], kind="sale").count() == 1


@pytest.mark.django_db
def test_expired_order_cannot_be_approved_and_releases_reservation(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    line = StoreOrder.objects.get(pk=created["id"]).lines.get()
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)

    response = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {
            "idempotency_key": str(uuid.uuid4()),
            "allocations": [{"line_id": str(line.pk), "batch_id": str(batch.pk), "quantity": 2}],
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "store_order_expired"
    assert "meta" not in response.json()
    order = StoreOrder.objects.get(pk=created["id"])
    assert order.status == StoreOrderStatus.CANCELLED
    assert not line.stock_reservations.filter(released_at__isnull=True).exists()


@pytest.mark.django_db
def test_historical_cancelled_order_manual_restock_is_idempotent(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    legacy_checkout_snapshot(created)
    APIClient().post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    approved = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    ).json()["data"]
    historical_cancelled_snapshot(created["id"], active_user)
    assert (
        APIClient()
        .get("/api/v1/public/store-products?locale=vi")
        .json()["data"][0]["available_quantity"]
        == 8
    )
    key = str(uuid.uuid4())
    payload = {
        "idempotency_key": key,
        "allocations": [{"allocation_id": approved["allocations"][0]["id"], "quantity": 1}],
    }
    first = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/restock", payload, format="json"
    )
    replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/restock", payload, format="json"
    )
    for response in (first, replay):
        assert_admin_action_receipt(response, created["id"], "restock", key)
    assert StoreStockMovement.objects.filter(kind="restock").count() == 1
    second_key = str(uuid.uuid4())
    second = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/restock",
        {**payload, "idempotency_key": second_key},
        format="json",
    )
    assert_admin_action_receipt(second, created["id"], "restock", second_key)
    later_replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/restock", payload, format="json"
    )
    assert_admin_action_receipt(later_replay, created["id"], "restock", key)
    assert later_replay.json()["data"]["allocations"][0]["restocked_quantity"] == 2
    assert StoreStockMovement.objects.filter(kind="restock").count() == 2
    assert (
        APIClient()
        .get("/api/v1/public/store-products?locale=vi")
        .json()["data"][0]["available_quantity"]
        == 10
    )


@pytest.mark.django_db
def test_existing_membership_usage_cannot_spend_store_reserved_quota(
    zero_shipping_fee, active_user, access_token, store_product
):
    membership = attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )
    assert created.status_code == 201

    with pytest.raises(InsufficientQuota):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": store_product.csa_quota_group_id, "units": 1}],
        )


@pytest.mark.django_db
def test_cycle_expiry_preserves_held_quota_then_cancel_expires_it(
    zero_shipping_fee, active_user, access_token, store_product
):
    membership = attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    ).json()["data"]
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    quota.cycle.start_date = timezone.localdate() - timedelta(days=1)
    quota.cycle.end_date = timezone.localdate()
    quota.cycle.save(update_fields=["start_date", "end_date"])

    ensure_due_cycles(membership, as_of=timezone.localdate())
    quota.refresh_from_db()
    assert quota.remaining_units == 4
    assert quota.expired_units == 0

    cancelled = client.post(
        f"/api/v1/admin/store-orders/{created['id']}/cancel",
        {
            "idempotency_key": str(uuid.uuid4()),
            "reason": "Kỳ quota đã hết",
            "reconciliation_note": "Không cấp bù",
        },
        format="json",
    )
    assert cancelled.status_code == 403
    # Buyer cannot admin-cancel; expiry release is exercised directly by the worker.
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())
    call_command("expire_store_orders")
    quota.refresh_from_db()
    assert quota.remaining_units == 0
    assert quota.expired_units == 4


@pytest.mark.django_db
def test_multiple_lines_share_quota_in_checkout_position_order(
    zero_shipping_fee, active_user, access_token, store_product, payment_settings
):
    attach_active_membership(active_user, store_product)
    second = Product.objects.create(
        category=store_product.category,
        csa_quota_group=store_product.csa_quota_group,
        sku="CUCUMBER",
        slug="cucumber",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=45_000,
        csa_quota_cost=2,
        primary_image=store_product.primary_image,
        status="active",
        is_published=True,
    )
    ProductTranslation.objects.bulk_create(
        [
            ProductTranslation(product=second, locale="vi", name="Dưa leo"),
            ProductTranslation(product=second, locale="en", name="Cucumber"),
        ]
    )
    farm = Farm.objects.create(
        name_vi="Nông trại B",
        name_en="Farm B",
        location_vi="Huế",
        location_en="Hue",
    )
    offer = FarmProduct.objects.create(farm=farm, product=second)
    FarmProductStockBatch.objects.create(
        farm_product=offer,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=3),
        quantity_units=10,
    )
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                },
                {
                    "product_id": str(second.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 0,
                },
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 90_000,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    assert [
        (line["product_id"], line["quota_quantity"], line["cash_quantity"])
        for line in response.json()["data"]["lines"]
    ] == [(str(store_product.pk), 2, 0), (str(second.pk), 0, 2)]


@pytest.mark.django_db
def test_legacy_paid_order_expired_by_approve_requires_refund_and_refund_replays(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(created)
    guest.post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    assert (
        admin.post(
            f"/api/v1/admin/store-orders/{created['id']}/record-payment",
            {"idempotency_key": str(uuid.uuid4()), "note": "Bank checked"},
            format="json",
        ).status_code
        == 200
    )
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())

    expired = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    )

    assert expired.status_code == 409
    order = StoreOrder.objects.get(pk=created["id"])
    assert order.status == StoreOrderStatus.CANCELLED
    assert order.payment_status == StorePaymentStatus.REFUND_REQUIRED
    refund_key = str(uuid.uuid4())
    payload = {
        "idempotency_key": refund_key,
        "amount_vnd": order.total_vnd,
        "note": "Expired after payment",
    }
    first = admin.post(f"/api/v1/admin/store-orders/{created['id']}/refund", payload, format="json")
    replay = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/refund", payload, format="json"
    )
    assert first.status_code == replay.status_code == 200
    assert StoreOrder.objects.get(pk=created["id"]).payment_status == StorePaymentStatus.REFUNDED


@pytest.mark.django_db
@pytest.mark.parametrize("initial_status", ["payment_pending", "pending_review"])
def test_legacy_expired_reservation_is_not_effective_before_worker(
    initial_status, store_product, payment_settings
):
    client = APIClient()
    created = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(created)
    if initial_status == "pending_review":
        client.post(
            f"/api/v1/public/store-orders/{created['id']}/report-transfer",
            {"access_token": created["guest_access_token"]},
            format="json",
        )
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())

    catalog = client.get("/api/v1/public/store-products?locale=vi")
    replacement = client.post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, quantity=10, expected_available=10),
        format="json",
    )

    assert catalog.json()["data"][0]["available_quantity"] == 10
    assert replacement.status_code == 201, replacement.json()
    call_command("expire_store_orders")
    call_command("expire_store_orders")
    original = StoreOrder.objects.get(pk=created["id"])
    assert original.status == StoreOrderStatus.CANCELLED
    assert original.lines.get().stock_reservations.get().release_reason == "expired"


@pytest.mark.django_db
def test_expired_quota_reservation_does_not_block_usage_before_worker(
    zero_shipping_fee, active_user, access_token, store_product
):
    membership = attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    ).json()["data"]
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())

    usage, was_created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": store_product.csa_quota_group_id, "units": 4}],
    )

    assert was_created
    assert usage.lines.get().units == 4
    call_command("expire_store_orders")
    reservation = StoreQuotaReservation.objects.get(line__order_id=created["id"])
    assert reservation.released_at is not None


@pytest.mark.django_db
@pytest.mark.parametrize("inactive_level", ["direct", "parent", "grandparent"])
def test_checkout_rejects_product_with_inactive_category_ancestor(
    inactive_level, store_product, payment_settings
):
    direct = store_product.category
    parent = Category.objects.create(slug="produce", is_active=True)
    grandparent = Category.objects.create(slug="food", is_active=True)
    direct.parent = parent
    direct.save(update_fields=["parent"])
    parent.parent = grandparent
    parent.save(update_fields=["parent"])
    {"direct": direct, "parent": parent, "grandparent": grandparent}[
        inactive_level
    ].__class__.objects.filter(
        pk={"direct": direct, "parent": parent, "grandparent": grandparent}[inactive_level].pk
    ).update(is_active=False)

    response = APIClient().post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "store_product_changed"
    assert not StoreOrder.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["profile", "email", ""])
@pytest.mark.parametrize("operation", ["checkout", "detail", "report", "cancel"])
def test_public_store_user_branch_requires_profile_and_email_scopes(
    scope, operation, active_user, access_token, store_product, payment_settings
):
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    StoreOrder.objects.filter(pk=created["id"]).update(buyer_user=active_user)
    address = ShippingAddress.objects.create(
        user=active_user,
        recipient_name=active_user.name,
        phone_number="+84901234567",
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    access_token.scope = scope
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    if operation == "checkout":
        payload = guest_payload(store_product, expected_available=8)
        payload.pop("guest")
        payload["shipping_address_updated_at"] = address.updated_at.isoformat()
        response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    elif operation == "detail":
        response = client.get(f"/api/v1/public/store-orders/{created['id']}")
    elif operation == "report":
        response = client.post(
            f"/api/v1/public/store-orders/{created['id']}/report-transfer", {}, format="json"
        )
    else:
        response = client.post(
            f"/api/v1/public/store-orders/{created['id']}/cancel",
            {"idempotency_key": str(uuid.uuid4())},
            format="json",
        )
    assert response.status_code == 403


@pytest.mark.django_db
def test_public_store_guest_access_does_not_accept_wrong_token(store_product, payment_settings):
    created = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    client = APIClient()
    client.credentials(HTTP_X_STORE_ACCESS_TOKEN="wrong")
    wrong = client.get(f"/api/v1/public/store-orders/{created['id']}")
    client.credentials(HTTP_X_STORE_ACCESS_TOKEN=created["guest_access_token"])
    correct = client.get(f"/api/v1/public/store-orders/{created['id']}")
    assert wrong.status_code == 404
    assert correct.status_code == 200


@pytest.mark.django_db
def test_duplicate_approve_allocation_is_rejected_without_partial_writes(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ][0]
    item = {**suggestion, "quantity": 1}

    response = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": [item, item]},
        format="json",
    )

    assert response.status_code == 400
    assert StoreStockAllocation.objects.count() == 0
    assert StoreStockMovement.objects.count() == 0
    assert StoreOrder.objects.get(pk=created["id"]).status == StoreOrderStatus.PENDING_REVIEW


@pytest.mark.django_db
def test_historical_cancelled_order_duplicate_restock_is_rejected_without_partial_writes(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(created)
    guest.post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    approved = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    ).json()["data"]
    historical_cancelled_snapshot(created["id"], active_user)
    item = {"allocation_id": approved["allocations"][0]["id"], "quantity": 1}

    response = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/restock",
        {"idempotency_key": str(uuid.uuid4()), "allocations": [item, item]},
        format="json",
    )

    assert response.status_code == 400
    assert StoreStockMovement.objects.filter(kind="restock").count() == 0


@pytest.mark.django_db
def test_admin_order_detail_exposes_all_source_candidates_and_restock_balance(
    active_user, access_token, store_product, payment_settings
):
    second_farm = Farm.objects.create(
        name_vi="Nông trại B", name_en="Farm B", location_vi="Huế", location_en="Hue"
    )
    second_offer = FarmProduct.objects.create(farm=second_farm, product=store_product)
    second_batch = FarmProductStockBatch.objects.create(
        farm_product=second_offer,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=5),
        quantity_units=4,
    )
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    created = (
        APIClient()
        .post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, expected_available=10),
            format="json",
        )
        .json()["data"]
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    detail = admin.get(f"/api/v1/admin/store-orders/{created['id']}")

    assert detail.status_code == 200
    inventory = detail.json()["data"]["source_inventory"]
    assert inventory["refresh_required_before_approve"] is True
    assert inventory["product_reservations"] == [
        {
            "product_id": str(store_product.pk),
            "reserved_quantity": 2,
            "this_order_quantity": 2,
            "other_orders_quantity": 0,
        }
    ]
    candidates = inventory["candidates"]
    assert {item["batch_id"] for item in candidates} == {
        str(
            FarmProductStockBatch.objects.get(
                farm_product__product=store_product, quantity_units=10
            ).pk
        ),
        str(second_batch.pk),
    }
    assert {item["farm_name"] for item in candidates} == {"Nông trại A", "Nông trại B"}
    assert {item["available_quantity"] for item in candidates} == {10, 4}


@pytest.mark.django_db
@pytest.mark.parametrize("expiry_trigger", ["read", "worker"])
def test_legacy_paid_order_expired_by_read_or_worker_requires_refund(
    expiry_trigger, active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(created)
    guest.post(
        f"/api/v1/public/store-orders/{created['id']}/report-transfer",
        {"access_token": created["guest_access_token"]},
        format="json",
    )
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/record-payment",
        {"idempotency_key": str(uuid.uuid4()), "note": "Bank checked"},
        format="json",
    )
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())

    if expiry_trigger == "read":
        response = admin.get(f"/api/v1/admin/store-orders/{created['id']}")
        assert response.status_code == 200
    else:
        call_command("expire_store_orders")

    order = StoreOrder.objects.get(pk=created["id"])
    assert order.status == StoreOrderStatus.CANCELLED
    assert order.payment_status == StorePaymentStatus.REFUND_REQUIRED


@pytest.mark.django_db
def test_expired_reservation_does_not_block_batch_cancellation(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    batch.quantity_units = 2
    batch.save(update_fields=["quantity_units", "updated_at"])
    created = (
        APIClient()
        .post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, quantity=2, expected_available=2),
            format="json",
        )
        .json()["data"]
    )
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = admin.post(
        f"/api/v1/admin/farms/{batch.farm_product.farm_id}/products/{store_product.pk}/batches/{batch.pk}/cancel",
        {"version": batch.version},
        format="json",
    )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["available_quantity"] == 0


@pytest.mark.django_db
def test_farm_inventory_tracks_sale_cancel_and_restock(
    active_user, access_token, store_product, payment_settings
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    suggestion = admin.get(f"/api/v1/admin/store-orders/{created['id']}").json()["data"][
        "allocation_suggestion"
    ]
    approved = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    ).json()["data"]
    farm_id = store_product.farm_offers.get().farm_id

    after_sale = admin.get(f"/api/v1/admin/farms/{farm_id}/products").json()["data"][0]
    assert after_sale["initial_quantity"] == 10
    assert after_sale["net_sold_quantity"] == 2
    assert after_sale["available_quantity"] == 8
    admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/cancel",
        {"idempotency_key": str(uuid.uuid4()), "reason": "Operational cancellation"},
        format="json",
    )
    after_cancel = admin.get(f"/api/v1/admin/farms/{farm_id}/products").json()["data"][0]
    assert after_cancel["available_quantity"] == 10
    allocation = approved["allocations"][0]
    repeated_restock = admin.post(
        f"/api/v1/admin/store-orders/{created['id']}/restock",
        {
            "idempotency_key": str(uuid.uuid4()),
            "allocations": [{"allocation_id": allocation["id"], "quantity": 2}],
        },
        format="json",
    )
    assert repeated_restock.status_code == 409, repeated_restock.json()
    assert StoreStockMovement.objects.filter(kind="restock").count() == 1
    after_restock = admin.get(f"/api/v1/admin/farms/{farm_id}/products").json()["data"][0]
    assert after_restock["net_sold_quantity"] == 0
    assert after_restock["available_quantity"] == 10


@pytest.mark.django_db
@pytest.mark.parametrize("invalid_source", ["farm", "offer", "batch"])
def test_farm_inventory_excludes_invalid_source(
    invalid_source, active_user, access_token, store_product
):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    offer = store_product.farm_offers.get()
    batch = offer.stock_batches.get()
    if invalid_source == "farm":
        Farm.objects.filter(pk=offer.farm_id).update(is_active=False)
    elif invalid_source == "offer":
        FarmProduct.objects.filter(pk=offer.pk).update(is_active=False, removed_at=timezone.now())
    else:
        FarmProductStockBatch.objects.filter(pk=batch.pk).update(cancelled_at=timezone.now())
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    product = admin.get(f"/api/v1/admin/farms/{offer.farm_id}/products").json()["data"][0]
    batch_data = admin.get(
        f"/api/v1/admin/farms/{offer.farm_id}/products/{store_product.pk}/batches"
    ).json()["data"][0]

    assert product["initial_quantity"] == 0
    assert product["available_quantity"] == 0
    assert batch_data["quantity_units"] == 10
    assert batch_data["remaining_quantity"] == 10
    assert batch_data["available_quantity"] == 0


@pytest.mark.django_db
@pytest.mark.parametrize("operation", ["checkout", "detail", "report", "cancel"])
def test_public_store_user_branch_accepts_complete_self_scopes(
    operation, active_user, access_token, store_product, payment_settings
):
    active_user.login_phone_number = "0901234567"
    active_user.save(update_fields=["login_phone_number"])
    address = ShippingAddress.objects.create(
        user=active_user,
        recipient_name=active_user.name,
        phone_number="+84901234567",
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    payload = guest_payload(store_product)
    payload.pop("guest")
    payload["shipping_address_updated_at"] = address.updated_at.isoformat()
    created = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert created.status_code == 201, created.json()
    order_id = created.json()["data"]["id"]
    if operation == "checkout":
        return
    if operation == "detail":
        response = client.get(f"/api/v1/public/store-orders/{order_id}")
    elif operation == "report":
        approve_review_order(created.json()["data"], active_user)
        response = client.post(
            f"/api/v1/public/store-orders/{order_id}/report-transfer", {}, format="json"
        )
    else:
        response = client.post(
            f"/api/v1/public/store-orders/{order_id}/cancel",
            {"idempotency_key": str(uuid.uuid4())},
            format="json",
        )
    assert response.status_code == 200, response.json()


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_store_checkouts_cannot_overreserve_stock(store_product, payment_settings):
    barrier = Barrier(2)

    def checkout():
        close_old_connections()
        barrier.wait()
        response = APIClient().post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, quantity=6),
            format="json",
        )
        close_old_connections()
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _index: checkout(), range(2)))

    assert statuses == [201, 409]
    assert (
        sum(
            StoreOrder.objects.filter(
                status__in=[StoreOrderStatus.PAYMENT_PENDING, StoreOrderStatus.PENDING_REVIEW]
            ).values_list("lines__stock_reservations__quantity", flat=True)
        )
        == 6
    )


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_store_checkouts_cannot_overreserve_quota(
    active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    barrier = Barrier(2)

    def checkout():
        close_old_connections()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
        barrier.wait()
        response = client.post(
            "/api/v1/public/store-checkouts",
            {
                "delivery_date": timezone.localdate(),
                "idempotency_key": str(uuid.uuid4()),
                "locale": "vi",
                "lines": [
                    {
                        "product_id": str(store_product.pk),
                        "quantity": 2,
                        "use_quota": True,
                        "expected_unit_price_vnd": 45_000,
                        "expected_available_quantity": 10,
                        "expected_quota_quantity": 2,
                    }
                ],
                "expected_shipping_fee_vnd": 0,
                "expected_total_vnd": 0,
                "shipping_address_updated_at": address.updated_at.isoformat(),
            },
            format="json",
        )
        close_old_connections()
        return response.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _index: checkout(), range(2)))

    assert statuses == [201, 409]
    assert StoreQuotaReservation.objects.filter(released_at__isnull=True).count() == 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_approve_and_expiry_have_one_cancelled_outcome(
    active_user, store_product, payment_settings
):
    from commerce.store_orders import StoreOrderError, approve_store_order, expire_order_if_needed

    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    order = StoreOrder.objects.get(pk=created["id"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    allocations = [{"line_id": order.lines.get().pk, "batch_id": batch.pk, "quantity": 2}]
    StoreOrder.objects.filter(pk=order.pk).update(expires_at=timezone.now())
    barrier = Barrier(2)

    def approve():
        close_old_connections()
        barrier.wait()
        try:
            approve_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                allocations=allocations,
            )
        except StoreOrderError:
            pass
        finally:
            close_old_connections()

    def expire():
        close_old_connections()
        barrier.wait()
        expire_order_if_needed(order.pk)
        close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda function: function(), [approve, expire]))

    order.refresh_from_db()
    assert order.status == StoreOrderStatus.CANCELLED
    assert not StoreStockAllocation.objects.exists()
    assert not StoreStockMovement.objects.exists()


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_checkout_and_batch_cancel_preserve_inventory(
    active_user, store_product, payment_settings
):
    from farms.services import FarmConflict, cancel_stock_batch

    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    batch.quantity_units = 2
    batch.save(update_fields=["quantity_units", "updated_at"])
    barrier = Barrier(2)

    def checkout():
        close_old_connections()
        barrier.wait()
        response = APIClient().post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, quantity=2, expected_available=2),
            format="json",
        )
        close_old_connections()
        return ("checkout", response.status_code)

    def cancel():
        close_old_connections()
        barrier.wait()
        try:
            cancel_stock_batch(
                farm_id=batch.farm_product.farm_id,
                product_id=store_product.pk,
                batch_id=batch.pk,
                actor=active_user,
                expected_version=batch.version,
            )
        except FarmConflict as error:
            result = ("cancel", error.code)
        else:
            result = ("cancel", "cancelled")
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = dict(pool.map(lambda function: function(), [checkout, cancel]))

    assert outcomes in [
        {"checkout": 201, "cancel": "stock_reserved"},
        {"checkout": 409, "cancel": "cancelled"},
    ]


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_approve_and_farm_deactivation_preserve_allocation(
    active_user, store_product, payment_settings
):
    from commerce.store_orders import approve_store_order
    from farms.services import FarmConflict, update_farm

    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    order = StoreOrder.objects.get(pk=created["id"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    farm = batch.farm_product.farm
    allocations = [{"line_id": order.lines.get().pk, "batch_id": batch.pk, "quantity": 2}]
    barrier = Barrier(2)

    def approve():
        close_old_connections()
        barrier.wait()
        approve_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            allocations=allocations,
        )
        close_old_connections()
        return ("approve", "approved")

    def deactivate():
        close_old_connections()
        barrier.wait()
        try:
            update_farm(
                farm_id=farm.pk,
                actor=active_user,
                expected_version=farm.version,
                data={"is_active": False},
            )
        except FarmConflict as error:
            result = ("deactivate", error.code)
        else:
            result = ("deactivate", "deactivated")
        close_old_connections()
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = dict(pool.map(lambda function: function(), [approve, deactivate]))

    order.refresh_from_db()
    assert outcomes["approve"] == "approved"
    assert outcomes["deactivate"] in {"stock_reserved", "deactivated"}
    assert order.status == StoreOrderStatus.PAYMENT_PENDING
    assert StoreStockAllocation.objects.filter(line__order=order).count() == 1
    assert StoreStockMovement.objects.filter(line__order=order, kind="sale").count() == 1


@pytest.mark.django_db
def test_due_scheduled_membership_is_resolved_before_store_checkout(
    zero_shipping_fee, active_user, access_token, store_product
):
    membership = attach_active_membership(active_user, store_product)
    Membership.objects.filter(pk=membership.pk).update(status=MembershipStatus.SCHEDULED)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 2,
                }
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    assert response.json()["data"]["shipping_discount_reason"] is None
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE


@pytest.mark.django_db
@pytest.mark.parametrize("membership_state", ["future", "expired", "revoked"])
def test_ineligible_membership_does_not_receive_store_benefits(
    membership_state, active_user, access_token, store_product, payment_settings
):
    membership = attach_active_membership(active_user, store_product)
    today = timezone.localdate()
    if membership_state == "future":
        Membership.objects.filter(pk=membership.pk).update(
            status=MembershipStatus.SCHEDULED,
            start_date=today + timedelta(days=1),
            end_date=today + timedelta(days=31),
        )
    elif membership_state == "expired":
        Membership.objects.filter(pk=membership.pk).update(
            start_date=today - timedelta(days=1), end_date=today
        )
    else:
        Membership.objects.filter(pk=membership.pk).update(
            status=MembershipStatus.REVOKED, current_slot=None
        )
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 2,
                    "use_quota": False,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 0,
                }
            ],
            "expected_shipping_fee_vnd": 30_000,
            "expected_total_vnd": 120_000,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    assert response.json()["data"]["shipping_fee_vnd"] == "30000"
    assert response.json()["data"]["shipping_discount_reason"] is None


@pytest.mark.django_db
@pytest.mark.parametrize("future_first", [True, False])
@pytest.mark.parametrize("use_quota", [True, False])
def test_current_membership_is_not_hidden_by_future_membership(
    zero_shipping_fee,
    future_first,
    use_quota,
    active_user,
    access_token,
    store_product,
    payment_settings,
):
    current = attach_active_membership(active_user, store_product)
    future = Membership.objects.get(pk=current.pk)
    future.pk = uuid.UUID(int=0 if future_first else (1 << 128) - 1)
    future.status = MembershipStatus.SCHEDULED
    future.start_date = current.end_date
    future.end_date = current.end_date + timedelta(days=30)
    future.farmbrite_order_reference = f"FUTURE-{uuid.uuid4()}"
    future.farmbrite_order_reference_key = str(uuid.uuid4())
    future.save(force_insert=True)
    address = ShippingAddress.objects.get(user=active_user)
    payload = quota_checkout_payload(store_product, address, quantity=2)
    payload["lines"][0]["use_quota"] = use_quota
    payload["lines"][0]["expected_quota_quantity"] = 2 if use_quota else 0
    payload["expected_total_vnd"] = 0 if use_quota else 90_000
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = client.post("/api/v1/public/store-checkouts", payload, format="json")

    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["shipping_fee_vnd"] == "0"
    assert data["shipping_discount_reason"] is None
    assert data["lines"][0]["quota_quantity"] == (2 if use_quota else 0)
    future.refresh_from_db()
    assert future.status == MembershipStatus.SCHEDULED


def quota_checkout_payload(product, address, *, quantity, expected_available=10):
    return {
        "delivery_date": timezone.localdate(),
        "idempotency_key": str(uuid.uuid4()),
        "locale": "vi",
        "lines": [
            {
                "product_id": str(product.pk),
                "quantity": quantity,
                "use_quota": True,
                "expected_unit_price_vnd": 45_000,
                "expected_available_quantity": expected_available,
                "expected_quota_quantity": quantity,
            }
        ],
        "expected_shipping_fee_vnd": 0,
        "expected_total_vnd": 0,
        "shipping_address_updated_at": address.updated_at.isoformat(),
    }


@pytest.mark.django_db
def test_quota_api_reports_raw_reserved_and_available_units(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    initial = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]
    assert initial["remaining_units"] == 4
    assert initial["reserved_units"] == 0
    assert initial["used_units"] == 0
    assert initial["available_units"] == 4
    created = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=1),
        format="json",
    )
    assert created.status_code == 201, created.json()

    held = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]
    assert held["remaining_units"] == 4
    assert held["reserved_units"] == 2
    assert held["used_units"] == 0
    assert held["available_units"] == 2
    assert held["current_order_reserved_units"] == 0
    assert held["available_units_for_order"] == 2


@pytest.mark.django_db
def test_quota_api_excludes_expired_hold_before_worker(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=2),
        format="json",
    ).json()["data"]
    StoreOrder.objects.filter(pk=created["id"]).update(expires_at=timezone.now())

    quota = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]

    assert quota["remaining_units"] == 4
    assert quota["reserved_units"] == 0
    assert quota["available_units"] == 4


@pytest.mark.django_db
def test_quota_api_resume_includes_current_order_hold(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    created = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=2),
        format="json",
    ).json()["data"]

    quota = client.get(f"/api/v1/memberships/quota?store_order_id={created['id']}").json()["data"][
        "products"
    ][0]

    assert quota["remaining_units"] == 4
    assert quota["reserved_units"] == 4
    assert quota["available_units"] == 0
    assert quota["current_order_reserved_units"] == 4
    assert quota["available_units_for_order"] == 4


@pytest.mark.django_db
def test_quota_api_release_and_consume_do_not_double_subtract(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    cancelled_order = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=1),
        format="json",
    ).json()["data"]
    cancelled = client.post(
        f"/api/v1/admin/store-orders/{cancelled_order['id']}/cancel",
        {"idempotency_key": str(uuid.uuid4()), "reason": "Release quota hold"},
        format="json",
    )
    assert cancelled.status_code == 200, cancelled.json()
    after_cancel = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]
    assert (
        after_cancel["remaining_units"],
        after_cancel["reserved_units"],
        after_cancel["available_units"],
    ) == (4, 0, 4)

    approved_order = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=2),
        format="json",
    ).json()["data"]
    suggestion = client.get(f"/api/v1/admin/store-orders/{approved_order['id']}").json()["data"][
        "allocation_suggestion"
    ]
    approved = client.post(
        f"/api/v1/admin/store-orders/{approved_order['id']}/approve",
        {"idempotency_key": str(uuid.uuid4()), "allocations": suggestion},
        format="json",
    )
    assert approved.status_code == 200, approved.json()
    after_approve = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]
    assert after_approve["used_units"] == 4
    history = client.get("/api/v1/memberships/usage").json()["data"]
    assert len(history) == 1
    assert history[0]["order_id"] == approved_order["id"]
    assert (
        after_approve["remaining_units"],
        after_approve["reserved_units"],
        after_approve["available_units"],
    ) == (0, 0, 0)
    reversed_order = client.post(
        f"/api/v1/admin/store-orders/{approved_order['id']}/cancel",
        {"idempotency_key": str(uuid.uuid4()), "reason": "Cancel approved order"},
        format="json",
    )
    assert reversed_order.status_code == 200, reversed_order.json()
    history = client.get("/api/v1/memberships/usage").json()["data"]
    assert history[0]["order_id"] == approved_order["id"]
    assert history[0]["status"] == "reversed"
    restored = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]
    assert (restored["available_units"], restored["reserved_units"], restored["used_units"]) == (
        4,
        0,
        0,
    )


@pytest.mark.django_db
def test_quota_api_aggregates_multiple_effective_store_holds(
    zero_shipping_fee, active_user, access_token, store_product
):
    attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    first = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=1),
        format="json",
    )
    second = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(
            store_product,
            address,
            quantity=1,
            expected_available=9,
        ),
        format="json",
    )
    assert first.status_code == second.status_code == 201

    quota = client.get("/api/v1/memberships/quota").json()["data"]["products"][0]

    assert quota["remaining_units"] == 4
    assert quota["reserved_units"] == 4
    assert quota["available_units"] == 0


@pytest.mark.django_db
def test_quota_resume_order_is_owner_scoped(active_user, access_token, store_product):
    attach_active_membership(active_user, store_product)
    guest_order = StoreOrder.objects.create(
        reference_code="SO261031OWNER",
        create_idempotency_key=uuid.uuid4(),
        request_fingerprint="0" * 64,
        locale="vi",
        recipient_name_snapshot="Guest",
        phone_e164_snapshot="+84901234567",
        province_code_snapshot=PROVINCE,
        ward_code_snapshot=WARD,
        address_snapshot="Guest address",
        subtotal_vnd=0,
        shipping_fee_vnd=0,
        total_vnd=0,
        status=StoreOrderStatus.PENDING_REVIEW,
        payment_status=StorePaymentStatus.NOT_REQUIRED,
        expires_at=timezone.now() + timedelta(hours=1),
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = client.get(f"/api/v1/memberships/quota?store_order_id={guest_order.pk}")

    assert response.status_code == 404


def approve_review_order(data, actor):
    from commerce.store_orders import approve_store_order, source_inventory

    order = StoreOrder.objects.get(pk=data["id"])
    allocations = [
        {**item, "line_id": uuid.UUID(item["line_id"]), "batch_id": uuid.UUID(item["batch_id"])}
        for item in source_inventory(order)["suggestion"]
    ]
    return approve_store_order(
        order_id=order.pk, actor=actor, idempotency_key=uuid.uuid4(), allocations=allocations
    )


def create_review_quota_checkout(user, token, product, *, key=None):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    payload = quota_checkout_payload(product, ShippingAddress.objects.get(user=user), quantity=1)
    if key is not None:
        payload["idempotency_key"] = str(key)
    response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.json()
    return response.json()["data"]


@pytest.mark.django_db
def test_cash_store_cancel_never_reverses_unrelated_usage_key(
    active_user, store_product, payment_settings
):
    from commerce.store_orders import cancel_store_order
    from memberships.models import UsageOrderStatus

    membership = attach_active_membership(active_user, store_product)
    key = uuid.uuid4()
    usage, _ = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=key,
        lines=[{"product_id": store_product.csa_quota_group_id, "units": 1}],
    )
    client = APIClient()
    data = client.post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, idempotency_key=key),
        format="json",
    ).json()["data"]
    approve_review_order(data, active_user)
    cancel_store_order(
        order_id=data["id"], actor=active_user, idempotency_key=uuid.uuid4(), reason="cancel"
    )
    usage.refresh_from_db()
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert (usage.status, quota.remaining_units) == (UsageOrderStatus.APPLIED, 3)


@pytest.mark.django_db
def test_store_quota_approval_is_independent_of_manual_usage_key(
    zero_shipping_fee, active_user, access_token, store_product
):
    from memberships.models import MembershipUsageOrder

    membership = attach_active_membership(active_user, store_product)
    key = uuid.uuid4()
    manual, _ = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=key,
        lines=[{"product_id": store_product.csa_quota_group_id, "units": 1}],
    )
    data = create_review_quota_checkout(active_user, access_token, store_product, key=key)
    order = approve_review_order(data, active_user)
    usage = MembershipUsageOrder.objects.exclude(pk=manual.pk).get()
    assert getattr(usage, "store_order_id", None) == order.pk
    assert usage.idempotency_key != key
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == 1


@pytest.mark.django_db
def test_store_usage_cannot_be_undone_through_csa_api(
    zero_shipping_fee, active_user, access_token, store_product
):
    from memberships.models import MembershipUsageOrder

    membership = attach_active_membership(active_user, store_product)
    data = create_review_quota_checkout(active_user, access_token, store_product)
    approve_review_order(data, active_user)
    usage = MembershipUsageOrder.objects.get(membership=membership)
    admin_group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(admin_group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.post(
        f"/api/v1/admin/membership-usage/{usage.pk}/undo", {"reason": "undo"}, format="json"
    )
    assert response.status_code == 409, response.json()
    usage.refresh_from_db()
    order = StoreOrder.objects.get(pk=data["id"])
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert usage.status == "applied"
    assert order.status == "pending_delivery"
    assert quota.remaining_units == 2


@pytest.mark.django_db
def test_checkout_rechecks_key_after_waiting_for_inventory_lock(
    store_product, payment_settings, monkeypatch
):
    from unittest.mock import Mock

    client = APIClient()
    payload = guest_payload(store_product)
    first = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert first.status_code == 201
    real_filter = StoreOrder.objects.filter
    missed = False

    def miss_initial_key_read(*args, **kwargs):
        nonlocal missed
        if "create_idempotency_key" in kwargs and not missed:
            missed = True
            return Mock(first=Mock(return_value=None))
        return real_filter(*args, **kwargs)

    monkeypatch.setattr(StoreOrder.objects, "filter", miss_initial_key_read)
    retry = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert retry.status_code == 200, retry.json()
    assert retry.json()["data"]["id"] == first.json()["data"]["id"]
    assert retry.json()["data"]["expires_at"] == first.json()["data"]["expires_at"]
    assert StoreOrder.objects.count() == 1
    missed = False
    payload["guest"]["address"] = "Different delivery address"
    conflict = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert conflict.status_code == 409
    assert conflict.json()["errors"][0]["code"] == "store_idempotency_conflict"
    assert StoreOrder.objects.count() == 1


@pytest.mark.django_db
def test_legacy_buyer_cancel_received_payment_can_be_refunded(
    active_user, store_product, payment_settings
):
    from commerce.store_orders import record_store_payment, refund_store_order

    client = APIClient()
    data = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(data)
    record_store_payment(
        order_id=data["id"], actor=active_user, idempotency_key=uuid.uuid4(), note="bank checked"
    )
    response = client.post(
        f"/api/v1/public/store-orders/{data['id']}/cancel",
        {"access_token": data["guest_access_token"], "idempotency_key": str(uuid.uuid4())},
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["payment_status"] == "refund_required"
    refunded = refund_store_order(
        order_id=data["id"],
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        amount_vnd=120000,
        note="refunded",
    )
    assert refunded.payment_status == "refunded"


@pytest.mark.django_db
@pytest.mark.parametrize("order_count", [1, 3])
def test_order_list_queries_do_not_grow_per_order(
    active_user,
    access_token,
    store_product,
    payment_settings,
    django_assert_num_queries,
    order_count,
):
    # A list needs count + orders + joined lines, regardless of the number of orders.
    from rest_framework.test import APIRequestFactory, force_authenticate

    from commerce.store_views import SelfStoreOrderListView

    client = APIClient()
    for index in range(order_count):
        response = client.post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, expected_available=10 - 2 * index),
            format="json",
        )
        assert response.status_code == 201
    StoreOrder.objects.update(buyer_user=active_user)
    request = APIRequestFactory().get("/api/v1/store-orders")
    force_authenticate(request, user=active_user, token=access_token)
    with django_assert_num_queries(3):
        response = SelfStoreOrderListView.as_view()(request)
    assert response.status_code == 200
    assert len(response.data["data"]) == order_count
    assert all(order["lines"][0]["image_url"] for order in response.data["data"])


@pytest.mark.django_db
def test_farm_deactivation_uses_store_inventory_lock_order(active_user, store_product, monkeypatch):
    from django.db.models import QuerySet

    from farms.services import update_farm

    farm = Farm.objects.get(products__product=store_product)
    locked_models = []
    real_fetch = QuerySet._fetch_all

    def trace_lock_requests(queryset):
        if queryset._result_cache is None and queryset.query.select_for_update:
            locked_models.append(queryset.model._meta.label_lower)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", trace_lock_requests)
    updated = update_farm(
        farm_id=farm.pk,
        actor=active_user,
        expected_version=farm.version,
        data={"is_active": False},
    )
    assert updated.is_active is False
    # SQLite cannot take row locks, but this verifies the requested lock order;
    # the threaded row-lock database test above exercises the same operations.
    assert locked_models[:3] == ["commerce.product", "farms.farmproduct", "farms.farm"]


@pytest.mark.django_db
@pytest.mark.parametrize("cancelled", [False, True])
def test_store_usage_migration_links_legacy_allocations_and_preserves_reversal(
    zero_shipping_fee, active_user, access_token, store_product, cancelled
):
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps

    from commerce.store_orders import cancel_store_order
    from memberships.models import MembershipUsageOrder

    membership = attach_active_membership(active_user, store_product)
    data = create_review_quota_checkout(active_user, access_token, store_product)
    order = approve_review_order(data, active_user)
    if cancelled:
        cancel_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="cancel"
        )
    usage = MembershipUsageOrder.objects.get(store_order=order)
    # The pre-link version stored the checkout UUID in the usage ledger.
    MembershipUsageOrder.objects.filter(pk=usage.pk).update(
        store_order=None, idempotency_key=order.create_idempotency_key
    )
    migration = import_module("memberships.migrations.0031_store_usage_link")
    for _ in range(2):
        migration.link_existing_store_usage(apps, SimpleNamespace(connection=connection))
    usage.refresh_from_db()
    assert usage.store_order_id == order.pk
    assert usage.status == ("reversed" if cancelled else "applied")
    if not cancelled:
        cancel_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="cancel"
        )
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == 4


@pytest.mark.django_db
@pytest.mark.parametrize("mismatch", ["owner", "allocations", "note"])
def test_store_usage_migration_never_guesses_from_uuid_alone(
    zero_shipping_fee, active_user, access_token, store_product, mismatch
):
    from importlib import import_module
    from types import SimpleNamespace

    from django.apps import apps

    from memberships.models import MembershipUsageAllocation, MembershipUsageOrder

    attach_active_membership(active_user, store_product)
    data = create_review_quota_checkout(active_user, access_token, store_product)
    order = approve_review_order(data, active_user)
    usage = MembershipUsageOrder.objects.get(store_order=order)
    MembershipUsageOrder.objects.filter(pk=usage.pk).update(
        store_order=None, idempotency_key=order.create_idempotency_key
    )
    if mismatch == "owner":
        StoreOrder.objects.filter(pk=order.pk).update(buyer_user=None)
    elif mismatch == "allocations":
        MembershipUsageAllocation.objects.filter(line__order=usage).update(units=1)
    else:
        MembershipUsageOrder.objects.filter(pk=usage.pk).update(note="Manual CSA usage")
    migration = import_module("memberships.migrations.0031_store_usage_link")
    migration.link_existing_store_usage(apps, SimpleNamespace(connection=connection))
    usage.refresh_from_db()
    assert usage.store_order_id is None


@pytest.mark.django_db
def test_manual_usage_cannot_replay_store_usage_idempotency_key(
    zero_shipping_fee, active_user, access_token, store_product
):
    from memberships.models import MembershipUsageOrder
    from memberships.services import UsageIdempotencyConflict

    membership = attach_active_membership(active_user, store_product)
    data = create_review_quota_checkout(active_user, access_token, store_product)
    order = approve_review_order(data, active_user)
    usage = MembershipUsageOrder.objects.get(store_order=order)
    with pytest.raises(UsageIdempotencyConflict):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=usage.idempotency_key,
            lines=[{"product_id": store_product.csa_quota_group_id, "units": 2}],
            note=usage.note,
        )
    assert MembershipUsageOrder.objects.filter(membership=membership).count() == 1


@pytest.mark.django_db
def test_store_approval_locks_membership_before_inventory_sources(
    zero_shipping_fee, active_user, access_token, store_product, monkeypatch
):
    from django.db.models import QuerySet

    attach_active_membership(active_user, store_product)
    data = create_review_quota_checkout(active_user, access_token, store_product)
    locked_models = []
    real_fetch = QuerySet._fetch_all

    def trace_lock_requests(queryset):
        if queryset._result_cache is None and queryset.query.select_for_update:
            locked_models.append(queryset.model._meta.label_lower)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", trace_lock_requests)
    order = approve_review_order(data, active_user)
    assert order.status == "pending_delivery"
    assert "memberships.membership" in locked_models[: locked_models.index("farms.farmproduct")]


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_same_key_checkout_returns_one_order(store_product, payment_settings):
    key = uuid.uuid4()
    barrier = Barrier(2)

    def checkout():
        close_old_connections()
        try:
            barrier.wait()
            response = APIClient().post(
                "/api/v1/public/store-checkouts",
                guest_payload(store_product, idempotency_key=key),
                format="json",
            )
            return response.status_code, response.json()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _index: checkout(), range(2)))
    assert sorted(status for status, _body in responses) == [200, 201]
    assert len({body["data"]["id"] for _status, body in responses}) == 1
    assert len({body["data"]["expires_at"] for _status, body in responses}) == 1
    assert StoreOrder.objects.count() == 1
    assert StoreOrder.objects.get().lines.get().stock_reservations.get().quantity == 2


@pytest.mark.django_db
def test_farm_deactivation_rejects_sources_added_while_acquiring_locks(
    active_user, store_product, monkeypatch
):
    from django.db.models import QuerySet

    from farms.services import FarmConflict, update_farm

    farm = Farm.objects.get(products__product=store_product)
    extra = Product.objects.create(
        category=store_product.category,
        sku="NEW-SOURCE",
        slug="new-source",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=45000,
    )
    real_fetch = QuerySet._fetch_all
    added = False

    def add_source_before_farm_lock(queryset):
        nonlocal added
        if queryset.query.select_for_update and queryset.model is Farm and not added:
            added = True
            FarmProduct.objects.create(farm=farm, product=extra)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", add_source_before_farm_lock)
    with pytest.raises(FarmConflict) as error:
        update_farm(
            farm_id=farm.pk,
            actor=active_user,
            expected_version=farm.version,
            data={"is_active": False},
        )
    assert error.value.code == "farm_product_changed"
    farm.refresh_from_db()
    assert farm.is_active is True


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payment_mode", "blocked_resource", "seconds_late"),
    [
        ("cash", "stock", 30),
        ("received", "stock", 30),
        ("quota", "stock", 30),
        ("quota", "quota", 30),
        ("quota", "quota", 0),
    ],
)
def test_legacy_store_approval_expires_after_waiting_for_resource_locks(
    active_user,
    access_token,
    store_product,
    payment_settings,
    monkeypatch,
    payment_mode,
    blocked_resource,
    seconds_late,
):
    from django.db.models import QuerySet

    from commerce.store_orders import StoreOrderError, approve_store_order, record_store_payment
    from memberships.models import MembershipUsageOrder

    if payment_mode == "quota":
        ShippingArea.objects.update(shipping_fee_vnd=0)
        membership = attach_active_membership(active_user, store_product)
        data = create_review_quota_checkout(active_user, access_token, store_product)
        legacy_checkout_snapshot(data)
    else:
        client = APIClient()
        data = client.post(
            "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
        ).json()["data"]
        legacy_checkout_snapshot(data)
        response = client.post(
            f"/api/v1/public/store-orders/{data['id']}/report-transfer",
            {"access_token": data["guest_access_token"]},
            format="json",
        )
        assert response.status_code == 200
        if payment_mode == "received":
            record_store_payment(
                order_id=data["id"], actor=active_user, idempotency_key=uuid.uuid4()
            )
    order = StoreOrder.objects.get(pk=data["id"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    allocations = [
        {
            "line_id": order.lines.get().pk,
            "batch_id": batch.pk,
            "quantity": 1 if payment_mode == "quota" else 2,
        }
    ]
    started = timezone.now()
    deadline = started + timedelta(seconds=1)
    StoreOrder.objects.filter(pk=order.pk).update(expires_at=deadline)
    clock = [started]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    real_fetch = QuerySet._fetch_all

    blocked_model = FarmProductStockBatch if blocked_resource == "stock" else MembershipCycleQuota

    def wait_for_source(queryset):
        if queryset.model is blocked_model and queryset.query.select_for_update:
            clock[0] = deadline + timedelta(seconds=seconds_late)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", wait_for_source)
    with pytest.raises(StoreOrderError) as error:
        approve_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            allocations=allocations,
        )
    assert error.value.code == "store_order_expired"
    order.refresh_from_db()
    assert order.status == "cancelled"
    assert order.cancellation_reason == "expired"
    assert order.cancelled_at == deadline + timedelta(seconds=seconds_late)
    assert order.approved_at is None
    assert not StoreStockMovement.objects.exists()
    assert not StoreStockAllocation.objects.exists()
    assert not MembershipUsageOrder.objects.exists()
    assert not order.lines.filter(stock_reservations__released_at__isnull=True).exists()
    expected_payment = {
        "cash": "unconfirmed",
        "received": "refund_required",
        "quota": "not_required",
    }
    assert order.payment_status == expected_payment[payment_mode]
    if payment_mode == "quota":
        ShippingArea.objects.update(shipping_fee_vnd=0)
        quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
        assert quota.remaining_units == 4
        assert not StoreQuotaReservation.objects.filter(released_at__isnull=True).exists()


@pytest.mark.django_db
def test_rejected_approval_preserves_cancelled_order_audit_fields(
    active_user, store_product, payment_settings, monkeypatch
):
    from commerce.store_orders import StoreOrderError, approve_store_order, expire_order_if_needed

    data = (
        APIClient()
        .post("/api/v1/public/store-checkouts", guest_payload(store_product), format="json")
        .json()["data"]
    )
    order = StoreOrder.objects.get(pk=data["id"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    StoreOrder.objects.filter(pk=order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    order, changed = expire_order_if_needed(order.pk)
    assert changed
    cancelled_at, updated_at = order.cancelled_at, order.updated_at
    monkeypatch.setattr(timezone, "now", lambda: cancelled_at + timedelta(hours=1))
    for _ in range(2):
        with pytest.raises(StoreOrderError):
            approve_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                allocations=[
                    {"line_id": order.lines.get().pk, "batch_id": batch.pk, "quantity": 2}
                ],
            )
        order.refresh_from_db()
        assert (order.cancelled_at, order.updated_at) == (cancelled_at, updated_at)
    assert not StoreStockMovement.objects.exists()


@pytest.mark.django_db
def test_admin_detail_allocation_query_count_is_constant(
    active_user, store_product, payment_settings
):
    from django.test.utils import CaptureQueriesContext
    from rest_framework.test import APIRequestFactory

    from commerce.store_views import order_payload

    original_batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    products = [store_product]
    for index in range(2):
        product = Product.objects.create(
            category=store_product.category,
            sku=f"QUERY-{index}",
            slug=f"query-{index}",
            unit_size=Decimal("1.000"),
            unit_label_vi="kg",
            unit_label_en="kg",
            retail_price_vnd=45000,
            status="active",
            is_published=True,
        )
        offer = FarmProduct.objects.create(farm=original_batch.farm_product.farm, product=product)
        FarmProductStockBatch.objects.create(
            farm_product=offer,
            start_date=original_batch.start_date,
            end_date=original_batch.end_date,
            quantity_units=10,
        )
        products.append(product)
    client = APIClient()
    counts = []
    for line_count in [1, 3]:
        payload = guest_payload(
            store_product, quantity=1, expected_available=10 if line_count == 1 else 9
        )
        payload["lines"] += [
            {
                **payload["lines"][0],
                "product_id": str(product.pk),
                "expected_available_quantity": 10,
            }
            for product in products[1:line_count]
        ]
        payload["expected_total_vnd"] = 45000 * line_count + 30000
        response = client.post("/api/v1/public/store-checkouts", payload, format="json")
        assert response.status_code == 201, response.json()
        data = response.json()["data"]
        order = approve_review_order(data, active_user)
        order = StoreOrder.objects.get(pk=order.pk)
        with CaptureQueriesContext(connection) as queries:
            result = order_payload(order, admin=True, request=APIRequestFactory().get("/"))
        assert len(result["allocations"]) == line_count
        assert all(item["restocked_quantity"] == 0 for item in result["allocations"])
        counts.append(len(queries))
    assert counts[1] == counts[0], counts


@pytest.mark.django_db
def test_legacy_approval_records_decision_after_locks_without_using_later_write_time(
    active_user, store_product, payment_settings, monkeypatch
):
    from django.db.models import QuerySet

    client = APIClient()
    data = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(data)
    client.post(
        f"/api/v1/public/store-orders/{data['id']}/report-transfer",
        {"access_token": data["guest_access_token"]},
        format="json",
    )
    started = timezone.now()
    deadline = started + timedelta(seconds=20)
    decision_at = started + timedelta(seconds=5)
    StoreOrder.objects.filter(pk=data["id"]).update(expires_at=deadline)
    clock = [started]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    real_fetch = QuerySet._fetch_all
    real_create = StoreStockAllocation.objects.create

    def source_ready(queryset):
        if queryset.model is FarmProductStockBatch and queryset.query.select_for_update:
            clock[0] = decision_at
        return real_fetch(queryset)

    def delayed_write(*args, **kwargs):
        clock[0] = deadline + timedelta(seconds=30)
        return real_create(*args, **kwargs)

    monkeypatch.setattr(QuerySet, "_fetch_all", source_ready)
    monkeypatch.setattr(StoreStockAllocation.objects, "create", delayed_write)
    order = approve_review_order(data, active_user)
    order.refresh_from_db()
    assert order.status == "pending_delivery"
    assert order.approved_at == decision_at
    assert order.payment_received_at == decision_at
    assert StoreStockMovement.objects.get().quantity == 2


@pytest.mark.django_db
def test_legacy_approval_preserves_first_recorded_payment_time(
    active_user, store_product, payment_settings, monkeypatch
):
    from commerce.store_orders import record_store_payment

    client = APIClient()
    data = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    legacy_checkout_snapshot(data)
    response = client.post(
        f"/api/v1/public/store-orders/{data['id']}/report-transfer",
        {"access_token": data["guest_access_token"]},
        format="json",
    )
    assert response.status_code == 200
    received_at = timezone.now()
    clock = [received_at]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    record_store_payment(
        order_id=data["id"], actor=active_user, idempotency_key=uuid.uuid4(), note="Bank checked"
    )
    clock[0] = received_at + timedelta(minutes=10)
    order = approve_review_order(data, active_user)
    order.refresh_from_db()
    assert order.payment_received_at == received_at
    assert order.approved_at == received_at + timedelta(minutes=10)
    clock[0] = received_at + timedelta(minutes=20)
    record_store_payment(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), note="Checked again"
    )
    response = client.get(
        f"/api/v1/public/store-orders/{order.pk}",
        HTTP_X_STORE_ACCESS_TOKEN=data["guest_access_token"],
    )
    assert response.status_code == 200
    assert response.json()["data"]["payment"]["received_at"] == received_at.isoformat()


@pytest.mark.django_db
def test_checkout_goods_review_deadline_after_bank_lock_and_replay_keeps_it(
    store_product, payment_settings, monkeypatch
):
    from django.db.models import QuerySet

    started = timezone.now()
    clock = [started]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    real_fetch = QuerySet._fetch_all

    def wait_for_settings(queryset):
        if queryset.model is MembershipPaymentSettings and queryset.query.select_for_update:
            clock[0] = started + timedelta(seconds=40)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", wait_for_settings)
    client = APIClient()
    payload = guest_payload(store_product)
    response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.json()
    order = StoreOrder.objects.get(pk=response.json()["data"]["id"])
    assert order.created_at == started + timedelta(seconds=40)
    assert order.expires_at == datetime.combine(
        order.delivery_date + timedelta(days=1), time.min, ZoneInfo("Asia/Ho_Chi_Minh")
    )
    clock[0] = order.created_at + timedelta(minutes=2)
    replay = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert replay.status_code == 200, replay.json()
    assert replay.json()["data"]["id"] == str(order.pk)
    assert replay.json()["data"]["expires_at"] == order.expires_at.isoformat()
    assert StoreOrder.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "blocked",
    [
        None,
        "batch_expired",
        "batch_cancelled",
        "offer_inactive",
        "farm_inactive",
        "fully_restocked",
    ],
)
def test_historical_cancelled_order_restock_projection_matches_server_eligibility(
    active_user, store_product, payment_settings, monkeypatch, blocked
):
    from commerce.store_orders import (
        StoreOrderError,
        approve_store_order,
        restock_store_order,
    )
    from commerce.store_views import order_payload

    client = APIClient()
    result = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )
    assert result.status_code == 201
    data = result.json()["data"]
    legacy_checkout_snapshot(data)
    client.post(
        f"/api/v1/public/store-orders/{data['id']}/report-transfer",
        {"access_token": data["guest_access_token"]},
        format="json",
    )
    order = StoreOrder.objects.get(pk=data["id"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    approve_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        allocations=[{"line_id": order.lines.get().pk, "batch_id": batch.pk, "quantity": 2}],
    )
    order = historical_cancelled_snapshot(order.pk, active_user)
    allocation = order.lines.get().stock_allocations.get()
    if blocked == "batch_expired":
        monkeypatch.setattr(
            timezone, "localdate", lambda *args, **kwargs: batch.end_date + timedelta(days=1)
        )
    elif blocked == "batch_cancelled":
        FarmProductStockBatch.objects.filter(pk=batch.pk).update(cancelled_at=timezone.now())
    elif blocked == "offer_inactive":
        FarmProduct.objects.filter(pk=batch.farm_product_id).update(
            is_active=False, removed_at=timezone.now()
        )
    elif blocked == "farm_inactive":
        Farm.objects.filter(pk=batch.farm_product.farm_id).update(is_active=False)
    elif blocked == "fully_restocked":
        restock_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            allocations=[{"allocation_id": allocation.pk, "quantity": 2}],
        )
    row = order_payload(order, admin=True)["allocations"][0]
    assert row["restock_eligible"] is (blocked is None)
    assert row["restock_unavailable_reason"] == blocked
    if blocked:
        with pytest.raises(StoreOrderError, match="store_restock_invalid"):
            restock_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                allocations=[{"allocation_id": allocation.pk, "quantity": 1}],
            )
    else:
        restock_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            allocations=[{"allocation_id": allocation.pk, "quantity": 1}],
        )
        updated = order_payload(order, admin=True)["allocations"][0]
        assert updated["restock_eligible"] is True
        assert updated["restock_remaining_quantity"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize("second_cost", [1, 2])
def test_approve_products_sharing_cycle_quota_aggregates_usage(
    zero_shipping_fee, active_user, access_token, store_product, payment_settings, second_cost
):
    attach_active_membership(active_user, store_product)
    second = Product.objects.create(
        category=store_product.category,
        csa_quota_group=store_product.csa_quota_group,
        sku="CUCUMBER",
        slug="cucumber",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=45_000,
        csa_quota_cost=second_cost,
        primary_image=store_product.primary_image,
        status="active",
        is_published=True,
    )
    ProductTranslation.objects.bulk_create(
        [
            ProductTranslation(product=second, locale="vi", name="Dưa leo"),
            ProductTranslation(product=second, locale="en", name="Cucumber"),
        ]
    )
    farm = Farm.objects.create(
        name_vi="Nông trại B",
        name_en="Farm B",
        location_vi="Huế",
        location_en="Hue",
    )
    offer = FarmProduct.objects.create(farm=farm, product=second)
    FarmProductStockBatch.objects.create(
        farm_product=offer,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=3),
        quantity_units=10,
    )
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    response = client.post(
        "/api/v1/public/store-checkouts",
        {
            "delivery_date": timezone.localdate(),
            "idempotency_key": str(uuid.uuid4()),
            "locale": "vi",
            "lines": [
                {
                    "product_id": str(store_product.pk),
                    "quantity": 1,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 1,
                },
                {
                    "product_id": str(second.pk),
                    "quantity": 1,
                    "use_quota": True,
                    "expected_unit_price_vnd": 45_000,
                    "expected_available_quantity": 10,
                    "expected_quota_quantity": 1,
                },
            ],
            "expected_shipping_fee_vnd": 0,
            "expected_total_vnd": 0,
            "shipping_address_updated_at": address.updated_at.isoformat(),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    from commerce.store_orders import allocation_suggestion, approve_store_order

    order = StoreOrder.objects.get(pk=response.json()["data"]["id"])
    allocations = [
        {
            "line_id": uuid.UUID(x["line_id"]),
            "batch_id": uuid.UUID(x["batch_id"]),
            "quantity": x["quantity"],
        }
        for x in allocation_suggestion(order)
    ]
    from commerce.store_orders import cancel_store_order
    from memberships.models import MembershipCycleQuota, MembershipUsageOrder

    key = uuid.uuid4()
    approve_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
    )
    approve_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=key, allocations=allocations
    )
    order.refresh_from_db()
    assert order.status == "pending_delivery"
    assert MembershipCycleQuota.objects.get().remaining_units == 4 - (2 + second_cost)
    usage = MembershipUsageOrder.objects.get(store_order=order)
    assert usage.lines.count() == 1
    line = usage.lines.get()
    assert line.units == 2 + second_cost
    assert line.allocations.count() == 1
    assert line.allocations.get().units == 2 + second_cost
    assert order.lines.filter(stock_allocations__isnull=False).count() == 2
    cancel_store_order(
        order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="test"
    )
    assert MembershipCycleQuota.objects.get().remaining_units == 4
    usage.refresh_from_db()
    assert usage.status == "reversed"


@pytest.mark.django_db
def test_buy_again_prepare_is_owner_scoped_and_uses_live_catalog(
    store_product, payment_settings, active_user, access_token
):
    from types import SimpleNamespace

    client = APIClient()
    created = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    order = StoreOrder.objects.get(pk=created["id"])
    url = f"/api/v1/store-orders/{order.pk}/buy-again?locale=en"
    assert client.get(url).status_code in (401, 403)
    client.force_authenticate(active_user, token=access_token)
    assert client.get(url).status_code == 404
    from rest_framework.test import APIRequestFactory

    from commerce.store_views import order_payload

    request = APIRequestFactory().get("/")
    request.user = active_user
    assert order_payload(order, request=request)["can_buy_again"] is False
    order.buyer_user = active_user
    order.save(update_fields=["buyer_user"])
    assert order_payload(order, request=request)["can_buy_again"] is True
    for previous_status in StoreOrderStatus.values:
        order.status = previous_status
        order.save(update_fields=["status"])
        assert client.get(url).status_code == 200
    order.status = StoreOrderStatus.DELIVERED
    order.save(update_fields=["status"])
    store_product.retail_price_vnd = 50000
    store_product.save(update_fields=["retail_price_vnd"])
    assert order_payload(order, request=request)["can_buy_again"] is True
    count = StoreOrder.objects.count()
    data = client.get(url).json()["data"]
    assert data["order_id"] == str(order.pk)
    assert data["lines"][0]["quantity"] == 2
    assert data["lines"][0]["product"]["retail_price_vnd"] == 50000
    assert data["lines"][0]["product"]["name"] == "Tomato"
    assert data["lines"][0]["reason"] is None
    # A separate pending order reserves 9, leaving only one available.
    client.force_authenticate(user=None)
    store_product.retail_price_vnd = 45000
    store_product.save(update_fields=["retail_price_vnd"])
    reserved = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product, quantity=9), format="json"
    )
    assert reserved.status_code == 201
    client.force_authenticate(active_user, token=access_token)
    row = client.get(url).json()["data"]["lines"][0]
    assert row["reason"] == "insufficient_stock"
    assert row["product"]["available_quantity"] == 1
    assert row["quantity"] == 2
    Product.objects.filter(pk=store_product.pk).update(
        status="discontinued", is_active=False, is_published=False
    )
    row = client.get(url).json()["data"]["lines"][0]
    assert row["reason"] == "unavailable"
    assert row["product"] is None
    assert StoreOrder.objects.count() == count + 1
    client.force_authenticate(active_user, token=SimpleNamespace(scope="profile"))
    assert client.get(url).status_code == 403


@pytest.mark.django_db
def test_buy_again_batches_all_lines_in_original_order_and_respects_category_visibility(
    store_product, payment_settings, active_user, access_token
):
    from django.test.utils import CaptureQueriesContext

    from commerce.models import StoreStockReservation

    client = APIClient()
    created = client.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    ).json()["data"]
    order = StoreOrder.objects.get(pk=created["id"])
    order.buyer_user = active_user
    order.status = StoreOrderStatus.DELIVERED
    order.save(update_fields=["buyer_user", "status"])
    client.force_authenticate(active_user, token=access_token)
    url = f"/api/v1/store-orders/{order.pk}/buy-again?locale=vi"
    with CaptureQueriesContext(connection) as single:
        assert client.get(url).status_code == 200
    original_id = store_product.pk
    second = Product.objects.get(pk=original_id)
    second.pk = None
    second.sku = "SECOND"
    second.slug = "second"
    second.save()
    ProductTranslation.objects.create(product=second, locale="vi", name="Sản phẩm thứ hai")
    line = order.lines.get()
    line.pk = None
    line.product = second
    line.position = 1
    line.quantity = 5
    line.cash_quantity = 5
    line.cash_subtotal_vnd = line.unit_price_vnd_snapshot * 5
    line.save()
    reservation_count = StoreStockReservation.objects.count()
    with CaptureQueriesContext(connection) as batch:
        response = client.get(url)
    assert len(batch) == len(single)
    rows = response.json()["data"]["lines"]
    assert [row["product_id"] for row in rows] == [str(original_id), str(second.pk)]
    assert [row["quantity"] for row in rows] == [2, 5]
    assert [row["reason"] for row in rows] == [None, "out_of_stock"]
    parent = Category.objects.create(slug="inactive-parent", is_active=False)
    category = second.category
    category.parent = parent
    category.save(update_fields=["parent"])
    rows = client.get(url).json()["data"]["lines"]
    assert [row["reason"] for row in rows] == ["unavailable", "unavailable"]
    assert StoreStockReservation.objects.count() == reservation_count
    assert StoreOrder.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("account_change", "confirmed_name", "confirmed_phone"),
    [
        ({"name": "Tên đã đổi"}, "Tên đã đổi", "+84901234567"),
        ({"login_phone_number": "0987654321"}, "Nông Dân Xanh", "+84987654321"),
    ],
)
def test_checkout_requires_confirmation_after_saved_contact_changes(
    active_user,
    access_token,
    store_product,
    payment_settings,
    account_change,
    confirmed_name,
    confirmed_phone,
):
    active_user.login_phone_number = "0901234567"
    active_user.save(update_fields=["login_phone_number"])
    address = ShippingAddress.objects.create(
        user=active_user,
        recipient_name="Tên cũ",
        phone_number="+84901234566",
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    quote_url = "/api/v1/public/store-quotes"
    checkout_url = "/api/v1/public/store-checkouts"
    request = {
        "delivery_date": timezone.localdate(),
        "lines": [{"product_id": str(store_product.pk), "quantity": 1, "use_quota": False}],
    }
    saved_address = client.get("/api/v1/shipping-address").json()["data"]
    quote = client.post(quote_url, request, format="json").json()["data"]
    assert saved_address["shipping_address_version"] == quote["shipping_address_version"]
    body = {
        "delivery_date": request["delivery_date"],
        "idempotency_key": str(uuid.uuid4()),
        "lines": quote["lines"],
        "shipping_address_updated_at": quote["shipping_address_updated_at"],
        "shipping_address_version": quote["shipping_address_version"],
        "expected_shipping_fee_vnd": quote["shipping_fee_vnd"],
        "expected_total_vnd": quote["total_vnd"],
    }

    type(active_user).objects.filter(pk=active_user.pk).update(**account_change)
    stale = client.post(checkout_url, body, format="json")
    assert stale.status_code == 409, stale.json()
    assert stale.json()["errors"] == [
        {"code": "store_requote_required", "context": {"reason": "shipping_address_changed"}}
    ]
    assert not StoreOrder.objects.exists()
    address.refresh_from_db()
    assert address.updated_at.isoformat() == body["shipping_address_updated_at"]

    fresh_address = client.get("/api/v1/shipping-address").json()["data"]
    fresh_quote = client.post(quote_url, request, format="json").json()["data"]
    assert fresh_address["recipient_name"] == confirmed_name
    assert fresh_address["phone_number"] == confirmed_phone
    assert fresh_address["shipping_address_version"] == fresh_quote["shipping_address_version"]
    assert fresh_quote["shipping_address_version"] != body["shipping_address_version"]
    body["shipping_address_version"] = fresh_quote["shipping_address_version"]
    created = client.post(checkout_url, body, format="json")
    assert created.status_code == 201, created.json()
    shipping_snapshot = created.json()["data"]["shipping_address"]
    assert shipping_snapshot["recipient_name"] == confirmed_name
    assert shipping_snapshot["phone_number"] == confirmed_phone

    type(active_user).objects.filter(pk=active_user.pk).update(name="Tên đổi sau đặt hàng")
    replay = client.post(checkout_url, body, format="json")
    assert replay.status_code == 200, replay.json()
    assert replay.json()["data"]["id"] == created.json()["data"]["id"]
    assert replay.json()["data"]["shipping_address"] == shipping_snapshot
    assert StoreOrder.objects.count() == 1


@pytest.mark.django_db
def test_timestamp_only_checkout_requires_legacy_contact_to_match_account(
    active_user, access_token, store_product, payment_settings
):
    active_user.login_phone_number = "0901234567"
    active_user.save(update_fields=["login_phone_number"])
    address = ShippingAddress.objects.create(
        user=active_user,
        recipient_name=active_user.name,
        phone_number="0901234567",
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    url = "/api/v1/public/store-checkouts"

    def attempt(expected_available):
        body = guest_payload(store_product, expected_available=expected_available)
        del body["guest"]
        return {**body, "shipping_address_updated_at": address.updated_at.isoformat()}

    first_body = attempt(expected_available=10)
    created = client.post(url, first_body, format="json")
    assert created.status_code == 201, created.json()
    snapshot = created.json()["data"]["shipping_address"]
    assert snapshot["phone_number"] == "+84901234567"

    unsubmitted_body = attempt(expected_available=8)
    type(active_user).objects.filter(pk=active_user.pk).update(login_phone_number="0987654321")
    stale = client.post(url, unsubmitted_body, format="json")
    assert stale.status_code == 409, stale.json()
    assert stale.json()["errors"] == [
        {"code": "store_requote_required", "context": {"reason": "shipping_address_changed"}}
    ]
    assert StoreOrder.objects.count() == 1

    replay = client.post(url, first_body, format="json")
    assert replay.status_code == 200, replay.json()
    assert replay.json()["data"]["id"] == created.json()["data"]["id"]
    assert replay.json()["data"]["shipping_address"] == snapshot


@pytest.mark.django_db
def test_store_quote_and_checkout_require_account_login_phone(
    active_user, access_token, store_product, payment_settings
):
    address = ShippingAddress.objects.create(
        user=active_user,
        recipient_name=active_user.name,
        phone_number="0901234567",
        province_code=PROVINCE,
        ward_code=WARD,
        address="Số 12 Nguyễn Huệ",
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    quote = client.post(
        "/api/v1/public/store-quotes",
        {
            "delivery_date": timezone.localdate(),
            "lines": [{"product_id": str(store_product.pk), "quantity": 1, "use_quota": False}],
        },
        format="json",
    )
    body = guest_payload(store_product)
    del body["guest"]
    body["shipping_address_updated_at"] = address.updated_at.isoformat()
    checkout = client.post("/api/v1/public/store-checkouts", body, format="json")

    for response in [quote, checkout]:
        assert response.status_code == 400, response.json()
        assert response.json()["errors"] == [{"code": "store_shipping_address_required"}]
    assert not StoreOrder.objects.exists()


@pytest.mark.django_db
def test_guest_checkout_cannot_supply_saved_address_version(store_product, payment_settings):
    body = {**guest_payload(store_product), "shipping_address_version": "a" * 64}
    response = APIClient().post("/api/v1/public/store-checkouts", body, format="json")
    assert response.status_code == 400, response.json()
    assert response.json()["errors"][0]["code"] == "store_shipping_address_invalid"
    assert not StoreOrder.objects.exists()


@pytest.mark.django_db
def test_voucher_preview_checkout_replay_and_cancel(
    store_product, payment_settings, active_user, access_token
):
    from memberships.models import CouponUsage, CSACoupon

    active_user.login_phone_number = "0901234567"
    active_user.save(update_fields=["login_phone_number"])
    ShippingAddress.objects.create(
        user=active_user,
        recipient_name="Voucher",
        phone_number="+84901234567",
        province_code=PROVINCE,
        ward_code=WARD,
        address="12 Nguyen Hue",
    )
    voucher = CSACoupon.objects.create(
        code="STORE30",
        application_scope="both",
        discount_type="fixed",
        discount_value=30_000,
        usage_limit=1,
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
    preview = client.post("/api/v1/store-voucher-preview", preview_input, format="json")
    assert preview.status_code == 200, preview.data
    quote = preview.json()["data"]
    assert quote["subtotal_vnd"] == "90000"
    assert quote["discount_amount_vnd"] == "30000"
    assert quote["total_vnd"] == "90000"
    assert not StoreOrder.objects.exists()
    assert not CouponUsage.objects.exists()
    voucher.refresh_from_db()
    assert voucher.used_count == 0
    body = {
        "delivery_date": timezone.localdate(),
        "idempotency_key": str(uuid.uuid4()),
        "locale": "vi",
        "coupon_code": voucher.code,
        "expected_voucher_quote": quote["voucher_quote"],
        "lines": quote["lines"],
        "shipping_address_updated_at": quote["shipping_address_updated_at"],
        "shipping_address_version": quote["shipping_address_version"],
        "expected_shipping_fee_vnd": 30_000,
        "expected_total_vnd": 90_000,
    }
    created = client.post("/api/v1/public/store-checkouts", body, format="json")
    assert created.status_code == 201, created.data
    order = StoreOrder.objects.get()
    assert (order.subtotal_vnd, order.discount_amount_vnd, order.total_vnd) == (
        90_000,
        30_000,
        90_000,
    )
    assert order.coupon_snapshot["code"] == "STORE30"
    assert CouponUsage.objects.get().user == active_user
    voucher.refresh_from_db()
    assert voucher.used_count == 1
    CSACoupon.objects.filter(pk=voucher.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    replay = client.post("/api/v1/public/store-checkouts", body, format="json")
    assert replay.status_code == 200, replay.data
    assert replay.json()["data"]["id"] == str(order.pk)
    from commerce.store_orders import cancel_pending_store_order

    key = uuid.uuid4()
    cancel_pending_store_order(
        order_id=order.pk, user=active_user, guest_token="", idempotency_key=key
    )
    cancel_pending_store_order(
        order_id=order.pk, user=active_user, guest_token="", idempotency_key=key
    )
    voucher.refresh_from_db()
    assert voucher.used_count == 0
    assert CouponUsage.objects.get().active_slot is None


@pytest.mark.django_db
def test_guest_voucher_is_rejected_without_order(store_product, payment_settings):
    body = guest_payload(store_product)
    body["coupon_code"] = "STORE30"
    response = APIClient().post("/api/v1/public/store-checkouts", body, format="json")
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "coupon_authentication_required"
    assert not StoreOrder.objects.exists()


@pytest.mark.django_db
def test_selling_unit_can_change_before_first_stock_but_stale_checkout_cannot_submit(
    store_product, payment_settings, active_user
):
    from commerce.services import CatalogConflict, update_product
    from farms.services import create_stock_batch

    product = store_product
    offer = product.farm_offers.get()
    # Fixture creates a normal stocked product. Represent the pre-first-batch state.
    offer.stock_batches.all().delete()
    client = APIClient()
    detail = f"/api/v1/public/store-products/{product.slug}?locale=vi"
    original = client.get(detail).json()["data"]
    assert original["unit_size"] == "1.000"
    assert original["available_quantity"] == 0
    stale = guest_payload(product, expected_available=0)
    unavailable = client.post("/api/v1/public/store-checkouts", stale, format="json")
    assert unavailable.status_code == 409
    assert unavailable.json()["errors"][0]["code"] == "store_stock_changed"

    update_product(actor=active_user, product=product, payload={"unit_size": Decimal("2.000")})
    today = timezone.localdate()
    create_stock_batch(
        farm_id=offer.farm_id,
        product_id=product.pk,
        expected_link_id=offer.pk,
        expected_version=offer.version,
        actor=active_user,
        data={"start_date": today, "end_date": today + timedelta(days=2), "quantity_units": 10},
    )
    rejected = client.post("/api/v1/public/store-checkouts", stale, format="json")
    assert rejected.status_code == 409
    assert rejected.json()["errors"][0]["code"] == "store_requote_required"
    assert not StoreOrder.objects.exists()

    refreshed = client.get(detail).json()["data"]
    assert refreshed["unit_size"] == "2.000"
    confirmed = guest_payload(product, expected_available=refreshed["available_quantity"])
    with pytest.raises(CatalogConflict, match="product_unit_in_use"):
        update_product(actor=active_user, product=product, payload={"unit_size": Decimal("3.000")})
    created = client.post("/api/v1/public/store-checkouts", confirmed, format="json")
    assert created.status_code == 201, created.json()
    assert StoreOrderLine.objects.get().unit_size_snapshot == Decimal("2.000")


@pytest.mark.django_db
@pytest.mark.parametrize("approve_first", [False, True])
def test_store_quota_reservation_uses_snapshot_after_product_cost_change(
    zero_shipping_fee, active_user, access_token, store_product, approve_first
):
    from commerce.services import update_product
    from commerce.store_orders import cancel_store_order
    from memberships.models import MembershipUsageReversal

    membership = attach_active_membership(active_user, store_product)
    address = ShippingAddress.objects.get(user=active_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(store_product, address, quantity=1),
        format="json",
    )
    assert response.status_code == 201, response.json()
    order = StoreOrder.objects.get(pk=response.json()["data"]["id"])
    line = order.lines.get()
    reserved = line.quota_reservations.get()
    original_cost = line.quota_cost_snapshot
    update_product(
        actor=active_user, product=store_product, payload={"csa_quota_cost": Decimal("3")}
    )
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    if approve_first:
        approve_review_order(response.json()["data"], active_user)
        reserved.refresh_from_db()
        quota.refresh_from_db()
        assert reserved.consumed_at is not None
        assert quota.remaining_units == quota.granted_units - reserved.units
    cancellation_key = uuid.uuid4()
    for _ in range(2):
        cancel_store_order(
            order_id=order.pk,
            actor=active_user,
            idempotency_key=cancellation_key,
            reason="Cancelled",
        )
    reserved.refresh_from_db()
    line.refresh_from_db()
    quota.refresh_from_db()
    assert (reserved.released_at is not None) is not approve_first
    assert MembershipUsageReversal.objects.filter(order__store_order=order).count() == int(
        approve_first
    )
    assert (reserved.consumed_at is not None) is approve_first
    assert line.quota_cost_snapshot == original_cost
    assert reserved.units == original_cost
    assert quota.remaining_units == quota.granted_units


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    not connection.features.has_select_for_update, reason="requires database row locking"
)
def test_checkout_and_discontinue_serialize_new_orders(
    store_product, payment_settings, active_user
):
    from commerce.services import transition_product

    start = Barrier(2)
    payload = guest_payload(store_product)

    def checkout():
        close_old_connections()
        try:
            start.wait(timeout=10)
            return APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
        finally:
            close_old_connections()

    def discontinue():
        close_old_connections()
        try:
            start.wait(timeout=10)
            transition_product(
                actor=active_user,
                product_id=store_product.pk,
                expected_status="active",
                target_status="discontinued",
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        checkout_result = pool.submit(checkout)
        transition_result = pool.submit(discontinue)
        response = checkout_result.result(timeout=30)
        transition_result.result(timeout=30)
    if response.status_code == 201:
        replay = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
        assert replay.json()["data"]["id"] == response.json()["data"]["id"]
        assert StoreOrder.objects.count() == 1
        assert StoreStockReservation.objects.count() == 1
    else:
        assert response.status_code == 400, response.json()
        assert response.json()["errors"][0]["code"] == "store_product_changed"
        assert not StoreOrder.objects.exists()
        assert not StoreStockReservation.objects.exists()


@pytest.mark.django_db
def test_discontinue_blocks_new_checkout_but_replays_and_approves_existing(
    store_product, payment_settings, active_user
):
    from commerce.services import transition_product
    from commerce.store_orders import cancel_store_order

    client = APIClient()
    url = "/api/v1/public/store-checkouts"
    original = guest_payload(store_product)
    response = client.post(url, original, format="json")
    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    hold = StoreStockReservation.objects.get()
    transition_product(
        actor=active_user,
        product_id=store_product.pk,
        expected_status="active",
        target_status="discontinued",
    )
    assert (
        client.get(f"/api/v1/public/store-products/{store_product.slug}?locale=vi").status_code
        == 404
    )
    hold.refresh_from_db()
    assert hold.released_at is None
    new = client.post(url, guest_payload(store_product, expected_available=8), format="json")
    assert new.status_code == 400, new.json()
    assert new.json()["errors"][0]["code"] == "store_product_changed"
    replay = client.post(url, original, format="json")
    assert replay.json()["data"]["id"] == data["id"]
    assert StoreOrder.objects.count() == 1
    assert StoreStockReservation.objects.count() == 1

    approved = approve_review_order(data, active_user)
    hold.refresh_from_db()
    assert approved.approved_at is not None
    assert hold.released_at is not None
    assert StoreStockMovement.objects.filter(kind="sale").count() == 1

    key = uuid.uuid4()
    for _ in range(2):
        cancel_store_order(
            order_id=approved.pk, actor=active_user, idempotency_key=key, reason="Cancelled"
        )
    assert StoreStockMovement.objects.filter(kind="restock").count() == 1
    assert StoreStockMovement.objects.get(kind="restock").quantity == hold.quantity


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    not connection.features.has_select_for_update, reason="requires database row locking"
)
def test_create_stock_batch_and_discontinue_serialize(store_product, active_user):
    from commerce.services import transition_product
    from farms.services import FarmConflict, create_stock_batch

    offer = store_product.farm_offers.get()
    start = Barrier(2)
    today = timezone.localdate()

    def add_stock():
        close_old_connections()
        try:
            start.wait(timeout=10)
            create_stock_batch(
                farm_id=offer.farm_id,
                product_id=store_product.pk,
                expected_link_id=offer.pk,
                expected_version=offer.version,
                actor=active_user,
                data={"start_date": today, "end_date": today, "quantity_units": 5},
            )
            return True
        except FarmConflict as error:
            assert error.code == "product_not_selling"
            return False
        finally:
            close_old_connections()

    def discontinue():
        close_old_connections()
        try:
            start.wait(timeout=10)
            transition_product(
                actor=active_user,
                product_id=store_product.pk,
                expected_status="active",
                target_status="discontinued",
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        intake = pool.submit(add_stock)
        transition = pool.submit(discontinue)
        created = intake.result(timeout=30)
        transition.result(timeout=30)
    store_product.refresh_from_db()
    assert store_product.status == "discontinued"
    assert offer.stock_batches.count() == (2 if created else 1)
