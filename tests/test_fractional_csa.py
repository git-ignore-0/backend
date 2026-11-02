"""CSA point debits preserve hundredths across checkout, holds, usage and reversal."""

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from commerce.models import Product, ShippingAddress, StoreOrder
from commerce.serializers import ProductWriteSerializer
from commerce.store_orders import cancel_store_order
from memberships.models import MembershipCycleQuota, MembershipUsageLine
from memberships.serializers import UsageLineSerializer
from memberships.services import (
    QuotaError,
    create_usage_order,
    undo_usage_order,
)
from tests.test_commerce_catalog_api import admin_api as admin_api
from tests.test_store_orders import (
    approve_review_order,
    attach_active_membership,
    quota_checkout_payload,
)
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area


@pytest.mark.parametrize("cost", ["0.25", "0.5", "0.75", "1", "2", "2147483647"])
def test_product_cost_accepts_hundredths(cost):
    serializer = ProductWriteSerializer(data={"csa_quota_cost": cost}, partial=True)
    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["csa_quota_cost"] == Decimal(cost)


@pytest.mark.parametrize(
    "cost",
    [
        "0.1",
        "0.2",
        "0.3",
        "0.9",
        "1.1",
        "1.25",
        "1.5",
        "2.75",
        "0.001",
        "-0.25",
        "NaN",
        "Infinity",
        "2147483648",
    ],
)
def test_product_cost_rejects_invalid_precision_and_range(cost):
    serializer = ProductWriteSerializer(data={"csa_quota_cost": cost}, partial=True)
    assert not serializer.is_valid()


@pytest.fixture
def csa_context(active_user, access_token, store_product):
    membership = attach_active_membership(active_user, store_product)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return membership, client


def checkout(client, product, user, *, quantity=1, expected_available=10, quota_quantity=None):
    payload = quota_checkout_payload(
        product,
        ShippingAddress.objects.get(user=user),
        quantity=quantity,
        expected_available=expected_available,
    )
    if quota_quantity is not None:
        payload["lines"][0]["expected_quota_quantity"] = quota_quantity
        payload["expected_total_vnd"] = (quantity - quota_quantity) * 45_000
    response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def quota_projection(client):
    response = client.get("/api/v1/memberships/quota")
    assert response.status_code == 200, response.json()
    return response.json()["data"]["products"][0]


@pytest.mark.django_db
def test_admin_cost_public_catalog_and_checkout_keep_fractional_contract(
    active_user, store_product, admin_api, csa_context
):
    response = admin_api.patch(
        f"/api/v1/admin/store-products/{store_product.pk}",
        {"csa_quota_cost": 0.25},
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["csa_quota_cost"] == 0.25
    public = admin_api.get(f"/api/v1/public/store-products/{store_product.slug}?locale=vi")
    assert public.status_code == 200
    assert public.json()["data"]["csa_quota_group"]["cost"] == 0.25
    data = checkout(csa_context[1], store_product, active_user)
    assert data["lines"][0]["quota_cost"] == 0.25
    assert data["lines"][0]["quota_units"] == 0.25
    assert data["lines"][0]["quota_quantity"] == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "cost,orders,remaining", [("0.25", 4, "3"), ("0.5", 2, "3"), ("0.1", 10, "3")]
)
def test_repeat_orders_debit_exactly_one_point(
    active_user, store_product, csa_context, cost, orders, remaining
):
    membership, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal(cost))
    for index in range(orders):
        data = checkout(client, store_product, active_user, expected_available=10 - index)
        held = quota_projection(client)
        expected_after = Decimal("4") - Decimal(cost) * (index + 1)
        assert Decimal(str(held["available_units"])) == expected_after
        assert Decimal(str(held["reserved_units"])) == Decimal(cost)
        approve_review_order(data, active_user)
        current = quota_projection(client)
        assert Decimal(str(current["remaining_units"])) == expected_after
        assert current["reserved_units"] == 0
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == Decimal(remaining)
    assert sum(
        MembershipUsageLine.objects.filter(order__membership=membership).values_list(
            "units", flat=True
        )
    ) == Decimal("1")


@pytest.mark.django_db
def test_cancel_hold_and_cancel_approved_restore_exact_fraction(
    active_user, store_product, csa_context
):
    membership, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.25"))
    first = checkout(client, store_product, active_user)
    cancel_store_order(
        order_id=first["id"], actor=active_user, idempotency_key=uuid.uuid4(), reason="Test cancel"
    )
    assert quota_projection(client)["available_units"] == 4
    second = checkout(client, store_product, active_user)
    approve_review_order(second, active_user)
    assert quota_projection(client)["remaining_units"] == 3.75
    cancel_store_order(
        order_id=second["id"],
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        reason="Test reversal",
    )
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == Decimal("4")
    assert quota.expired_units == 0


@pytest.mark.django_db
def test_small_balance_covers_only_whole_selling_units(
    active_user, store_product, csa_context, payment_settings
):
    membership, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.25"))
    MembershipCycleQuota.objects.filter(cycle__membership=membership).update(
        remaining_units=Decimal("0.5")
    )
    data = checkout(client, store_product, active_user, quantity=3, quota_quantity=2)
    assert data["lines"][0]["quota_quantity"] == 2
    assert data["lines"][0]["cash_quantity"] == 1
    assert data["lines"][0]["quota_units"] == 0.5
    assert data["subtotal_vnd"] == "45000"
    assert quota_projection(client)["available_units"] == 0


@pytest.mark.django_db
def test_fractional_manual_usage_undo_and_replay_preserve_balance(
    active_user, store_product, csa_context
):
    membership, _ = csa_context
    key = uuid.uuid4()
    lines = [{"product_id": store_product.csa_quota_group_id, "units": "0.25"}]
    serializer = UsageLineSerializer(data=lines[0])
    assert serializer.is_valid(), serializer.errors
    usage, created = create_usage_order(
        membership_id=membership.pk, actor=active_user, idempotency_key=key, lines=lines
    )
    assert created
    replay, created = create_usage_order(
        membership_id=membership.pk, actor=active_user, idempotency_key=key, lines=lines
    )
    assert not created and replay.pk == usage.pk
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == Decimal("3.75")
    undo_usage_order(order_id=usage.pk, actor=active_user, reason="Test reversal")
    quota.refresh_from_db()
    assert quota.remaining_units == Decimal("4")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "units", ["0.1", "0.2", "0.9", "1.25", "2.5", "0.001", "0", "-0.25", "NaN", "Infinity"]
)
def test_manual_service_rejects_invalid_fractional_debits(
    active_user, store_product, csa_context, units
):
    membership, _ = csa_context
    with pytest.raises(QuotaError):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": store_product.csa_quota_group_id, "units": units}],
        )


@pytest.mark.django_db
def test_cycle_expiry_keeps_tenth_point_hold_without_precision_error(
    active_user, store_product, csa_context
):
    from datetime import timedelta

    from django.utils import timezone

    from memberships.services import ensure_due_cycles

    membership, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.1"))
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    quota.granted_units = 1
    quota.remaining_units = 1
    quota.save(update_fields=["granted_units", "remaining_units"])
    boundary = timezone.localdate() + timedelta(days=1)
    quota.cycle.end_date = boundary
    quota.cycle.save(update_fields=["end_date"])
    data = checkout(client, store_product, active_user)
    StoreOrder.objects.filter(pk=data["id"]).update(expires_at=timezone.now() + timedelta(days=2))
    ensure_due_cycles(membership, as_of=boundary)
    quota.refresh_from_db()
    assert quota.remaining_units == Decimal("0.1")
    assert quota.expired_units == Decimal("0.9")


@pytest.mark.django_db
def test_catalog_cost_change_does_not_reprice_reserved_quota(
    active_user, store_product, csa_context
):
    _, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.25"))
    data = checkout(client, store_product, active_user)
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.5"))
    approve_review_order(data, active_user)
    assert quota_projection(client)["remaining_units"] == 3.75
    line = StoreOrder.objects.get(pk=data["id"]).lines.get()
    assert line.quota_cost_snapshot == Decimal("0.25")
    assert line.quota_units == Decimal("0.25")


@pytest.mark.django_db
def test_manual_usage_cannot_spend_fraction_held_by_store(active_user, store_product, csa_context):
    from memberships.services import InsufficientQuota

    membership, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.25"))
    checkout(client, store_product, active_user)
    with pytest.raises(InsufficientQuota):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": store_product.csa_quota_group_id, "units": "4"}],
        )
    assert quota_projection(client)["remaining_units"] == 4
    assert quota_projection(client)["available_units"] == 3.75


@pytest.mark.django_db
@pytest.mark.parametrize("remaining,orders", [("1", 10), ("0.7", 7)])
def test_repeated_tenth_point_approvals_exhaust_balance_exactly(
    active_user, store_product, csa_context, remaining, orders
):
    membership, client = csa_context
    Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.1"))
    MembershipCycleQuota.objects.filter(cycle__membership=membership).update(
        granted_units=1,
        remaining_units=Decimal(remaining),
        expired_units=Decimal("1") - Decimal(remaining),
    )
    for index in range(orders):
        data = checkout(client, store_product, active_user, expected_available=10 - index)
        approve_review_order(data, active_user)
    result = quota_projection(client)
    assert result["remaining_units"] == 0
    assert result["available_units"] == 0
    assert result["reserved_units"] == 0


@pytest.mark.parametrize("units", ["0.1", "0.9", "1.25", "2.5"])
def test_manual_serializer_rejects_unsupported_point_steps(units):
    serializer = UsageLineSerializer(data={"product_id": uuid.uuid4(), "units": units})
    assert not serializer.is_valid()


@pytest.mark.parametrize("units", ["0.25", "0.5", "0.75", "1", "2"])
def test_manual_serializer_accepts_supported_point_steps(units):
    serializer = UsageLineSerializer(data={"product_id": uuid.uuid4(), "units": units})
    assert serializer.is_valid(), serializer.errors


@pytest.mark.django_db
@pytest.mark.parametrize("cost", ["0.1", "1.25", "2.5"])
def test_catalog_service_rejects_unsupported_point_steps(
    active_user, store_product, csa_context, cost
):
    from rest_framework.exceptions import ValidationError

    from commerce.services import update_product

    with pytest.raises(ValidationError):
        update_product(
            actor=active_user, product=store_product, payload={"csa_quota_cost": Decimal(cost)}
        )
