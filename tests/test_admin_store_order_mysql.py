"""Verify checkout and receipt races against real MySQL row locks."""

from functools import partial
from uuid import uuid4

import pytest
from django.db import connection

from commerce.admin_store_orders import create_admin_store_order, quote_admin_store_order
from commerce.models import (
    CatalogTreeLock,
    Product,
    ShippingAddress,
    StoreOrder,
    StoreStockReservation,
)
from commerce.serializers import (
    AdminStoreOrderCreateSerializer,
    AdminStoreOrderQuoteSerializer,
    StoreCheckoutSerializer,
)
from commerce.store_orders import StoreOrderError, create_store_checkout
from content.models import AdminCreationReceipt
from tests.test_admin_store_order_creation import buyer as buyer
from tests.test_admin_store_order_creation import order_body
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_product_lifecycle_locking import run_in_lock_order
from tests.test_store_orders import guest_payload
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


def admin_create_payload(actor, product, quantity, kind="new", buyer=None):
    body = order_body(product, quantity=quantity, kind=kind, buyer=buyer)
    quote_request = AdminStoreOrderQuoteSerializer(data=body)
    quote_request.is_valid(raise_exception=True)
    quote = quote_admin_store_order(actor=actor, payload=quote_request.validated_data)
    request = AdminStoreOrderCreateSerializer(
        data={
            **body,
            "confirmation": quote["confirmation"],
            "idempotency_key": str(uuid4()),
        }
    )
    request.is_valid(raise_exception=True)
    return request.validated_data


@pytest.mark.parametrize("admin_first", [True, False])
def test_admin_and_public_checkout_cannot_oversell(
    active_user,
    store_product,
    payment_settings,
    admin_first,
):
    admin_payload = admin_create_payload(active_user, store_product, 6)
    public_request = StoreCheckoutSerializer(data=guest_payload(store_product, quantity=6))
    public_request.is_valid(raise_exception=True)
    admin, public = run_in_lock_order(
        partial(create_admin_store_order, actor=active_user, payload=admin_payload),
        partial(create_store_checkout, user=None, payload=public_request.validated_data),
        left_first=admin_first,
        lock_model=CatalogTreeLock,
    )
    winner, loser = (admin, public) if admin_first else (public, admin)
    assert winner.result()[-1] is True
    with pytest.raises(StoreOrderError) as caught:
        loser.result()
    assert caught.value.code in {"store_stock_changed", "store_requote_required"}
    assert StoreOrder.objects.count() == 1
    assert StoreStockReservation.objects.get().quantity == 6
    assert AdminCreationReceipt.objects.count() == int(admin_first)


@pytest.mark.parametrize("kind", ["new", "historical"])
def test_concurrent_admin_replays_commit_one_receipt(
    active_user,
    store_product,
    payment_settings,
    kind,
):
    payload = admin_create_payload(active_user, store_product, 2, kind)
    operation = partial(create_admin_store_order, actor=active_user, payload=payload)
    first, second = run_in_lock_order(
        operation,
        operation,
        left_first=True,
        lock_model=CatalogTreeLock if kind == "new" else Product,
    )
    results = [first.result(), second.result()]
    assert [created for _, created in results] == [True, False]
    resource = StoreOrder.objects.get()
    assert all(receipt.resource_id == resource.pk for receipt, _ in results)
    assert AdminCreationReceipt.objects.count() == 1
    if kind == "new":
        assert StoreStockReservation.objects.get().quantity == 2
    else:
        assert not StoreStockReservation.objects.exists()


@pytest.mark.parametrize("new_first", [True, False])
def test_cross_kind_key_conflict_with_a_shared_address(
    active_user, buyer, store_product, payment_settings, new_first
):
    past_product = Product.objects.create(
        category=store_product.category,
        sku="FORMER",
        slug="former",
        unit_size=1,
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=1,
        status="discontinued",
        is_active=False,
    )
    retail = admin_create_payload(active_user, store_product, 2, buyer=buyer)
    historical = admin_create_payload(active_user, past_product, 2, "historical", buyer=buyer)
    historical["idempotency_key"] = retail["idempotency_key"]
    new, past = run_in_lock_order(
        partial(create_admin_store_order, actor=active_user, payload=retail),
        partial(create_admin_store_order, actor=active_user, payload=historical),
        left_first=new_first,
        lock_model=ShippingAddress,
    )
    winner, loser = (new, past) if new_first else (past, new)
    assert winner.result()[1] is True
    with pytest.raises(StoreOrderError) as caught:
        loser.result()
    assert caught.value.code == "store_idempotency_conflict"
    assert StoreOrder.objects.get().is_historical is not new_first
    assert AdminCreationReceipt.objects.count() == 1
