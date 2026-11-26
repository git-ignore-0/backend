from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from commerce.models import Category, Product, StoreOrder, StoreStockReservation
from farms.models import Farm, FarmProduct, FarmProductStockBatch, LegacyFarmWeeklySupply
from memberships.models import CSASelection
from tests.test_commerce_catalog_api import admin_api as admin_api
from tests.test_csa_rounds import context as context
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db
PRODUCTS_URL = "/api/v1/admin/store-products"


def make_product(*, sku="CAP-1", category=None):
    return Product.objects.create(
        category=category or Category.objects.create(slug="capabilities"),
        sku=sku,
        slug=sku.lower(),
        unit_size=1,
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=1000,
    )


def make_farm_link(product):
    farm = Farm.objects.create(name_vi="Vườn Rau", name_en="Vegetable Farm")
    return FarmProduct.objects.create(product=product, farm=farm)


def make_order_line(product):
    order = StoreOrder.objects.create(
        reference_code="CAP-ORDER",
        create_idempotency_key=uuid4(),
        locale="vi",
        subtotal_vnd=1000,
        shipping_fee_vnd=0,
        total_vnd=1000,
    )
    return order.lines.create(
        product=product,
        position=0,
        sku_snapshot=product.sku,
        name_snapshot="Historical tomato",
        unit_size_snapshot=1,
        unit_label_snapshot="kg",
        unit_price_vnd_snapshot=1000,
        quantity=1,
        cash_quantity=1,
        cash_subtotal_vnd=1000,
    )


def read_capabilities(admin_api, product):
    response = admin_api.get(f"{PRODUCTS_URL}/{product.pk}")
    assert response.status_code == 200
    return response.json()["data"]["capabilities"]


def assert_delete_protected(admin_api, product):
    response = admin_api.delete(f"{PRODUCTS_URL}/{product.pk}")
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "product_in_use"
    assert Product.objects.filter(pk=product.pk).exists()


def test_farm_source_reasons_include_bilingual_name_and_one_reference_per_kind(admin_api):
    product = make_product()
    link = make_farm_link(product)
    today = timezone.localdate()
    batches = [
        FarmProductStockBatch.objects.create(
            farm_product=link,
            start_date=today - timedelta(days=offset),
            end_date=today - timedelta(days=offset),
            quantity_units=5,
        )
        for offset in (1, 2)
    ]
    supplies = [
        LegacyFarmWeeklySupply.objects.create(
            farm_product=link, week_start=today - timedelta(weeks=offset), quantity_units=2
        )
        for offset in (1, 2)
    ]
    farm_reference = {
        "farm_id": str(link.farm_id),
        "farm_name_vi": link.farm.name_vi,
        "farm_name_en": link.farm.name_en,
    }

    capabilities = read_capabilities(admin_api, product)

    assert capabilities["edit_identity"] == {
        "allowed": False,
        "reasons": [
            {"code": "stock_batch", "id": str(min(row.pk for row in batches)), **farm_reference},
            {"code": "legacy_supply", "id": str(min(row.pk for row in supplies)), **farm_reference},
        ],
    }
    assert capabilities["edit_csa"]["allowed"] is True
    assert capabilities["delete"]["allowed"] is False
    assert capabilities["delete"]["reasons"][-1] == {
        "code": "farm_link",
        "id": str(link.pk),
        **farm_reference,
    }
    assert_delete_protected(admin_api, product)
    assert link.stock_batches.count() == link.weekly_supplies.count() == 2


def test_farm_link_without_stock_still_blocks_deletion(admin_api):
    product = make_product()
    link = make_farm_link(product)
    capabilities = read_capabilities(admin_api, product)

    assert capabilities["edit_identity"]["allowed"] is True
    assert capabilities["delete"] == {
        "allowed": False,
        "reasons": [
            {
                "code": "farm_link",
                "id": str(link.pk),
                "farm_id": str(link.farm_id),
                "farm_name_vi": link.farm.name_vi,
                "farm_name_en": link.farm.name_en,
            }
        ],
    }
    assert_delete_protected(admin_api, product)


def test_store_order_line_prevents_deletion_and_retains_order_reference(admin_api):
    product = make_product()
    line = make_order_line(product)
    capabilities = read_capabilities(admin_api, product)

    assert capabilities["delete"] == {
        "allowed": False,
        "reasons": [{"code": "store_order", "id": str(line.order_id), "label": "CAP-ORDER"}],
    }
    assert_delete_protected(admin_api, product)
    line.refresh_from_db()
    assert line.sku_snapshot == product.sku


def test_store_reservation_independently_protects_referenced_product(admin_api):
    inventory_product = make_product()
    ordered_product = make_product(sku="ORDERED", category=inventory_product.category)
    line = make_order_line(ordered_product)
    # Exercise PROTECT even if a historical reservation references a different product.
    reservation = StoreStockReservation.objects.create(
        line=line, product=inventory_product, quantity=1, released_at=timezone.now()
    )
    capabilities = read_capabilities(admin_api, inventory_product)

    assert capabilities["delete"] == {
        "allowed": False,
        "reasons": [{"code": "store_reservation", "id": str(line.order_id), "label": "CAP-ORDER"}],
    }
    assert_delete_protected(admin_api, inventory_product)
    reservation.refresh_from_db()
    assert reservation.released_at is not None


@pytest.mark.parametrize(
    ("reference", "state", "reason", "can_edit"),
    [
        ("line", CSASelection.State.CANCELLED, "csa_history", True),
        ("hold", CSASelection.State.SELECTING, "csa_hold", False),
        ("hold", CSASelection.State.CANCELLED, "csa_history", True),
    ],
)
def test_each_csa_reference_protects_product_and_identifies_round(
    admin_api, context, reference, state, reason, can_edit
):
    _, existing_product, member, round_, _ = context
    product = make_product(category=existing_product.category)
    selection = CSASelection.objects.create(membership=member, round=round_, state=state)
    if reference == "line":
        row = selection.lines.create(product=product, quantity=1, snapshot={"sku": "original"})
    else:
        row = selection.stock_holds.create(product=product, quantity=1)

    capabilities = read_capabilities(admin_api, product)

    assert capabilities["delete"] == {
        "allowed": False,
        "reasons": [{"code": reason, "id": str(round_.pk), "label": round_.name}],
    }
    assert capabilities["edit_identity"]["allowed"] is can_edit
    assert capabilities["edit_csa"]["allowed"] is can_edit
    assert_delete_protected(admin_api, product)
    row.refresh_from_db()
    assert row.quantity == 1


def test_product_list_excludes_capabilities_and_keeps_query_count_bounded(admin_api):
    product = make_product()
    make_farm_link(product)

    with CaptureQueriesContext(connection) as one_row_queries:
        first = admin_api.get(PRODUCTS_URL)
    assert first.status_code == 200
    assert len(first.json()["data"]) == 1

    for number in range(2, 7):
        make_farm_link(make_product(sku=f"CAP-{number}", category=product.category))
    with CaptureQueriesContext(connection) as many_row_queries:
        response = admin_api.get(PRODUCTS_URL)

    assert response.status_code == 200
    assert len(response.json()["data"]) == 6
    assert all("capabilities" not in row for row in response.json()["data"])
    assert len(many_row_queries) == len(one_row_queries)
