from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import Category, Product, ProductTranslation
from farms.models import Farm, FarmProduct, FarmProductStockBatch


@pytest.fixture
def farm_admin_api(active_user, access_token):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def make_farm(name="Green Farm", *, active=True):
    return Farm.objects.create(
        name_en=name,
        name_vi=name,
        location_en="Da Lat",
        location_vi="Đà Lạt",
        is_active=active,
    )


def make_product(sku="TOMATO-500G", *, active=True, published=True):
    category = Category.objects.create(slug=f"category-{sku.lower()}", is_active=True)
    product = Product.objects.create(
        category=category,
        sku=sku,
        slug=sku.lower(),
        unit_size="0.500",
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=25000,
        is_active=active,
        is_published=published,
    )
    ProductTranslation.objects.create(
        product=product,
        locale="vi",
        name="Cà chua",
        short_description="Tươi",
        description="Cà chua tươi",
    )
    return product


def batch_payload(*, start_date, end_date, quantity_units=10):
    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "quantity_units": quantity_units,
    }


@pytest.mark.django_db
def test_farm_payload_counts_only_active_links_by_current_stock(farm_admin_api):
    farm = make_farm()
    today = timezone.localdate()

    in_stock = FarmProduct.objects.create(farm=farm, product=make_product("IN-STOCK"))
    for quantity in (10, 15):
        FarmProductStockBatch.objects.create(
            farm_product=in_stock,
            start_date=today,
            end_date=today,
            quantity_units=quantity,
        )

    expired = FarmProduct.objects.create(farm=farm, product=make_product("EXPIRED"))
    FarmProductStockBatch.objects.create(
        farm_product=expired,
        start_date=today - timedelta(days=2),
        end_date=today - timedelta(days=1),
        quantity_units=20,
    )
    FarmProduct.objects.create(farm=farm, product=make_product("NO-BATCH"))
    cancelled = FarmProduct.objects.create(farm=farm, product=make_product("CANCELLED"))
    FarmProductStockBatch.objects.create(
        farm_product=cancelled,
        start_date=today,
        end_date=today,
        quantity_units=30,
        cancelled_at=timezone.now(),
    )
    stopped = FarmProduct.objects.create(
        farm=farm,
        product=make_product("STOPPED"),
        is_active=False,
    )
    FarmProductStockBatch.objects.create(
        farm_product=stopped,
        start_date=today,
        end_date=today,
        quantity_units=40,
    )

    listed = farm_admin_api.get("/api/v1/admin/farms")
    detailed = farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}")

    assert listed.status_code == 200
    assert detailed.status_code == 200
    list_row = next(row for row in listed.json()["data"] if row["id"] == str(farm.pk))
    assert list_row["in_stock_product_count"] == 1
    assert list_row["out_of_stock_product_count"] == 3
    assert detailed.json()["data"]["in_stock_product_count"] == 1
    assert detailed.json()["data"]["out_of_stock_product_count"] == 3


@pytest.mark.django_db
def test_stock_batches_allow_overlaps_and_enforce_valid_quantity_and_dates():
    relation = FarmProduct.objects.create(farm=make_farm(), product=make_product())
    today = timezone.localdate()

    first = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today,
        end_date=today + timedelta(days=6),
        quantity_units=10,
    )
    second = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today,
        end_date=today + timedelta(days=6),
        quantity_units=10,
    )

    assert first.pk != second.pk
    assert relation.stock_batches.count() == 2
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProductStockBatch.objects.create(
            farm_product=relation,
            start_date=today,
            end_date=today,
            quantity_units=0,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProductStockBatch.objects.create(
            farm_product=relation,
            start_date=today,
            end_date=today - timedelta(days=1),
            quantity_units=1,
        )


@pytest.mark.django_db
def test_linking_product_creates_first_batch_atomically_and_returns_stock_summary(farm_admin_api):
    farm = make_farm()
    product = make_product()
    today = timezone.localdate()

    response = farm_admin_api.post(
        f"/api/v1/admin/farms/{farm.pk}/products",
        {
            "product_id": str(product.pk),
            **batch_payload(
                start_date=today - timedelta(days=4),
                end_date=today + timedelta(days=2),
                quantity_units=10,
            ),
        },
        format="json",
    )

    assert response.status_code == 201, response.json()
    data = response.json()["data"]
    assert data["available_quantity"] == 10
    assert data["batch_count"] == 1
    assert data["active_batch_count"] == 1
    assert data["next_expiry_date"] == (today + timedelta(days=2)).isoformat()
    relation = FarmProduct.objects.get(farm=farm, product=product)
    batch = relation.stock_batches.get()
    assert (batch.start_date, batch.end_date, batch.quantity_units) == (
        today - timedelta(days=4),
        today + timedelta(days=2),
        10,
    )


@pytest.mark.django_db
def test_linking_product_rejects_expired_or_nonpositive_first_batch_without_partial_link(
    farm_admin_api,
):
    farm = make_farm()
    today = timezone.localdate()

    for index, payload in enumerate(
        [
            batch_payload(
                start_date=today - timedelta(days=10),
                end_date=today - timedelta(days=1),
            ),
            batch_payload(start_date=today, end_date=today, quantity_units=0),
            batch_payload(start_date=today + timedelta(days=1), end_date=today),
        ]
    ):
        product = make_product(f"INVALID-BATCH-{index}")
        response = farm_admin_api.post(
            f"/api/v1/admin/farms/{farm.pk}/products",
            {"product_id": str(product.pk), **payload},
            format="json",
        )
        assert response.status_code == 400, response.json()
        assert not FarmProduct.objects.filter(farm=farm, product=product).exists()


@pytest.mark.django_db
def test_admin_can_add_overlapping_batches_and_filter_computed_statuses(farm_admin_api):
    farm = make_farm()
    product = make_product()
    relation = FarmProduct.objects.create(farm=farm, product=product)
    today = timezone.localdate()
    base = f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/batches"

    for quantity in (10, 10):
        created = farm_admin_api.post(
            base,
            batch_payload(
                start_date=today - timedelta(days=1),
                end_date=today + timedelta(days=6),
                quantity_units=quantity,
            ),
            format="json",
        )
        assert created.status_code == 201, created.json()
        assert created.json()["data"]["status"] == "active"
    FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today + timedelta(days=7),
        end_date=today + timedelta(days=14),
        quantity_units=5,
    )
    expired = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today - timedelta(days=14),
        end_date=today - timedelta(days=7),
        quantity_units=50,
    )
    cancelled = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today,
        end_date=today + timedelta(days=2),
        quantity_units=30,
        cancelled_at=timezone.now(),
    )

    listed = farm_admin_api.get(base)
    assert listed.status_code == 200, listed.json()
    assert listed.json()["meta"] == {"page": 1, "page_size": 20, "total": 5}
    assert {row["status"] for row in listed.json()["data"]} == {
        "active",
        "upcoming",
        "expired",
        "cancelled",
    }
    assert (
        next(row for row in listed.json()["data"] if row["id"] == str(expired.pk))["status"]
        == "expired"
    )
    assert (
        next(row for row in listed.json()["data"] if row["id"] == str(cancelled.pk))["status"]
        == "cancelled"
    )
    active = farm_admin_api.get(f"{base}?status=active")
    assert active.json()["meta"]["total"] == 2
    assert sum(row["quantity_units"] for row in active.json()["data"]) == 20


@pytest.mark.django_db
def test_batch_update_uses_version_and_locks_started_or_historical_records(farm_admin_api):
    farm = make_farm()
    product = make_product()
    relation = FarmProduct.objects.create(farm=farm, product=product)
    today = timezone.localdate()
    active = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today - timedelta(days=1),
        end_date=today + timedelta(days=6),
        quantity_units=10,
    )
    detail = f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/batches/{active.pk}"

    changed = farm_admin_api.patch(
        detail,
        {
            "version": active.version,
            "quantity_units": 12,
            "end_date": (today + timedelta(days=7)).isoformat(),
        },
        format="json",
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["quantity_units"] == 12
    assert changed.json()["data"]["version"] == 2
    stale = farm_admin_api.patch(detail, {"version": 1, "quantity_units": 13}, format="json")
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "version_conflict"
    locked_start = farm_admin_api.patch(
        detail,
        {
            "version": 2,
            "start_date": (today - timedelta(days=2)).isoformat(),
        },
        format="json",
    )
    assert locked_start.status_code == 409
    assert locked_start.json()["errors"][0]["code"] == "stock_batch_start_locked"

    expired = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today - timedelta(days=10),
        end_date=today - timedelta(days=1),
        quantity_units=3,
    )
    expired_response = farm_admin_api.patch(
        f"{detail.rsplit('/', 1)[0]}/{expired.pk}",
        {"version": expired.version, "quantity_units": 4},
        format="json",
    )
    assert expired_response.status_code == 409
    assert expired_response.json()["errors"][0]["code"] == "stock_batch_expired"


@pytest.mark.django_db
def test_batch_cancel_is_soft_and_removes_it_from_current_stock(farm_admin_api):
    farm = make_farm()
    product = make_product()
    relation = FarmProduct.objects.create(farm=farm, product=product)
    today = timezone.localdate()
    batch = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today,
        end_date=today + timedelta(days=6),
        quantity_units=10,
    )

    response = farm_admin_api.post(
        f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/batches/{batch.pk}/cancel",
        {"version": batch.version},
        format="json",
    )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["status"] == "cancelled"
    batch.refresh_from_db()
    assert batch.cancelled_at is not None
    assert batch.cancelled_by_id is not None
    assert FarmProductStockBatch.objects.filter(pk=batch.pk).exists()
    products = farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}/products")
    assert products.json()["data"][0]["available_quantity"] == 0


@pytest.mark.django_db
def test_admin_product_search_does_not_multiply_stock_by_translation_count(farm_admin_api):
    farm = make_farm()
    product = make_product()
    ProductTranslation.objects.create(
        product=product,
        locale="en",
        name="Tomato",
        short_description="Fresh",
        description="Fresh tomato",
    )
    relation = FarmProduct.objects.create(farm=farm, product=product)
    today = timezone.localdate()
    FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=today,
        end_date=today,
        quantity_units=10,
    )

    response = farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}/products?search={product.sku}")

    assert response.status_code == 200
    assert response.json()["data"][0]["available_quantity"] == 10
    assert response.json()["data"][0]["active_batch_count"] == 1


@pytest.mark.django_db
def test_creating_an_additional_batch_locks_product_unit_definition(farm_admin_api):
    farm = make_farm()
    product = make_product()
    FarmProduct.objects.create(farm=farm, product=product)
    today = timezone.localdate()

    with patch.object(
        Product.objects,
        "select_for_update",
        wraps=Product.objects.select_for_update,
    ) as lock_product:
        response = farm_admin_api.post(
            f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/batches",
            batch_payload(start_date=today, end_date=today),
            format="json",
        )

    assert response.status_code == 201, response.json()
    lock_product.assert_called_once_with()
