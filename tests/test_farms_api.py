from datetime import date
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from commerce.models import Category, Product, ProductTranslation
from content.models import MediaAsset, TrackerFarm, TrackerFarmPhoto
from farms.models import Farm, FarmPhoto, FarmProduct, FarmWeeklySupply
from farms.services import FarmConflict


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

    changed = farm_admin_api.patch(
        f"/api/v1/admin/farms/{data['id']}",
        {"version": data["version"], "name_vi": "Trang trại Xanh"},
        format="json",
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["name_vi"] == "Trang trại Xanh"
    assert changed.json()["data"]["version"] == data["version"] + 1

    stale = farm_admin_api.patch(
        f"/api/v1/admin/farms/{data['id']}",
        {"version": data["version"], "name_vi": "Ghi đè"},
        format="json",
    )
    assert stale.status_code == 409


@pytest.mark.django_db
def test_farm_product_and_weekly_supply_are_scoped_to_farm(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    other = Farm.objects.create(**farm_payload(name_en="Other Farm"))
    category = Category.objects.create(slug="greens", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="GREEN-500G",
        slug="green-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
    )
    url = f"/api/v1/admin/farms/{farm.pk}/products"
    linked = farm_admin_api.post(url, {"product_id": str(product.pk)}, format="json")
    assert linked.status_code == 201, linked.json()
    assert FarmProduct.objects.filter(farm=farm, product=product).exists()
    assert (
        farm_admin_api.post(url, {"product_id": str(product.pk)}, format="json").status_code == 409
    )

    week = date(2026, 10, 19)
    supply_url = f"{url}/{product.pk}/weeks/{week.isoformat()}"
    supplied = farm_admin_api.put(supply_url, {"quantity_units": 10}, format="json")
    assert supplied.status_code == 200, supplied.json()
    assert supplied.json()["data"]["quantity_units"] == 10
    assert (
        FarmWeeklySupply.objects.get(farm_product__farm=farm, week_start=week).quantity_units == 10
    )
    assert (
        farm_admin_api.put(
            f"/api/v1/admin/farms/{other.pk}/products/{product.pk}/weeks/{week.isoformat()}",
            {"quantity_units": 10},
            format="json",
        ).status_code
        == 404
    )
    assert farm_admin_api.put(supply_url, {"quantity_units": -1}, format="json").status_code == 400
    assert (
        farm_admin_api.put(
            f"{url}/{product.pk}/weeks/20261019", {"quantity_units": 5}, format="json"
        ).status_code
        == 400
    )
    assert (
        farm_admin_api.put(
            f"{url}/{product.pk}/weeks/2026-10-20", {"quantity_units": 5}, format="json"
        ).status_code
        == 400
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
        is_active=True,
    )
    base = f"/api/v1/admin/farms/{farm.pk}"
    available = farm_admin_api.get(f"{base}/available-products")
    assert any(row["id"] == str(product.pk) for row in available.json()["data"])

    product.is_active = False
    product.save(update_fields=["is_active"])
    response = farm_admin_api.post(
        f"{base}/products", {"product_id": str(product.pk)}, format="json"
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
    with patch("farms.views.add_farm_product", side_effect=Product.DoesNotExist):
        response = farm_admin_api.post(
            f"{base}/products", {"product_id": str(product.pk)}, format="json"
        )
    assert response.status_code == 404
    with patch("farms.views.update_farm_product", side_effect=FarmConflict("farm_product_changed")):
        response = farm_admin_api.patch(
            f"{base}/products/{product.pk}", {"is_active": False}, format="json"
        )
    assert response.status_code == 409
    assert FarmProduct.objects.filter(pk=link.pk).exists()

    with patch("farms.views.update_farm", side_effect=Farm.DoesNotExist):
        response = farm_admin_api.patch(
            base, {"version": farm.version, "name_en": "Changed"}, format="json"
        )
    assert response.status_code == 404


@pytest.mark.django_db
def test_weekly_write_returns_conflict_if_link_disappears_after_initial_lookup(farm_admin_api):
    farm = Farm.objects.create(**farm_payload())
    category = Category.objects.create(slug="farm-product-race", is_active=True)
    product = Product.objects.create(
        category=category,
        sku="RACE-500G",
        slug="race-500g",
        unit_size="500.000",
        unit_label_vi="gói",
        unit_label_en="pack",
        retail_price_vnd=25000,
    )
    FarmProduct.objects.create(farm=farm, product=product)
    with patch.object(FarmProduct.objects, "select_for_update") as locked:
        locked.return_value.filter.return_value.first.return_value = None
        response = farm_admin_api.put(
            f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/weeks/2026-10-19",
            {"quantity_units": 10},
            format="json",
        )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "farm_product_changed"
    with patch.object(FarmProduct.objects, "select_for_update") as locked:
        locked.return_value.filter.return_value.first.return_value = None
        deleted = farm_admin_api.delete(f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}")
    assert deleted.status_code == 409
    assert deleted.json()["errors"][0]["code"] == "farm_product_changed"


@pytest.mark.django_db
def test_farm_api_requires_admin(active_user, access_token):
    access_token.scope = "profile email"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert client.get("/api/v1/admin/farms").status_code == 403
    assert client.post("/api/v1/admin/farms", farm_payload(), format="json").status_code == 403


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
def test_product_sale_unit_cannot_change_after_farm_weekly_supply_exists(farm_admin_api):
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
    FarmWeeklySupply.objects.create(
        farm_product=row, week_start=date(2026, 10, 19), quantity_units=20
    )
    response = farm_admin_api.patch(
        f"/api/v1/admin/store-products/{product.pk}",
        {"unit_size": "2.000"},
        format="json",
    )
    assert response.status_code == 409
    product.refresh_from_db()
    assert product.unit_size == 1


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
    removed = farm_admin_api.delete(url)
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
    assert farm_admin_api.delete(url).status_code == 204

    enabled_again = farm_admin_api.post(url, {"signup_count": 99}, format="json")
    assert enabled_again.status_code == 201, enabled_again.json()
    assert enabled_again.json()["data"]["version"] > old_version
    stale = farm_admin_api.patch(url, {"version": old_version, "signup_count": 13}, format="json")
    assert stale.status_code == 409
    assert TrackerFarm.objects.get(farm_id=farm.pk).signup_count == 99


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

    removed = farm_admin_api.delete(f"/api/v1/admin/tracker-farms/{farm_id}")
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
    removed = farm_admin_api.delete(f"/api/v1/admin/tracker-farms/{farm_id}")
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
def test_weekly_supply_list_requires_bounded_monday_range(farm_admin_api):
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
    FarmWeeklySupply.objects.create(
        farm_product=row, week_start=date(2026, 10, 19), quantity_units=8
    )
    url = f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}/weeks"
    good = farm_admin_api.get(f"{url}?from_week=2026-10-19&to_week=2026-10-26")
    assert good.status_code == 200, good.json()
    assert good.json()["data"] == [{"week_start": "2026-10-19", "quantity_units": 8}]
    assert farm_admin_api.get(f"{url}?from_week=2026-10-20&to_week=2026-10-26").status_code == 400
    assert farm_admin_api.get(f"{url}?from_week=2026-10-19&to_week=2028-10-23").status_code == 400


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
def test_unreferenced_farm_and_unsupplied_link_can_be_removed_but_history_is_protected(
    farm_admin_api,
):
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
    assert farm_admin_api.delete(link_url).status_code == 204
    assert not FarmProduct.objects.filter(pk=relation.pk).exists()
    assert farm_admin_api.delete(farm_url).status_code == 204
    assert not Farm.objects.filter(pk=farm.pk).exists()

    farm = Farm.objects.create(**farm_payload(name_en="Second Farm"))
    relation = FarmProduct.objects.create(farm=farm, product=product)
    FarmWeeklySupply.objects.create(
        farm_product=relation, week_start=date(2026, 10, 19), quantity_units=5
    )
    assert (
        farm_admin_api.delete(f"/api/v1/admin/farms/{farm.pk}/products/{product.pk}").status_code
        == 409
    )
