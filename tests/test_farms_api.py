from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection
from django.db.models.deletion import ProtectedError, RestrictedError
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from django.utils.module_loading import import_string
from rest_framework.test import APIClient

from commerce.models import (
    Category,
    Product,
    ProductTranslation,
    StoreOrder,
    StoreOrderLine,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
)
from content.models import ContentAuditLog, MediaAsset, TrackerFarm, TrackerFarmPhoto
from farms.models import (
    Farm,
    FarmPhoto,
    FarmProduct,
    FarmProductStockBatch,
    LegacyFarmWeeklySupply,
)
from farms.services import FarmConflict, delete_farm
from tests.test_farm_stock_batches import make_product
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_store_orders import approve_review_order
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area


@pytest.fixture
def farm_admin_api(active_user, access_token):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def farm_payload(**overrides):
    data = {
        "name_en": "Green Farm",
        "name_vi": "Nông trại Xanh",
        "location_en": "Da Lat",
        "location_vi": "Đà Lạt",
        "description_en": "Organic vegetables",
        "description_vi": "Rau hữu cơ",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_admin_can_manage_canonical_farm_without_creating_tracker_profile(farm_admin_api):
    created = farm_admin_api.post("/api/v1/admin/farms", farm_payload(), format="json")
    assert created.status_code == 201, created.json()
    data = created.json()["data"]
    assert data["name_en"] == "Green Farm"
    assert data["photos"] == []
    assert data["is_active"] is True
    assert Farm.objects.filter(pk=data["id"]).exists()
    assert not TrackerFarm.objects.filter(farm_id=data["id"]).exists()

    listed = farm_admin_api.get("/api/v1/admin/farms?search=Green")
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()["data"]] == [data["id"]]
    assert "capabilities" not in listed.json()["data"][0]
    detail = farm_admin_api.get(f"/api/v1/admin/farms/{data['id']}")
    assert detail["Cache-Control"] == "private, no-store"
    assert detail.json()["data"]["capabilities"] == {"delete": {"allowed": True, "reasons": []}}

    changed = farm_admin_api.patch(
        f"/api/v1/admin/farms/{data['id']}",
        {"version": data["version"], "name_vi": "Trang trại Xanh"},
        format="json",
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["name_vi"] == "Trang trại Xanh"
    assert changed.json()["data"]["version"] == data["version"] + 1
    assert changed.json()["data"]["capabilities"] == detail.json()["data"]["capabilities"]

    stale = farm_admin_api.patch(
        f"/api/v1/admin/farms/{data['id']}",
        {"version": data["version"], "name_vi": "Ghi đè"},
        format="json",
    )
    assert stale.status_code == 409


@pytest.mark.django_db
def test_farm_product_link_and_stock_batch_are_scoped_to_farm(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    other = Farm.objects.create(**farm_payload(name_en="Other Farm"))
    category = Category.objects.create(slug="greens", is_active=True)
    product = Product.objects.create(
        status="active",
        is_published=True,
        category=category,
        sku="GREEN-500G",
        slug="green-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
    )
    url = f"/api/v1/admin/farms/{farm.pk}/products"
    today = timezone.localdate()
    payload = {"product_ids": [str(product.pk)], "idempotency_key": str(uuid4())}
    linked = farm_admin_api.post(f"{url}/bulk", payload, format="json")
    assert linked.status_code == 201, linked.json()
    source = FarmProduct.objects.get(farm=farm, product=product)
    assert not source.stock_batches.exists()
    duplicate = farm_admin_api.post(
        f"{url}/bulk", {**payload, "idempotency_key": str(uuid4())}, format="json"
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["errors"][0]["code"] == "product_already_linked"
    created = farm_admin_api.post(
        f"{url}/{product.pk}/batches",
        lifecycle_command(
            source,
            start_date=today.isoformat(),
            end_date=(today + timedelta(days=6)).isoformat(),
            quantity_units=10,
        ),
        format="json",
    )
    assert created.status_code == 201, created.json()
    assert source.stock_batches.get().quantity_units == 10
    assert (
        farm_admin_api.post(
            f"/api/v1/admin/farms/{other.pk}/products/{product.pk}/batches",
            lifecycle_command(
                source, start_date=today.isoformat(), end_date=today.isoformat(), quantity_units=10
            ),
            format="json",
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_farm_cannot_link_product_deactivated_after_catalog_selection(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="deactivated-product", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="DEACTIVATED-500G",
        slug="deactivated-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
        status="active",
        is_published=True,
    )
    base = f"/api/v1/admin/farms/{farm.pk}"
    available = farm_admin_api.get(f"{base}/available-products")
    assert any(row["id"] == str(product.pk) for row in available.json()["data"])

    Product.objects.filter(pk=product.pk).update(
        status="discontinued", is_active=False, is_published=False
    )
    response = farm_admin_api.post(
        f"{base}/products/bulk",
        {"product_ids": [str(product.pk)], "idempotency_key": str(uuid4())},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "product_inactive"
    assert not FarmProduct.objects.filter(farm=farm, product=product).exists()


@pytest.mark.django_db
def test_farm_product_list_is_paginated_searchable_and_contains_display_summary(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="farm-product-page", is_active=True)
    for index in range(21):
        product = Product.objects.create(
            status="active",
            is_published=True,
            category=category,
            sku=f"FARM-PRODUCT-{index:02d}",
            slug=f"farm-product-{index:02d}",
            unit_size="500.000",
            unit_label_vi="gói",
            unit_label_en="pack",
            retail_price_vnd=25000,
        )
        ProductTranslation.objects.create(product=product, locale="vi", name=f"Rau số {index:02d}")
        FarmProduct.objects.create(farm=farm, product=product)

    url = f"/api/v1/admin/farms/{farm.pk}/products"
    first = farm_admin_api.get(f"{url}?page=1")
    assert first.status_code == 200
    assert first.json()["meta"] == {"page": 1, "page_size": 20, "total": 21}
    assert len(first.json()["data"]) == 20
    assert first.json()["data"][0]["name_vi"] == "Rau số 00"
    assert first.json()["data"][0]["name_en"] == "Rau số 00"
    assert first.json()["data"][0]["unit_size"] == "500.000"
    assert first.json()["data"][0]["unit_label_vi"] == "gói"
    second = farm_admin_api.get(f"{url}?page=2")
    assert second.json()["meta"]["total"] == 21
    assert len(second.json()["data"]) == 1
    searched = farm_admin_api.get(f"{url}?search=Rau+s%E1%BB%91+07")
    assert searched.json()["meta"]["total"] == 1
    assert searched.json()["data"][0]["sku"] == "FARM-PRODUCT-07"


@pytest.mark.django_db
def test_available_products_excludes_all_farm_links_across_pages(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="farm-available", is_active=True)
    for index in range(31):
        product = Product.objects.create(
            status="active",
            is_published=True,
            category=category,
            sku=f"AVAILABLE-{index:02d}",
            slug=f"available-{index:02d}",
            unit_size="1.000",
            unit_label_vi="gói",
            unit_label_en="pack",
            retail_price_vnd=10000,
            is_active=True,
        )
        ProductTranslation.objects.create(product=product, locale="vi", name=f"Rau {index:02d}")
        if index == 30:
            FarmProduct.objects.create(farm=farm, product=product)
    response = farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}/available-products")
    assert response.status_code == 200
    assert response.json()["meta"] == {"page": 1, "page_size": 30, "total": 30}
    assert all(item["sku"] != "AVAILABLE-30" for item in response.json()["data"])
    searched = farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}/available-products?search=Rau+30")
    assert searched.json()["meta"]["total"] == 0


@pytest.mark.django_db
def test_farm_rejects_media_outside_its_namespace(farm_admin_api):
    image = MediaAsset.objects.create(
        file="products/not-a-farm.webp",
        variants=[],
        width=100,
        height=100,
        original_name="not-a-farm.webp",
    )
    response = farm_admin_api.post(
        "/api/v1/admin/farms",
        farm_payload(photo_ids=[str(image.pk)]),
        format="json",
    )
    assert response.status_code == 400
    assert Farm.objects.count() == 0

    foreign_variant = MediaAsset.objects.create(
        file="farms/cover.webp",
        variants=[{"file": "products/cover-small.webp", "width": 50, "height": 50}],
        width=100,
        height=100,
        original_name="cover.webp",
    )
    response = farm_admin_api.post(
        "/api/v1/admin/farms",
        farm_payload(photo_ids=[str(foreign_variant.pk)]),
        format="json",
    )
    assert response.status_code == 400
    assert Farm.objects.count() == 0


@pytest.mark.django_db
def test_legacy_tracker_api_rejects_new_foreign_media_but_keeps_historical_links(farm_admin_api):
    foreign = MediaAsset.objects.create(
        file="products/historical.webp",
        variants=[],
        width=100,
        height=100,
        original_name="historical.webp",
    )
    created = farm_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(foreign.pk)]),
        format="json",
    )
    assert created.status_code == 400
    assert not Farm.objects.exists()
    assert not TrackerFarm.objects.exists()
    created = farm_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(image_id=str(foreign.pk)),
        format="json",
    )
    assert created.status_code == 400
    assert not Farm.objects.exists()

    farm = Farm.objects.create(**farm_payload(), image=foreign)
    FarmPhoto.objects.create(farm=farm, image=foreign, sort_order=0)
    profile = TrackerFarm.objects.create(id=farm.pk, farm=farm, **farm_payload(), image=foreign)
    TrackerFarmPhoto.objects.create(farm=profile, image=foreign, sort_order=0)
    farm_url = f"/api/v1/admin/farms/{farm.pk}"
    changed = farm_admin_api.patch(
        farm_url,
        {"version": farm.version, "name_en": "Renamed", "photo_ids": [str(foreign.pk)]},
        format="json",
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["photos"][0]["id"] == str(foreign.pk)

    legacy = farm_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{farm.pk}",
        {"version": profile.version + 1, "photo_ids": [str(foreign.pk)]},
        format="json",
    )
    assert legacy.status_code == 200, legacy.json()


@pytest.mark.django_db
def test_farm_api_handles_records_removed_after_preflight(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="farm-race", is_active=True)
    product = Product.objects.create(
        status="active",
        is_published=True,
        category=category,
        sku="FARM-RACE",
        slug="farm-race",
        unit_size="1.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=10000,
    )
    link = FarmProduct.objects.create(farm=farm, product=product)
    base = f"/api/v1/admin/farms/{farm.pk}"
    with patch("farms.views.AdminFarmBulkProductLinkView.write", side_effect=Farm.DoesNotExist):
        response = farm_admin_api.post(
            f"{base}/products/bulk",
            {"product_ids": [str(product.pk)], "idempotency_key": str(uuid4())},
            format="json",
        )
    assert response.status_code == 404
    with patch("farms.views.remove_farm_product", side_effect=FarmConflict("farm_product_changed")):
        response = farm_admin_api.post(
            f"{base}/products/{product.pk}/remove", lifecycle_command(link), format="json"
        )
    assert response.status_code == 409
    assert FarmProduct.objects.filter(pk=link.pk).exists()

    with patch("farms.views.update_farm", side_effect=Farm.DoesNotExist):
        response = farm_admin_api.patch(
            base, {"version": farm.version, "name_en": "Changed"}, format="json"
        )
    assert response.status_code == 404


@pytest.mark.django_db
def test_batch_write_returns_conflict_if_link_disappears_after_initial_lookup(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="farm-product-race", is_active=True)
    product = Product.objects.create(
        status="active",
        is_published=True,
        category=category,
        sku="RACE-500G",
        slug="race-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
    )
    relation = FarmProduct.objects.create(farm=farm, product=product)
    with patch("farms.views.create_stock_batch", side_effect=FarmConflict("farm_product_changed")):
        response = farm_admin_api.post(
            f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/batches",
            {
                **lifecycle_command(relation),
                "start_date": timezone.localdate().isoformat(),
                "end_date": timezone.localdate().isoformat(),
                "quantity_units": 10,
            },
            format="json",
        )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "farm_product_changed"


@pytest.mark.django_db
def test_farm_api_requires_admin(active_user, access_token):
    access_token.scope = "profile email"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert client.get("/api/v1/admin/farms").status_code == 403
    assert client.post("/api/v1/admin/farms", farm_payload(), format="json").status_code == 403
    farm = Farm.objects.create(**farm_payload())
    source = FarmProduct.objects.create(farm=farm, product=make_product())
    base = f"/api/v1/admin/farms/{farm.pk}/products/{source.product_id}"
    assert client.get(base).status_code == 403
    assert client.get(f"{base}/history").status_code == 403
    assert (
        client.post(f"{base}/remove", lifecycle_command(source), format="json").status_code == 403
    )
    assert (
        client.post(f"{base}/re-add", lifecycle_command(source), format="json").status_code == 403
    )


@pytest.mark.django_db
def test_tracker_uses_canonical_farm_and_preserves_public_payload(farm_admin_api):
    created = farm_admin_api.post(
        "/api/v1/admin/tracker-farms",
        {**farm_payload(), "signup_count": 14, "one_month_signup_count": 4},
        format="json",
    )
    assert created.status_code == 201, created.json()
    tracker = created.json()["data"]
    farm = Farm.objects.get(pk=tracker["id"])
    assert TrackerFarm.objects.get(pk=tracker["id"]).farm_id == farm.pk

    changed = farm_admin_api.patch(
        f"/api/v1/admin/farms/{farm.pk}",
        {"version": farm.version, "name_en": "Renamed at Farm"},
        format="json",
    )
    assert changed.status_code == 200, changed.json()

    public = APIClient().get("/api/v1/public/tracker-farms?locale=en")
    assert public.status_code == 200
    result = next(item for item in public.json()["data"] if item["id"] == str(farm.pk))
    assert result["name"] == "Renamed at Farm"
    assert result["signup_count"] == 14

    farm_admin_api.patch(
        f"/api/v1/admin/farms/{farm.pk}",
        {"version": changed.json()["data"]["version"], "is_active": False},
        format="json",
    )
    hidden = APIClient().get("/api/v1/public/tracker-farms?locale=en")
    assert all(item["id"] != str(farm.pk) for item in hidden.json()["data"])


@pytest.mark.django_db
def test_product_sale_unit_cannot_change_after_stock_batch_exists(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="vegetables-2", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="CARROT-1KG",
        slug="carrot-1kg",
        unit_size="1.000",
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=30000,
    )
    row = FarmProduct.objects.create(farm=farm, product=product)
    batch = FarmProductStockBatch.objects.create(
        farm_product=row,
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
        quantity_units=20,
    )
    detail = farm_admin_api.get(f"/api/v1/admin/store-products/{product.pk}").json()["data"]
    capabilities = detail["capabilities"]
    assert capabilities["edit_identity"] == {
        "allowed": False,
        "reasons": [
            {
                "code": "stock_batch",
                "id": str(batch.pk),
                "farm_id": str(farm.pk),
                "farm_name_vi": farm.name_vi,
                "farm_name_en": farm.name_en,
            }
        ],
    }
    assert capabilities["edit_csa"]["allowed"] is True
    assert capabilities["delete"]["allowed"] is False
    blocked_sku = farm_admin_api.patch(
        f"/api/v1/admin/store-products/{product.pk}", {"sku": "NEW-SKU"}, format="json"
    )
    assert blocked_sku.status_code == 409
    response = farm_admin_api.patch(
        f"/api/v1/admin/store-products/{product.pk}",
        {"unit_size": "2.000"},
        format="json",
    )
    assert response.status_code == 409
    product.refresh_from_db()
    assert product.unit_size == 1


@pytest.mark.django_db
def test_product_sale_unit_cannot_change_when_archived_weekly_history_exists(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="legacy-unit-category", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="LEGACY-UNIT",
        slug="legacy-unit",
        unit_size="1.000",
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=30_000,
    )
    relation = FarmProduct.objects.create(farm=farm, product=product)
    LegacyFarmWeeklySupply.objects.create(
        farm_product=relation,
        week_start=timezone.localdate(),
        quantity_units=0,
    )

    response = farm_admin_api.patch(
        f"/api/v1/admin/store-products/{product.pk}",
        {"unit_size": "2.000"},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "product_unit_in_use"


@pytest.mark.django_db
def test_tracker_profile_can_be_enabled_for_existing_farm_and_removed_independently(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    url = f"/api/v1/admin/farms/{farm.pk}/growth-tracker"
    enabled = farm_admin_api.post(
        url, {"signup_count": 12, "one_month_signup_count": 3}, format="json"
    )
    assert enabled.status_code == 201, enabled.json()
    assert enabled.json()["data"]["id"] == str(farm.pk)
    assert enabled.json()["data"]["signup_count"] == 12
    assert farm_admin_api.post(url, {}, format="json").status_code == 409

    public = APIClient().get("/api/v1/public/tracker-farms?locale=en")
    assert any(item["id"] == str(farm.pk) for item in public.json()["data"])
    removed = farm_admin_api.delete(
        url, {"version": enabled.json()["data"]["version"]}, format="json"
    )
    assert removed.status_code == 204
    assert Farm.objects.filter(pk=farm.pk).exists()
    assert not TrackerFarm.objects.filter(farm_id=farm.pk).exists()
    public = APIClient().get("/api/v1/public/tracker-farms?locale=en")
    assert all(item["id"] != str(farm.pk) for item in public.json()["data"])


@pytest.mark.django_db
def test_reenabled_tracker_profile_rejects_a_draft_from_its_previous_lifecycle(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    url = f"/api/v1/admin/farms/{farm.pk}/growth-tracker"
    first = farm_admin_api.post(url, {"signup_count": 12}, format="json")
    assert first.status_code == 201, first.json()
    old_version = first.json()["data"]["version"]
    assert farm_admin_api.delete(url, {"version": old_version}, format="json").status_code == 204

    enabled_again = farm_admin_api.post(url, {"signup_count": 99}, format="json")
    assert enabled_again.status_code == 201, enabled_again.json()
    assert enabled_again.json()["data"]["version"] > old_version
    stale = farm_admin_api.patch(url, {"version": old_version, "signup_count": 13}, format="json")
    assert stale.status_code == 409
    assert TrackerFarm.objects.get(farm_id=farm.pk).signup_count == 99


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("route", "method", "service_path", "delete_first"),
    [
        ("farms/{id}/growth-tracker", "patch", "farms.views.update_growth_tracker_profile", True),
        ("farms/{id}/growth-tracker", "patch", "farms.views.update_growth_tracker_profile", False),
        ("farms/{id}/growth-tracker", "delete", "farms.views.delete_growth_tracker_profile", True),
        ("tracker-farms/{id}", "patch", "content.views.update_tracker_farm", True),
        ("tracker-farms/{id}", "patch", "content.views.update_tracker_farm", False),
    ],
    ids=[
        "before-profile-patch",
        "after-profile-patch",
        "before-profile-delete",
        "before-legacy-patch",
        "after-legacy-patch",
    ],
)
def test_tracker_write_returns_not_found_when_farm_is_deleted_during_request(
    farm_admin_api, active_user, route, method, service_path, delete_first
):
    farm = Farm.objects.create(**farm_payload())
    profile = TrackerFarm.objects.create(pk=farm.pk, farm=farm)
    write = import_string(service_path)

    def overlap_farm_deletion(**kwargs):
        if delete_first:
            delete_farm(farm_id=farm.pk, actor=active_user)
            return write(**kwargs)
        result = write(**kwargs)
        delete_farm(farm_id=farm.pk, actor=active_user)
        return result

    url = f"/api/v1/admin/{route.format(id=farm.pk)}"
    farm_admin_api.raise_request_exception = False
    with patch(service_path, side_effect=overlap_farm_deletion):
        if method == "patch":
            response = farm_admin_api.patch(
                url, {"version": profile.version, "signup_count": 3}, format="json"
            )
        else:
            response = farm_admin_api.delete(url, {"version": profile.version}, format="json")

    assert response.status_code == 404
    assert not Farm.objects.filter(pk=farm.pk).exists()
    assert not TrackerFarm.objects.filter(pk=profile.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("legacy", [False, True], ids=["profile-create", "legacy-create"])
def test_tracker_create_returns_not_found_when_farm_and_image_are_deleted_before_response(
    farm_admin_api, active_user, legacy
):
    asset = MediaAsset.objects.create(file="farms/private.webp", width=100, height=100)
    if legacy:
        url = "/api/v1/admin/tracker-farms"
        data = farm_payload(photo_ids=[str(asset.pk)])
        service_path = "content.views.create_tracker_farm"
    else:
        farm = Farm.objects.create(**farm_payload(), image=asset)
        url = f"/api/v1/admin/farms/{farm.pk}/growth-tracker"
        data = {}
        service_path = "farms.views.create_growth_tracker_profile"
    create_profile = import_string(service_path)

    def delete_before_response(**kwargs):
        profile = create_profile(**kwargs)
        delete_farm(farm_id=profile.farm_id, actor=active_user)
        return profile

    farm_admin_api.raise_request_exception = False
    with patch(service_path, side_effect=delete_before_response):
        response = farm_admin_api.post(url, data, format="json")

    assert response.status_code == 404
    assert not Farm.objects.exists()
    assert not TrackerFarm.objects.exists()
    assert not MediaAsset.objects.filter(pk=asset.pk).exists()


@pytest.mark.django_db
def test_legacy_tracker_delete_preserves_independently_created_farm(farm_admin_api):
    image = MediaAsset.objects.create(
        file="farms/independent.webp",
        variants=[],
        width=100,
        height=100,
        original_name="independent.webp",
    )
    created = farm_admin_api.post(
        "/api/v1/admin/farms", farm_payload(photo_ids=[str(image.pk)]), format="json"
    )
    farm_id = created.json()["data"]["id"]
    enabled = farm_admin_api.post(
        f"/api/v1/admin/farms/{farm_id}/growth-tracker", {}, format="json"
    )
    assert enabled.status_code == 201, enabled.json()
    old_version = enabled.json()["data"]["version"]

    removed = farm_admin_api.delete(
        f"/api/v1/admin/tracker-farms/{farm_id}", {"version": old_version}, format="json"
    )
    assert removed.status_code == 204, removed.json() if removed.content else None
    assert Farm.objects.filter(pk=farm_id).exists()
    assert not TrackerFarm.objects.filter(farm_id=farm_id).exists()
    assert Farm.objects.get(pk=farm_id).photos.filter(image_id=image.pk).exists()
    assert MediaAsset.objects.filter(pk=image.pk).exists()
    enabled_again = farm_admin_api.post(
        f"/api/v1/admin/farms/{farm_id}/growth-tracker", {}, format="json"
    )
    assert enabled_again.status_code == 201, enabled_again.json()
    assert enabled_again.json()["data"]["version"] > old_version


@pytest.mark.django_db
def test_deleting_unknown_farm_media_returns_not_found(farm_admin_api):
    from uuid import uuid4

    response = farm_admin_api.delete(f"/api/v1/admin/farms/media/{uuid4()}")
    assert response.status_code == 404


@pytest.mark.django_db
def test_new_farm_admin_edit_promotes_legacy_tracker_farm_to_independent(farm_admin_api):
    created = farm_admin_api.post(
        "/api/v1/admin/tracker-farms", {**farm_payload(), "signup_count": 2}, format="json"
    )
    assert created.status_code == 201, created.json()
    farm_id = created.json()["data"]["id"]
    farm = Farm.objects.get(pk=farm_id)
    assert farm.legacy_tracker_owned is True

    updated = farm_admin_api.patch(
        f"/api/v1/admin/farms/{farm_id}",
        {"version": farm.version, "name_en": "Promoted Farm"},
        format="json",
    )
    assert updated.status_code == 200, updated.json()
    profile = TrackerFarm.objects.get(farm_id=farm_id)
    removed = farm_admin_api.delete(
        f"/api/v1/admin/tracker-farms/{farm_id}", {"version": profile.version}, format="json"
    )
    assert removed.status_code == 204
    assert Farm.objects.filter(pk=farm_id, name_en="Promoted Farm").exists()


@pytest.mark.django_db
def test_farm_gallery_is_shared_with_growth_tracker_and_stale_tracker_form_conflicts(
    farm_admin_api,
):
    image = MediaAsset.objects.create(
        file="farms/photo.webp", variants=[], width=300, height=200, original_name="photo.webp"
    )
    created = farm_admin_api.post(
        "/api/v1/admin/farms", farm_payload(photo_ids=[str(image.pk)]), format="json"
    )
    assert created.status_code == 201, created.json()
    farm_data = created.json()["data"]
    assert [item["id"] for item in farm_data["photos"]] == [str(image.pk)]
    enabled = farm_admin_api.post(
        f"/api/v1/admin/farms/{farm_data['id']}/growth-tracker", {}, format="json"
    )
    assert enabled.status_code == 201, enabled.json()
    old_version = enabled.json()["data"]["version"]
    public = APIClient().get("/api/v1/public/tracker-farms?locale=en")
    assert public.json()["data"][0]["images"][0]["id"] == str(image.pk)

    changed = farm_admin_api.patch(
        f"/api/v1/admin/farms/{farm_data['id']}",
        {"version": farm_data["version"], "name_en": "Updated Farm"},
        format="json",
    )
    assert changed.status_code == 200, changed.json()
    stale = farm_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{farm_data['id']}",
        {"version": old_version, "name_en": "Stale name"},
        format="json",
    )
    assert stale.status_code == 409
    public = APIClient().get("/api/v1/public/tracker-farms?locale=en")
    assert public.json()["data"][0]["name"] == "Updated Farm"


@pytest.mark.django_db
def test_stock_batch_list_returns_batch_history(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="vegetables-3", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="KALE-500G",
        slug="kale-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
    )
    row = FarmProduct.objects.create(farm=farm, product=product)
    batch = FarmProductStockBatch.objects.create(
        farm_product=row,
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
        quantity_units=8,
    )
    url = f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/batches"
    good = farm_admin_api.get(url)
    assert good.status_code == 200, good.json()
    assert good.json()["data"][0]["id"] == str(batch.pk)
    assert good.json()["data"][0]["quantity_units"] == 8


@pytest.mark.django_db
def test_tracker_count_only_update_does_not_change_shared_farm_version(farm_admin_api):
    created = farm_admin_api.post(
        "/api/v1/admin/tracker-farms", {**farm_payload(), "signup_count": 4}, format="json"
    )
    assert created.status_code == 201, created.json()
    tracker = created.json()["data"]
    before = Farm.objects.get(pk=tracker["id"]).version
    updated = farm_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{tracker['id']}",
        {"version": tracker["version"], "signup_count": 5},
        format="json",
    )
    assert updated.status_code == 200, updated.json()
    assert Farm.objects.get(pk=tracker["id"]).version == before


@pytest.mark.django_db
def test_legacy_link_writes_are_blocked_but_a_removed_unused_source_can_be_deleted(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="root-4", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="CUCUMBER-1",
        slug="cucumber-1",
        unit_size="1.000",
        unit_label_vi="quả",
        unit_label_en="piece",
        retail_price_vnd=10000,
    )
    relation = FarmProduct.objects.create(farm=farm, product=product)
    farm_url = f"/api/v1/admin/farms/{farm.pk}"
    link_url = f"{farm_url}/products/{product.pk}"
    assert farm_admin_api.delete(farm_url).status_code == 409
    for response in (
        farm_admin_api.patch(link_url, {"is_active": False}, format="json"),
        farm_admin_api.delete(link_url),
    ):
        assert response.status_code == 409
        assert response.json()["errors"][0]["code"] == "farm_product_legacy_write_disabled"
    relation.refresh_from_db()
    assert relation.removed_at is None
    assert (
        farm_admin_api.post(
            f"{link_url}/remove", lifecycle_command(relation), format="json"
        ).status_code
        == 200
    )
    assert FarmProduct.objects.get(pk=relation.pk).removed_at is not None
    batch = FarmProductStockBatch.objects.create(
        farm_product=relation,
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
        quantity_units=10,
    )
    assert farm_admin_api.get(farm_url).json()["data"]["capabilities"]["delete"]["allowed"]
    assert farm_admin_api.delete(farm_url).status_code == 204
    assert not FarmProductStockBatch.objects.filter(pk=batch.pk).exists()
    assert not FarmProduct.objects.filter(pk=relation.pk).exists()
    assert Product.objects.filter(pk=product.pk).exists()
    unreferenced = Farm.objects.create(**farm_payload(name_en="Second Farm"))
    assert farm_admin_api.delete(f"/api/v1/admin/farms/{unreferenced.pk}").status_code == 204


@pytest.mark.django_db
def test_archived_zero_weekly_history_survives_soft_removal(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="legacy-link-category", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="LEGACY-LINK",
        slug="legacy-link",
        unit_size="1.000",
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=10_000,
    )
    relation = FarmProduct.objects.create(farm=farm, product=product)
    archived = LegacyFarmWeeklySupply.objects.create(
        farm_product=relation,
        week_start=timezone.localdate(),
        quantity_units=0,
    )

    listed = farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}/products")
    assert listed.status_code == 200
    assert listed.json()["data"][0]["has_supply_history"] is True

    response = farm_admin_api.post(
        f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/remove",
        lifecycle_command(relation),
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["data"]["has_supply_history"] is True
    assert LegacyFarmWeeklySupply.objects.filter(pk=archived.pk, farm_product=relation).exists()
    blocked = farm_admin_api.delete(f"/api/v1/admin/farms/{farm.pk}")
    assert blocked.status_code == 409
    assert blocked.json()["errors"][0]["context"]["reasons"][0]["code"] == "legacy_supply"
    assert LegacyFarmWeeklySupply.objects.filter(pk=archived.pk).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("visible,count", [(True, 0), (True, 7), (False, 7)])
def test_delete_farm_cascades_tracker_and_cleans_only_orphaned_media(
    farm_admin_api, django_capture_on_commit_callbacks, visible, count
):
    shared = MediaAsset.objects.create(file=f"farms/{uuid4()}.webp", width=100, height=100)
    private = MediaAsset.objects.create(file=f"farms/{uuid4()}.webp", width=100, height=100)
    farm = Farm.objects.create(**farm_payload(), image=shared)
    other = Farm.objects.create(**farm_payload(), image=shared)
    FarmPhoto.objects.create(farm=farm, image=shared, sort_order=0)
    tracker = TrackerFarm.objects.create(
        farm=farm, image=private, is_visible=visible, signup_count=count
    )
    TrackerFarmPhoto.objects.create(farm=tracker, image=private, sort_order=0)
    url = f"/api/v1/admin/farms/{farm.pk}"

    assert farm_admin_api.get(url).json()["data"]["capabilities"]["delete"]["allowed"]
    with (
        patch("farms.services.request_frontend_revalidation") as revalidate,
        django_capture_on_commit_callbacks(execute=True),
    ):
        response = farm_admin_api.delete(url)
        assert response.status_code == 204
        assert response["Cache-Control"] == "private, no-store"
        revalidate.assert_not_called()

    revalidate.assert_called_once_with(["tracker-farms"])
    assert farm_admin_api.get(url).status_code == 404
    assert not TrackerFarm.objects.filter(pk=tracker.pk).exists()
    assert not TrackerFarmPhoto.objects.filter(farm=tracker).exists()
    assert not FarmPhoto.objects.filter(farm=farm).exists()
    assert not MediaAsset.objects.filter(pk=private.pk).exists()
    assert Farm.objects.filter(pk=other.pk, image=shared).exists()
    assert ContentAuditLog.objects.filter(action="farm.delete", entity_id=str(farm.pk)).exists()
    assert ContentAuditLog.objects.filter(
        action="tracker_farm.delete", entity_id=str(tracker.pk)
    ).exists()


def allocate_historical_farm_batch(source, status, *, restocked=False):
    today = timezone.localdate()
    batch = FarmProductStockBatch.objects.create(
        farm_product=source,
        supply_generation=1,
        start_date=today - timedelta(days=14),
        end_date=today - timedelta(days=7),
        quantity_units=10,
    )
    order = StoreOrder.objects.create(
        reference_code="NFV-000123",
        create_idempotency_key=uuid4(),
        locale="vi",
        status=status,
        subtotal_vnd=10000,
        shipping_fee_vnd=0,
        total_vnd=10000,
    )
    line = StoreOrderLine.objects.create(
        order=order,
        product_id=source.product_id,
        position=1,
        sku_snapshot="SKU",
        name_snapshot="Vegetables",
        unit_size_snapshot=1,
        unit_label_snapshot="kg",
        unit_price_vnd_snapshot=10000,
        quantity=1,
        cash_quantity=1,
        cash_subtotal_vnd=10000,
    )
    allocation = StoreStockAllocation.objects.create(line=line, batch=batch, quantity=1)
    for kind in ["sale", "restock"] if restocked else ["sale"]:
        StoreStockMovement.objects.create(
            allocation=allocation,
            line=line,
            batch=batch,
            quantity=1,
            kind=kind,
            idempotency_key=uuid4(),
        )
    return order, allocation


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status,restocked",
    [
        (StoreOrderStatus.DELIVERED, False),
        (StoreOrderStatus.CANCELLED, False),
        (StoreOrderStatus.CANCELLED, True),
    ],
)
def test_farm_delete_preserves_allocation_history_across_generations(
    farm_admin_api, status, restocked
):
    farm = Farm.objects.create(**farm_payload(), legacy_tracker_owned=True)
    tracker = TrackerFarm.objects.create(farm=farm)
    product = make_product()
    source = FarmProduct.objects.create(farm=farm, product=product, supply_generation=2)
    order, allocation = allocate_historical_farm_batch(source, status, restocked=restocked)
    url = f"/api/v1/admin/farms/{farm.pk}"
    capability = farm_admin_api.get(url).json()["data"]["capabilities"]["delete"]
    assert capability["allowed"] is False
    assert capability["reasons"] == [
        {
            "code": "supplying_product",
            "id": str(product.pk),
            "label": product.sku,
            "name_vi": "Cà chua",
            "name_en": "Cà chua",
        },
        {"code": "store_order", "id": str(order.pk), "label": order.reference_code},
    ]
    assert farm_admin_api.delete(url).json()["errors"] == [
        {"code": "farm_in_use", "context": {"reasons": capability["reasons"]}},
    ]
    removed = farm_admin_api.post(
        f"{url}/products/{product.pk}/remove", lifecycle_command(source), format="json"
    )
    assert removed.status_code == 200
    assert farm_admin_api.get(url).json()["data"]["capabilities"]["delete"] == {
        "allowed": False,
        "reasons": capability["reasons"][1:],
    }
    assert farm_admin_api.delete(url).status_code == 409
    assert Farm.objects.filter(pk=farm.pk).exists()
    assert TrackerFarm.objects.filter(pk=tracker.pk).exists()
    assert StoreStockAllocation.objects.filter(pk=allocation.pk).exists()
    assert allocation.movements.count() == (2 if restocked else 1)
    assert not ContentAuditLog.objects.filter(action="farm.delete", entity_id=str(farm.pk)).exists()

    removed = farm_admin_api.delete(
        f"/api/v1/admin/tracker-farms/{tracker.pk}",
        {"version": tracker.version},
        format="json",
    )
    assert removed.status_code == 204
    assert not TrackerFarm.objects.filter(pk=tracker.pk).exists()
    assert Farm.objects.filter(pk=farm.pk).exists()
    assert StoreStockAllocation.objects.filter(pk=allocation.pk).exists()


@pytest.mark.django_db
def test_delete_recognizes_stock_movement_attached_to_a_batch_even_without_its_own_allocation(
    farm_admin_api,
):
    farm = Farm.objects.create(**farm_payload())
    other = Farm.objects.create(**farm_payload(name_en="Other"))
    product = make_product()
    source = FarmProduct.objects.create(
        farm=farm, product=product, removed_at=timezone.now(), is_active=False
    )
    batch = FarmProductStockBatch.objects.create(
        farm_product=source,
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
        quantity_units=2,
    )
    order, allocation = allocate_historical_farm_batch(
        FarmProduct.objects.create(farm=other, product=product), StoreOrderStatus.CANCELLED
    )
    # Historical ledger inconsistency: the ledger claims this batch, while the
    # allocation points elsewhere. Protect both batch references.
    movement = allocation.movements.get()
    movement.batch = batch
    movement.save(update_fields=["batch"])
    url = f"/api/v1/admin/farms/{farm.pk}"
    response = farm_admin_api.delete(url)
    assert response.status_code == 409
    assert response.json()["errors"][0]["context"]["reasons"] == [
        {"code": "store_order", "id": str(order.pk), "label": order.reference_code},
    ]
    assert FarmProductStockBatch.objects.filter(pk=batch.pk).exists()
    assert StoreStockMovement.objects.filter(pk=movement.pk, batch=batch).exists()


@pytest.mark.django_db
def test_delete_rolls_back_tracker_batches_sources_and_audit_on_unexpected_protection(
    farm_admin_api,
):
    farm = Farm.objects.create(**farm_payload())
    tracker = TrackerFarm.objects.create(farm=farm)
    source = FarmProduct.objects.create(
        farm=farm, product=make_product(), removed_at=timezone.now(), is_active=False
    )
    batch = FarmProductStockBatch.objects.create(
        farm_product=source,
        start_date=timezone.localdate(),
        end_date=timezone.localdate(),
        quantity_units=2,
    )
    url = f"/api/v1/admin/farms/{farm.pk}"
    with patch("farms.models.Farm.delete", side_effect=ProtectedError("dependency", {farm})):
        response = farm_admin_api.delete(url)
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "farm_delete_protected"}]
    assert Farm.objects.filter(pk=farm.pk).exists()
    assert TrackerFarm.objects.filter(pk=tracker.pk).exists()
    assert FarmProduct.objects.filter(pk=source.pk).exists()
    assert FarmProductStockBatch.objects.filter(pk=batch.pk).exists()
    assert not ContentAuditLog.objects.filter(
        action__in=["farm.delete", "tracker_farm.delete"],
        entity_id__in=[str(farm.pk), str(tracker.pk)],
    ).exists()


@pytest.mark.django_db
def test_delete_rolls_back_earlier_asset_cleanup_and_on_commit_work_on_unexpected_restriction(
    farm_admin_api, django_capture_on_commit_callbacks, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    assets = [
        MediaAsset.objects.create(
            pk=asset_id,
            file=default_storage.save(f"farms/{asset_id}.webp", ContentFile(b"image")),
            width=100,
            height=100,
        )
        for asset_id in sorted([uuid4(), uuid4()])
    ]
    farm = Farm.objects.create(**farm_payload(), image=assets[0])
    FarmPhoto.objects.create(farm=farm, image=assets[1], sort_order=0)
    TrackerFarm.objects.create(farm=farm)
    delete_asset = MediaAsset.delete

    def restrict_second_asset(asset):
        if asset.pk == assets[1].pk:
            assert not MediaAsset.objects.filter(pk=assets[0].pk).exists()
            assert default_storage.exists(assets[0].file.name)
            raise RestrictedError("dependency", {asset})
        return delete_asset(asset)

    with (
        patch.object(MediaAsset, "delete", new=restrict_second_asset),
        django_capture_on_commit_callbacks(execute=True) as callbacks,
    ):
        response = farm_admin_api.delete(f"/api/v1/admin/farms/{farm.pk}")

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "farm_delete_protected"}]
    assert not callbacks
    assert Farm.objects.filter(pk=farm.pk, image=assets[0]).exists()
    assert FarmPhoto.objects.filter(farm=farm, image=assets[1]).exists()
    assert TrackerFarm.objects.filter(farm=farm).exists()
    for asset in assets:
        assert MediaAsset.objects.filter(pk=asset.pk).exists()
        assert default_storage.exists(asset.file.name)


@pytest.mark.django_db
def test_farm_delete_capability_bounds_detail_queries_and_references(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    url = f"/api/v1/admin/farms/{farm.pk}"
    FarmProduct.objects.create(farm=farm, product=make_product("FIRST"))
    with CaptureQueriesContext(connection) as first:
        assert len(farm_admin_api.get(url).json()["data"]["capabilities"]["delete"]["reasons"]) == 1
    for index in range(12):
        FarmProduct.objects.create(farm=farm, product=make_product(f"SKU-{index}"))
    with CaptureQueriesContext(connection) as many:
        assert len(farm_admin_api.get(url).json()["data"]["capabilities"]["delete"]["reasons"]) == 1
    assert len(many) == len(first)
    # Each capability query projects at most one representative reference.
    for table in (
        "farms_farmproduct",
        "commerce_storestockallocation",
        "commerce_storestockmovement",
    ):
        assert any(table in row["sql"] and "LIMIT 1" in row["sql"] for row in many)


def lifecycle_command(source, **values):
    payload = {
        "expected_link_id": str(source.pk),
        "expected_version": source.version,
    }
    return {**payload, **values}


@pytest.mark.django_db
def test_source_transitions_return_committed_identity_and_fresh_stock(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    product = make_product()
    url = f"/api/v1/admin/farms/{farm.pk}/products"
    detail = f"{url}/{product.pk}"
    today = timezone.localdate()
    dates = {"start_date": today.isoformat(), "end_date": (today + timedelta(days=3)).isoformat()}

    added = farm_admin_api.post(
        f"{url}/bulk",
        {"product_ids": [str(product.pk)], "idempotency_key": str(uuid4())},
        format="json",
    )
    assert added.status_code == 201
    source = FarmProduct.objects.get(farm=farm, product=product)
    assert not source.stock_batches.exists()
    supplied = farm_admin_api.post(
        f"{detail}/batches", lifecycle_command(source, **dates, quantity_units=10), format="json"
    )
    assert supplied.status_code == 201, supplied.json()
    first = source.stock_batches.get()
    source.refresh_from_db()
    live = farm_admin_api.get(detail).json()["data"]
    assert live["capabilities"] == {
        "can_remove": True,
        "can_readd": False,
        "can_create_batch": True,
        "blockers": {"readd": {"code": "farm_product_not_removed"}},
    }

    removed = farm_admin_api.post(f"{detail}/remove", lifecycle_command(source), format="json")
    assert removed.status_code == 200
    data = removed.json()["data"]
    assert (data["id"], data["supply_status"], data["version"], data["supply_generation"]) == (
        str(source.pk),
        "removed",
        3,
        1,
    )
    assert data["available_quantity"] == 0
    assert data["removed_at"] is not None
    assert farm_admin_api.get(f"{detail}/batches?scope=current").json()["data"] == []
    removed_history = farm_admin_api.get(f"{detail}/batches?scope=history").json()["data"]
    assert [(row["id"], row["is_historical"]) for row in removed_history] == [(str(first.pk), True)]
    assert (
        farm_admin_api.post(
            f"{detail}/re-add",
            lifecycle_command(source, idempotency_key=str(uuid4())),
            format="json",
        ).json()["errors"][0]["code"]
        == "farm_product_version_conflict"
    )
    source.refresh_from_db()
    removed_detail = farm_admin_api.get(detail).json()["data"]
    assert removed_detail["capabilities"]["can_readd"] is True
    assert removed_detail["capabilities"]["can_create_batch"] is False

    readded = farm_admin_api.post(
        f"{detail}/re-add",
        lifecycle_command(source, idempotency_key=str(uuid4())),
        format="json",
    )
    assert readded.status_code == 200
    data = readded.json()["data"]
    assert (data["id"], data["version"], data["supply_generation"]) == (str(source.pk), 4, 2)
    assert "first_batch_id" not in data
    assert data["removed_at"] is None
    assert data["available_quantity"] == 0
    assert not source.stock_batches.exclude(pk=first.pk).exists()
    assert farm_admin_api.get(f"{detail}/batches?scope=current").json()["data"] == []
    source.refresh_from_db()
    supplied = farm_admin_api.post(
        f"{detail}/batches", lifecycle_command(source, **dates, quantity_units=4), format="json"
    )
    assert supplied.status_code == 201, supplied.json()
    second = source.stock_batches.exclude(pk=first.pk).get()

    history = farm_admin_api.get(f"{detail}/batches?scope=history&status=active").json()["data"]
    assert [
        (row["id"], row["available_quantity"], row["remaining_quantity"]) for row in history
    ] == [(str(first.pk), 0, 10)]
    assert history[0]["is_historical"] is True
    assert history[0]["supply_generation"] == 1
    current = farm_admin_api.get(f"{detail}/batches?scope=current").json()["data"]
    assert [
        (row["id"], row["available_quantity"], row["remaining_quantity"]) for row in current
    ] == [(str(second.pk), 4, 4)]
    assert current[0]["is_historical"] is False
    assert current[0]["supply_generation"] == 2
    stale_batch = farm_admin_api.patch(
        f"{detail}/batches/{first.pk}",
        {"version": first.version, "quantity_units": 11},
        format="json",
    )
    assert stale_batch.status_code == 409
    assert stale_batch.json()["errors"][0]["code"] == "farm_product_generation_changed"
    assert source.stock_batches.get(pk=first.pk).quantity_units == 10


@pytest.mark.django_db
def test_source_filters_and_picker_distinguish_removed_and_unlinked(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    supplying = make_product("SUPPLYING")
    removed = make_product("REMOVED")
    available = make_product("AVAILABLE")
    make_product("DRAFT", published=False)
    FarmProduct.objects.create(farm=farm, product=supplying)
    FarmProduct.objects.create(
        farm=farm, product=removed, is_active=False, removed_at=timezone.now()
    )
    base = f"/api/v1/admin/farms/{farm.pk}"
    assert farm_admin_api.get(f"{base}/products?supply_status=invalid").status_code == 400
    for status, skus in (
        ("all", ["REMOVED", "SUPPLYING"]),
        ("removed", ["REMOVED"]),
        ("supplying", ["SUPPLYING"]),
    ):
        response = farm_admin_api.get(f"{base}/products?supply_status={status}")
        assert response.status_code == 200
        rows = response.json()["data"]
        assert [row["sku"] for row in rows] == skus
        assert all("capabilities" not in row for row in rows)

    picker = farm_admin_api.get(f"{base}/available-products").json()["data"]
    assert [row["id"] for row in picker] == [str(available.pk)]
    assert (picker[0]["unit_size"], picker[0]["unit_label_vi"], picker[0]["unit_label_en"]) == (
        "0.500",
        "kg",
        "kg",
    )


@pytest.mark.django_db
def test_source_writers_reject_unversioned_malformed_and_changed_identity(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    product = make_product()
    other = Farm.objects.create(**farm_payload(name_en="Other farm"))
    source = FarmProduct.objects.create(farm=farm, product=product)
    other_source = FarmProduct.objects.create(farm=other, product=product)
    base = f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}"
    date = timezone.localdate().isoformat()
    batch = {"start_date": date, "end_date": date, "quantity_units": 5}

    for path, values in (("remove", {}), ("re-add", batch), ("batches", batch)):
        assert farm_admin_api.post(f"{base}/{path}", values, format="json").status_code == 400
    for values in (
        lifecycle_command(source, expected_link_id="not-a-uuid"),
        lifecycle_command(source, expected_version=0),
        lifecycle_command(source, supply_generation=2),
    ):
        assert farm_admin_api.post(f"{base}/remove", values, format="json").status_code == 400

    mismatch = farm_admin_api.post(f"{base}/remove", lifecycle_command(other_source), format="json")
    assert mismatch.status_code == 409
    assert mismatch.json()["errors"][0]["code"] == "farm_product_identity_changed"
    assert (
        farm_admin_api.get(f"/api/v1/admin/farms/{farm.pk}/products/{uuid4()}/history").status_code
        == 404
    )
    assert source.stock_batches.count() == 0
    source.refresh_from_db()
    assert source.removed_at is None

    created = farm_admin_api.post(
        f"{base}/batches", lifecycle_command(source, **batch), format="json"
    )
    assert created.status_code == 201
    assert created.json()["data"]["source_version"] == 2
    stale = farm_admin_api.post(
        f"{base}/batches", lifecycle_command(source, **batch), format="json"
    )
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "farm_product_version_conflict"
    assert source.stock_batches.count() == 1


@pytest.mark.django_db
def test_lifecycle_history_is_scoped_and_stably_paginated(farm_admin_api, active_user):
    farm = Farm.objects.create(**farm_payload())
    product = make_product("FIRST")
    other = make_product("OTHER")
    source = FarmProduct.objects.create(farm=farm, product=product)
    second = FarmProduct.objects.create(farm=farm, product=other)
    events = ContentAuditLog.objects.bulk_create(
        [
            ContentAuditLog(
                action="farm.product.lifecycle_backfill" if index == 0 else "farm.product.update",
                actor=active_user if index else None,
                entity_type="farmproduct",
                entity_id=str(source.pk),
                context={
                    "farm_id": str(farm.pk),
                    "product_id": str(product.pk),
                    "provenance": "legacy_disabled_link",
                    "classified_at": "2026-10-14T01:00:00+00:00",
                    "supply_generation": 1,
                }
                if index == 0
                else {
                    "is_active": True,
                    "stock_batch_id": "invalid-id",
                    "supply_generation": True,
                    "email": "private@example.invalid",
                },
            )
            for index in range(21)
        ]
    )
    ContentAuditLog.objects.filter(pk__in=[event.pk for event in events]).update(
        created_at=timezone.now()
    )
    ContentAuditLog.objects.create(
        action="farm.product.add", entity_type="farmproduct", entity_id=str(second.pk)
    )
    ContentAuditLog.objects.create(
        action="farm.update", entity_type="farm", entity_id=str(source.pk)
    )

    url = f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/history"
    first = farm_admin_api.get(url)
    assert first.status_code == 200
    assert first.json()["meta"] == {"page": 1, "page_size": 20, "total": 21}
    page2 = farm_admin_api.get(f"{url}?page=2").json()["data"]
    rows = first.json()["data"] + page2
    assert [row["id"] for row in rows] == sorted(str(event.pk) for event in events)
    assert {row["action"] for row in rows} == {
        "farm.product.lifecycle_backfill",
        "farm.product.update",
    }
    migrated = next(row for row in rows if row["action"] == "farm.product.lifecycle_backfill")
    assert migrated["context"] == {
        "farm_id": str(farm.pk),
        "product_id": str(product.pk),
        "provenance": "legacy_disabled_link",
        "classified_at": "2026-10-14T01:00:00+00:00",
        "supply_generation": 1,
    }
    assert migrated["actor"] is None
    legacy = next(row for row in rows if row["action"] == "farm.product.update")
    assert legacy["context"] == {"is_active": True}
    assert legacy["actor"] == {"id": str(active_user.pk), "name": active_user.name}


@pytest.mark.django_db
def test_source_removal_returns_the_same_blocker_as_detail(
    farm_admin_api, store_product, checkout_operation, active_user
):
    source = store_product.farm_offers.get()
    base = f"/api/v1/admin/farms/{source.farm_id}/products/{store_product.pk}"
    order, _, created = checkout_operation()
    assert created

    def remove_blocker():
        response = farm_admin_api.get(base)
        assert response.status_code == 200
        capabilities = response.json()["data"]["capabilities"]
        assert capabilities["can_remove"] is False
        assert capabilities["can_create_batch"] is True
        rejected = farm_admin_api.post(f"{base}/remove", lifecycle_command(source), format="json")
        assert rejected.status_code == 409
        blocker = rejected.json()["errors"][0]
        assert blocker == capabilities["blockers"]["remove"]
        return blocker

    assert remove_blocker() == {
        "code": "farm_product_supply_shortfall",
        "context": {"required_units": 2, "remaining_units": 0, "shortfall_units": 2},
    }
    approve_review_order({"id": order.pk}, active_user)
    assert remove_blocker() == {
        "code": "farm_product_has_open_allocations",
        "context": {
            "orders": [{"id": str(order.pk), "reference_code": order.reference_code}],
            "has_more": False,
        },
    }
    source.refresh_from_db()
    assert (source.removed_at, source.version) == (None, 1)


@pytest.mark.django_db
def test_supply_list_does_not_evaluate_capability_queries_per_row(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    other = Farm.objects.create(**farm_payload(name_en="Other farm"))
    for index in range(20):
        product = make_product(f"SUPPLY-{index:02d}")
        FarmProduct.objects.create(farm=farm, product=product)
        if index == 0:
            FarmProduct.objects.create(farm=other, product=product)

    def selects(farm_id, expected_rows):
        with CaptureQueriesContext(connection) as queries:
            response = farm_admin_api.get(
                f"/api/v1/admin/farms/{farm_id}/products?supply_status=supplying"
            )
        assert response.status_code == 200
        assert len(response.json()["data"]) == expected_rows
        return sum(query["sql"].lstrip().upper().startswith("SELECT") for query in queries)

    assert selects(farm.pk, 20) == selects(other.pk, 1)


@pytest.mark.django_db
@pytest.mark.parametrize("legacy", [False, True])
def test_tracker_delete_requires_current_profile_version(farm_admin_api, legacy):
    farm = Farm.objects.create(**farm_payload())
    url = f"/api/v1/admin/farms/{farm.pk}/growth-tracker"
    delete_url = f"/api/v1/admin/tracker-farms/{farm.pk}" if legacy else url
    original = farm_admin_api.post(url, {"is_visible": True}, format="json").json()["data"]
    assert farm_admin_api.delete(delete_url, {}, format="json").status_code == 400
    updated = farm_admin_api.patch(
        url, {"version": original["version"], "is_visible": False}, format="json"
    ).json()["data"]
    stale = farm_admin_api.delete(delete_url, {"version": original["version"]}, format="json")
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "version_conflict"
    assert TrackerFarm.objects.get(farm_id=farm.pk).is_visible is False
    assert (
        farm_admin_api.delete(
            delete_url, {"version": updated["version"]}, format="json"
        ).status_code
        == 204
    )
    assert Farm.objects.filter(pk=farm.pk).exists()
