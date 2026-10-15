import io
from decimal import Decimal

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image
from rest_framework.test import APIClient

from commerce.models import Category, Product
from content.models import MediaAsset
from memberships.models import MembershipPackage
from memberships.models import Product as MembershipProduct
from memberships.models import ProductTranslation as MembershipProductTranslation
from memberships.serializers import PackageWriteSerializer


@pytest.fixture
def admin_api(active_user, access_token):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def category_payload(slug="vegetables", *, parent_id=None, is_active=True):
    return {
        "slug": slug,
        "parent_id": parent_id,
        "sort_order": 0,
        "is_active": is_active,
        "translations": [
            {"locale": "vi", "name": f"Rau {slug}", "description": "Rau sạch"},
            {"locale": "en", "name": f"Vegetables {slug}", "description": "Fresh vegetables"},
        ],
    }


def product_payload(category_id, *, group_id=None, cost=0, published=False, image_id=None):
    return {
        "sku": " rau-muong-500g ",
        "slug": "rau-muong-500g",
        "category_id": category_id,
        "unit_size": "500.000",
        "unit_label_vi": "gói 500g",
        "unit_label_en": "500g pack",
        "retail_price_vnd": 35000,
        "csa_quota_group_id": group_id,
        "csa_quota_cost": cost,
        "primary_image_id": image_id,
        "is_active": True,
        "is_published": published,
        "translations": [
            {
                "locale": "vi",
                "name": "Rau muống",
                "short_description": "Rau tươi",
                "description": "Thu hoạch trong ngày",
            },
            {
                "locale": "en",
                "name": "Water spinach",
                "short_description": "Fresh greens",
                "description": "Harvested today",
            },
        ],
    }


def image_upload(name="product.png"):
    output = io.BytesIO()
    Image.new("RGB", (300, 200), "#356b45").save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


@pytest.mark.django_db
def test_commerce_catalog_requires_website_admin(client, active_user, access_token):
    paths = ["store-categories", "store-products"]
    for path in paths:
        anonymous = client.post(f"/api/v1/admin/{path}", {}, content_type="application/json")
        assert anonymous.status_code in {401, 403}
        ordinary = APIClient()
        ordinary.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
        denied = ordinary.post(f"/api/v1/admin/{path}", {}, format="json")
        assert denied.status_code == 403
    missing_id = "11111111-1111-4111-8111-111111111111"
    for path in (
        f"store-categories/{missing_id}",
        f"store-products/{missing_id}",
        f"store-products/media/{missing_id}",
    ):
        assert ordinary.delete(f"/api/v1/admin/{path}").status_code == 403
    assert (
        ordinary.patch(f"/api/v1/admin/store-products/{missing_id}", {}, format="json").status_code
        == 403
    )
    assert (
        ordinary.post("/api/v1/admin/store-products/media", {}, format="multipart").status_code
        == 403
    )


@pytest.mark.django_db
def test_category_tree_and_protected_delete(admin_api):
    root = admin_api.post("/api/v1/admin/store-categories", category_payload(), format="json")
    assert root.status_code == 201, root.json()
    root_id = root.json()["data"]["id"]
    child = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("leafy", parent_id=root_id),
        format="json",
    )
    assert child.status_code == 201, child.json()
    child_id = child.json()["data"]["id"]
    grandchild = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("greens", parent_id=child_id),
        format="json",
    )
    assert grandchild.status_code == 201, grandchild.json()
    assert grandchild.json()["data"]["depth"] == 3
    too_deep = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("fourth", parent_id=grandchild.json()["data"]["id"]),
        format="json",
    )
    assert too_deep.status_code == 400
    cycle = admin_api.patch(
        f"/api/v1/admin/store-categories/{root_id}",
        {"parent_id": child_id},
        format="json",
    )
    assert cycle.status_code == 400
    protected = admin_api.delete(f"/api/v1/admin/store-categories/{root_id}")
    assert protected.status_code == 409
    assert protected.json()["errors"][0]["code"] == "category_in_use"


@pytest.mark.django_db
def test_category_search_and_inactive_draft(admin_api):
    draft = category_payload("draft", is_active=False)
    draft["translations"] = [{"locale": "vi", "name": "Bản nháp", "description": ""}]
    created = admin_api.post("/api/v1/admin/store-categories", draft, format="json")
    assert created.status_code == 201, created.json()
    activate = admin_api.patch(
        f"/api/v1/admin/store-categories/{created.json()['data']['id']}",
        {"is_active": True},
        format="json",
    )
    assert activate.status_code == 400
    found = admin_api.get("/api/v1/admin/store-categories?search=Bản%20nháp&is_active=false")
    assert found.status_code == 200
    assert [row["slug"] for row in found.json()["data"]] == ["draft"]


@pytest.mark.django_db
def test_inactive_child_remains_editable_below_inactive_parent(admin_api):
    parent = admin_api.post(
        "/api/v1/admin/store-categories", category_payload("parent"), format="json"
    ).json()["data"]
    child = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("child", parent_id=parent["id"]),
        format="json",
    ).json()["data"]

    assert (
        admin_api.patch(
            f"/api/v1/admin/store-categories/{child['id']}",
            {"is_active": False},
            format="json",
        ).status_code
        == 200
    )
    assert (
        admin_api.patch(
            f"/api/v1/admin/store-categories/{parent['id']}",
            {"is_active": False},
            format="json",
        ).status_code
        == 200
    )

    edited = admin_api.patch(
        f"/api/v1/admin/store-categories/{child['id']}",
        {"sort_order": 7},
        format="json",
    )
    assert edited.status_code == 200, edited.json()
    assert edited.json()["data"]["sort_order"] == 7

    reactivated = admin_api.patch(
        f"/api/v1/admin/store-categories/{child['id']}",
        {"is_active": True},
        format="json",
    )
    assert reactivated.status_code == 400


@pytest.mark.django_db
def test_automatic_slugs_use_category_name_and_product_sku_and_stay_stable(admin_api):
    first_category = category_payload("first")
    first_category.pop("slug")
    first_category["translations"] = [
        {"locale": "vi", "name": "Rau ăn lá", "description": ""},
        {"locale": "en", "name": "Leafy Greens", "description": ""},
    ]
    first = admin_api.post("/api/v1/admin/store-categories", first_category, format="json")
    assert first.status_code == 201, first.json()
    assert first.json()["data"]["slug"] == "leafy-greens"

    duplicate_category = {**first_category, "is_active": False}
    duplicate = admin_api.post("/api/v1/admin/store-categories", duplicate_category, format="json")
    assert duplicate.status_code == 201, duplicate.json()
    assert duplicate.json()["data"]["slug"] == "leafy-greens-2"

    renamed = admin_api.patch(
        f"/api/v1/admin/store-categories/{first.json()['data']['id']}",
        {
            "translations": [
                {"locale": "vi", "name": "Rau xanh", "description": ""},
                {"locale": "en", "name": "Green Vegetables", "description": ""},
            ]
        },
        format="json",
    )
    assert renamed.status_code == 200, renamed.json()
    assert renamed.json()["data"]["slug"] == "leafy-greens"

    first_product = product_payload(first.json()["data"]["id"])
    first_product.pop("slug")
    created_product = admin_api.post("/api/v1/admin/store-products", first_product, format="json")
    assert created_product.status_code == 201, created_product.json()
    assert created_product.json()["data"]["slug"] == "rau-muong-500g"

    duplicate_product = {**first_product, "sku": "RAU--MUONG-500G"}
    second_product = admin_api.post(
        "/api/v1/admin/store-products", duplicate_product, format="json"
    )
    assert second_product.status_code == 201, second_product.json()
    assert second_product.json()["data"]["slug"] == "rau-muong-500g-2"

    renamed_product = admin_api.patch(
        f"/api/v1/admin/store-products/{created_product.json()['data']['id']}",
        {
            "sku": "RAU-MUONG-MOI-500G",
            "translations": [
                {
                    "locale": "vi",
                    "name": "Rau muống mới",
                    "short_description": "",
                    "description": "",
                },
                {
                    "locale": "en",
                    "name": "New water spinach",
                    "short_description": "",
                    "description": "",
                },
            ],
        },
        format="json",
    )
    assert renamed_product.status_code == 200, renamed_product.json()
    assert renamed_product.json()["data"]["sku"] == "RAU-MUONG-MOI-500G"
    assert renamed_product.json()["data"]["slug"] == "rau-muong-500g"


@pytest.mark.django_db
def test_product_auto_slug_is_generated_after_acquiring_catalog_lock(admin_api, monkeypatch):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    payload = product_payload(category["id"])
    payload.pop("slug")
    lock_acquired = False

    from commerce import services

    original_slug = services._automatic_slug

    def record_lock():
        nonlocal lock_acquired
        lock_acquired = True

    def assert_locked(*args, **kwargs):
        assert lock_acquired
        return original_slug(*args, **kwargs)

    monkeypatch.setattr(services, "_lock_category_tree", record_lock)
    monkeypatch.setattr(services, "_automatic_slug", assert_locked)

    created = admin_api.post("/api/v1/admin/store-products", payload, format="json")
    assert created.status_code == 201, created.json()


@pytest.mark.django_db
def test_product_retail_and_csa_rules(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    retail = admin_api.post(
        "/api/v1/admin/store-products", product_payload(category["id"]), format="json"
    )
    assert retail.status_code == 201, retail.json()
    assert retail.json()["data"]["sku"] == "RAU-MUONG-500G"
    assert retail.json()["data"]["csa_quota_group_id"] is None
    string_price = product_payload(category["id"])
    string_price["sku"] = "PRICE-STRING"
    string_price["slug"] = "price-string"
    string_price["retail_price_vnd"] = "9007199254740993"
    string_price_response = admin_api.post(
        "/api/v1/admin/store-products", string_price, format="json"
    )
    assert string_price_response.status_code == 201, string_price_response.json()
    assert string_price_response.json()["data"]["retail_price_vnd"] == "9007199254740993"
    assert Product.objects.get(sku="PRICE-STRING").retail_price_vnd == 9007199254740993
    duplicate = admin_api.post(
        "/api/v1/admin/store-products", product_payload(category["id"]), format="json"
    )
    assert duplicate.status_code == 400
    invalid = product_payload(category["id"], cost=1)
    invalid["sku"] = "OTHER"
    invalid["slug"] = "other"
    assert admin_api.post("/api/v1/admin/store-products", invalid, format="json").status_code == 400
    group = MembershipProduct.objects.create(
        name="Rau lá",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    csa = product_payload(category["id"], group_id=str(group.pk), cost=2)
    csa["sku"] = "CSA-RAU-MUONG"
    csa["slug"] = "csa-rau-muong"
    created = admin_api.post("/api/v1/admin/store-products", csa, format="json")
    assert created.status_code == 201, created.json()
    assert created.json()["data"]["csa_quota_cost"] == 2
    assert created.json()["data"]["csa_quota_group"]["name"] == "Rau lá"
    group.is_active = False
    group.save(update_fields=["is_active"])
    csa["sku"] = "CSA-ANOTHER"
    csa["slug"] = "csa-another"
    assert admin_api.post("/api/v1/admin/store-products", csa, format="json").status_code == 400


@pytest.mark.django_db
def test_product_requires_leaf_category_and_image_for_publication(admin_api, tmp_path):
    root = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    leaf = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("leaf", parent_id=root["id"]),
        format="json",
    ).json()["data"]
    rejected = admin_api.post(
        "/api/v1/admin/store-products", product_payload(root["id"]), format="json"
    )
    assert rejected.status_code == 400
    published = product_payload(leaf["id"], published=True)
    assert (
        admin_api.post("/api/v1/admin/store-products", published, format="json").status_code == 400
    )
    with override_settings(MEDIA_ROOT=tmp_path):
        upload = admin_api.post(
            "/api/v1/admin/store-products/media", {"image": image_upload()}, format="multipart"
        )
        assert upload.status_code == 201, upload.json()
        published["primary_image_id"] = upload.json()["data"]["id"]
        created = admin_api.post("/api/v1/admin/store-products", published, format="json")
        assert created.status_code == 201, created.json()
        assert created.json()["data"]["is_published"] is True
        assert created.json()["data"]["primary_image"]["url"].endswith(".webp")
        found = admin_api.get(
            "/api/v1/admin/store-products", {"search": "Rau muống", "is_published": "true"}
        )
        assert found.status_code == 200
        assert [row["id"] for row in found.json()["data"]] == [created.json()["data"]["id"]]


@pytest.mark.django_db
def test_product_delete_and_category_reference(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    product = admin_api.post(
        "/api/v1/admin/store-products", product_payload(category["id"]), format="json"
    ).json()["data"]
    assert admin_api.delete(f"/api/v1/admin/store-categories/{category['id']}").status_code == 409
    assert admin_api.delete(f"/api/v1/admin/store-products/{product['id']}").status_code == 204
    assert admin_api.delete(f"/api/v1/admin/store-categories/{category['id']}").status_code == 204


@pytest.mark.django_db
def test_category_cannot_gain_child_after_receiving_product(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    admin_api.post("/api/v1/admin/store-products", product_payload(category["id"]), format="json")
    child = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("child", parent_id=category["id"]),
        format="json",
    )
    assert child.status_code == 409
    assert child.json()["errors"][0]["code"] == "category_has_products"


@pytest.mark.django_db
def test_category_move_checks_descendant_depth(admin_api):
    a = admin_api.post(
        "/api/v1/admin/store-categories", category_payload("a"), format="json"
    ).json()["data"]
    b = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("b", parent_id=a["id"]),
        format="json",
    ).json()["data"]
    parent = admin_api.post(
        "/api/v1/admin/store-categories", category_payload("other"), format="json"
    ).json()["data"]
    grandparent = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("other-child", parent_id=parent["id"]),
        format="json",
    ).json()["data"]
    moved = admin_api.patch(
        f"/api/v1/admin/store-categories/{a['id']}",
        {"parent_id": grandparent["id"]},
        format="json",
    )
    assert moved.status_code == 400
    assert Category.objects.get(pk=b["id"]).parent_id == Category.objects.get(pk=a["id"]).pk


@pytest.mark.django_db
def test_product_patch_and_filters(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    created = admin_api.post(
        "/api/v1/admin/store-products", product_payload(category["id"]), format="json"
    ).json()["data"]
    changed = admin_api.patch(
        f"/api/v1/admin/store-products/{created['id']}",
        {"retail_price_vnd": 39000, "is_active": False},
        format="json",
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["retail_price_vnd"] == "39000"
    assert changed.json()["data"]["is_active"] is False
    matching = admin_api.get(
        "/api/v1/admin/store-products",
        {"category_id": category["id"], "is_active": "false", "csa_eligible": "false"},
    )
    assert [row["id"] for row in matching.json()["data"]] == [created["id"]]
    assert admin_api.get("/api/v1/admin/store-products", {"is_active": "true"}).json()["data"] == []


@pytest.mark.django_db
def test_product_category_filter_includes_descendants(admin_api):
    root = admin_api.post(
        "/api/v1/admin/store-categories", category_payload("root"), format="json"
    ).json()["data"]
    child = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("child", parent_id=root["id"]),
        format="json",
    ).json()["data"]
    leaf = admin_api.post(
        "/api/v1/admin/store-categories",
        category_payload("leaf", parent_id=child["id"]),
        format="json",
    ).json()["data"]
    product = admin_api.post(
        "/api/v1/admin/store-products", product_payload(leaf["id"]), format="json"
    ).json()["data"]
    for category_id in (root["id"], child["id"], leaf["id"]):
        response = admin_api.get("/api/v1/admin/store-products", {"category_id": category_id})
        assert response.status_code == 200, response.json()
        assert [item["id"] for item in response.json()["data"]] == [product["id"]]


@pytest.mark.django_db
def test_draft_product_can_be_edited_or_moved_after_category_deactivation(admin_api):
    old = admin_api.post(
        "/api/v1/admin/store-categories", category_payload("old"), format="json"
    ).json()["data"]
    new = admin_api.post(
        "/api/v1/admin/store-categories", category_payload("new"), format="json"
    ).json()["data"]
    product = admin_api.post(
        "/api/v1/admin/store-products", product_payload(old["id"]), format="json"
    ).json()["data"]
    assert (
        admin_api.patch(
            f"/api/v1/admin/store-categories/{old['id']}",
            {"is_active": False},
            format="json",
        ).status_code
        == 200
    )

    edited = admin_api.patch(
        f"/api/v1/admin/store-products/{product['id']}",
        {"category_id": old["id"], "retail_price_vnd": 40000},
        format="json",
    )
    assert edited.status_code == 200, edited.json()
    assert edited.json()["data"]["retail_price_vnd"] == "40000"
    edited_without_category = admin_api.patch(
        f"/api/v1/admin/store-products/{product['id']}",
        {"retail_price_vnd": 41000},
        format="json",
    )
    assert edited_without_category.status_code == 200, edited_without_category.json()
    publish = admin_api.patch(
        f"/api/v1/admin/store-products/{product['id']}",
        {"is_published": True},
        format="json",
    )
    assert publish.status_code == 400
    assert "category_id" in publish.json()

    moved = admin_api.patch(
        f"/api/v1/admin/store-products/{product['id']}",
        {"category_id": new["id"]},
        format="json",
    )
    assert moved.status_code == 200, moved.json()
    assert moved.json()["data"]["category_id"] == new["id"]


@pytest.mark.django_db
def test_product_rejects_sku_that_cannot_form_a_slug(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    payload = product_payload(category["id"])
    payload.pop("slug")
    payload["sku"] = "---"
    response = admin_api.post("/api/v1/admin/store-products", payload, format="json")
    assert response.status_code == 400
    assert "sku" in response.json()
    assert "translations" not in response.json()


@pytest.mark.django_db
def test_product_quota_group_payload_includes_translations(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    group = MembershipProduct.objects.create(name="Rau", unit_size=Decimal("1"))
    MembershipProductTranslation.objects.create(product=group, locale="vi", name="Rau")
    MembershipProductTranslation.objects.create(product=group, locale="en", name="Vegetables")
    response = admin_api.post(
        "/api/v1/admin/store-products",
        product_payload(category["id"], group_id=str(group.pk), cost=1),
        format="json",
    )
    assert response.status_code == 201, response.json()
    assert sorted(
        response.json()["data"]["csa_quota_group"]["translations"],
        key=lambda translation: translation["locale"],
    ) == [
        {"locale": "en", "name": "Vegetables", "short_description": ""},
        {"locale": "vi", "name": "Rau", "short_description": ""},
    ]


@pytest.mark.django_db
def test_deleting_quota_group_used_by_store_product_returns_conflict(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    group = MembershipProduct.objects.create(name="Rau", unit_size=Decimal("1"))
    created = admin_api.post(
        "/api/v1/admin/store-products",
        product_payload(category["id"], group_id=str(group.pk), cost=1),
        format="json",
    )
    assert created.status_code == 201, created.json()
    blocked = admin_api.delete(f"/api/v1/admin/membership-products/{group.pk}")
    assert blocked.status_code == 409
    assert blocked.json()["errors"][0]["code"] == "csa_quota_group_in_use"
    assert MembershipProduct.objects.filter(pk=group.pk).exists()


@pytest.mark.django_db
def test_published_product_cannot_be_deactivated_without_unpublishing(admin_api, tmp_path):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    with override_settings(MEDIA_ROOT=tmp_path):
        image = admin_api.post(
            "/api/v1/admin/store-products/media", {"image": image_upload()}, format="multipart"
        ).json()["data"]
        published = product_payload(category["id"], published=True, image_id=image["id"])
        created = admin_api.post("/api/v1/admin/store-products", published, format="json").json()[
            "data"
        ]
        result = admin_api.patch(
            f"/api/v1/admin/store-products/{created['id']}",
            {"is_active": False},
            format="json",
        )
        assert result.status_code == 400
        assert Product.objects.get(pk=created["id"]).is_active is True
        unpublish = admin_api.patch(
            f"/api/v1/admin/store-products/{created['id']}",
            {"is_published": False, "is_active": False},
            format="json",
        )
        assert unpublish.status_code == 200


@pytest.mark.django_db
def test_product_media_isolated_and_orphan_cleaned_after_delete(
    admin_api, tmp_path, django_capture_on_commit_callbacks
):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    with override_settings(MEDIA_ROOT=tmp_path):
        image = admin_api.post(
            "/api/v1/admin/store-products/media", {"image": image_upload()}, format="multipart"
        ).json()["data"]
        stored_files = [
            variant["file"] for variant in MediaAsset.objects.get(pk=image["id"]).variants
        ]
        first = product_payload(category["id"], image_id=image["id"])
        created = admin_api.post("/api/v1/admin/store-products", first, format="json").json()[
            "data"
        ]
        other = product_payload(category["id"], image_id=image["id"])
        other["sku"] = "SECOND"
        other["slug"] = "second"
        second = admin_api.post("/api/v1/admin/store-products", other, format="json").json()["data"]
        admin_api.delete(f"/api/v1/admin/store-products/{created['id']}")
        assert MediaAsset.objects.filter(pk=image["id"]).exists()
        with django_capture_on_commit_callbacks(execute=True):
            admin_api.delete(f"/api/v1/admin/store-products/{second['id']}")
        assert not MediaAsset.objects.filter(pk=image["id"]).exists()
        assert not any(default_storage.exists(name) for name in stored_files)


@pytest.mark.django_db
def test_product_rejects_non_product_media(admin_api, active_user):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    image = MediaAsset.objects.create(
        file="workshops/2026/10/example.webp",
        width=100,
        height=100,
        original_name="example.webp",
        created_by=active_user,
    )
    response = admin_api.post(
        "/api/v1/admin/store-products",
        product_payload(category["id"], image_id=str(image.pk)),
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db
def test_unused_product_upload_can_be_cancelled_but_referenced_upload_cannot(admin_api, tmp_path):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    with override_settings(MEDIA_ROOT=tmp_path):
        upload = admin_api.post(
            "/api/v1/admin/store-products/media", {"image": image_upload()}, format="multipart"
        )
        image_id = upload.json()["data"]["id"]
        cancelled = admin_api.delete(f"/api/v1/admin/store-products/media/{image_id}")
        assert cancelled.status_code == 204
        assert not MediaAsset.objects.filter(pk=image_id).exists()

        upload = admin_api.post(
            "/api/v1/admin/store-products/media", {"image": image_upload()}, format="multipart"
        )
        image_id = upload.json()["data"]["id"]
        created = admin_api.post(
            "/api/v1/admin/store-products",
            product_payload(category["id"], image_id=image_id),
            format="json",
        )
        assert created.status_code == 201
        refused = admin_api.delete(f"/api/v1/admin/store-products/media/{image_id}")
        assert refused.status_code == 409
        assert MediaAsset.objects.filter(pk=image_id).exists()


@pytest.mark.django_db
def test_published_store_product_protects_quota_group_deactivation(admin_api, tmp_path):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    group = MembershipProduct.objects.create(
        name="Rau lá",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    MembershipProductTranslation.objects.create(product=group, locale="vi", name="Rau lá")
    MembershipProductTranslation.objects.create(product=group, locale="en", name="Leafy greens")
    with override_settings(MEDIA_ROOT=tmp_path):
        image = admin_api.post(
            "/api/v1/admin/store-products/media", {"image": image_upload()}, format="multipart"
        ).json()["data"]
        payload = product_payload(
            category["id"], group_id=str(group.pk), cost=1, published=True, image_id=image["id"]
        )
        created = admin_api.post("/api/v1/admin/store-products", payload, format="json")
        assert created.status_code == 201, created.json()
        blocked = admin_api.patch(
            f"/api/v1/admin/membership-products/{group.pk}",
            {"is_active": False},
            format="json",
        )
        assert blocked.status_code == 409
        assert blocked.json()["errors"][0]["code"] == "csa_quota_group_in_use"
        group.refresh_from_db()
        assert group.is_active is True


@pytest.mark.django_db
def test_draft_store_product_protects_quota_group_deactivation(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    group = MembershipProduct.objects.create(
        name="Rau lá",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    MembershipProductTranslation.objects.create(product=group, locale="vi", name="Rau lá")
    MembershipProductTranslation.objects.create(product=group, locale="en", name="Leafy greens")
    created = admin_api.post(
        "/api/v1/admin/store-products",
        product_payload(category["id"], group_id=str(group.pk), cost=1),
        format="json",
    )
    assert created.status_code == 201
    blocked = admin_api.patch(
        f"/api/v1/admin/membership-products/{group.pk}",
        {"is_active": False},
        format="json",
    )
    assert blocked.status_code == 409


@pytest.mark.django_db
def test_category_list_does_not_query_per_row(admin_api):
    for number in range(12):
        response = admin_api.post(
            "/api/v1/admin/store-categories",
            category_payload(f"cat-{number}"),
            format="json",
        )
        assert response.status_code == 201
    with CaptureQueriesContext(connection) as queries:
        response = admin_api.get("/api/v1/admin/store-categories")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 12
    assert len(queries) <= 8


@pytest.mark.django_db
def test_catalog_rejects_integer_values_outside_database_range(admin_api):
    category = category_payload()
    category["sort_order"] = 2**31
    assert (
        admin_api.post("/api/v1/admin/store-categories", category, format="json").status_code == 400
    )
    category["sort_order"] = 0
    category_id = admin_api.post("/api/v1/admin/store-categories", category, format="json").json()[
        "data"
    ]["id"]
    expensive = product_payload(category_id)
    expensive["retail_price_vnd"] = 2**63
    assert (
        admin_api.post("/api/v1/admin/store-products", expensive, format="json").status_code == 400
    )
    excessive_quota = product_payload(category_id)
    excessive_quota["csa_quota_cost"] = 2**31
    assert (
        admin_api.post("/api/v1/admin/store-products", excessive_quota, format="json").status_code
        == 400
    )


@pytest.mark.django_db
def test_category_tree_has_shared_database_lock_row():
    lock_model = django_apps.get_model("commerce", "CatalogTreeLock")
    assert lock_model.objects.filter(pk=1).exists()


@pytest.mark.django_db
def test_malformed_catalog_detail_ids_return_not_found(admin_api):
    assert admin_api.get("/api/v1/admin/store-categories/not-a-uuid").status_code == 404
    assert admin_api.get("/api/v1/admin/store-products/not-a-uuid").status_code == 404


@pytest.mark.django_db
def test_csa_package_components_remain_editable_with_legacy_public_contract(admin_api):
    def make_group(name):
        response = admin_api.post(
            "/api/v1/admin/membership-products",
            {
                "translations": [
                    {"locale": "vi", "name": name, "short_description": ""},
                    {"locale": "en", "name": f"{name} EN", "short_description": ""},
                ],
                "unit_size": "0.500",
                "unit_label_vi": "kg",
                "unit_label_en": "kg",
            },
            format="json",
        )
        assert response.status_code == 201, response.json()
        return response.json()["data"]["id"]

    leafy = make_group("Rau lá")
    herbs = make_group("Gia vị")
    package = admin_api.post(
        "/api/v1/admin/membership-packages",
        {
            "translations": [
                {"locale": "vi", "name": "Gói xanh", "description": ""},
                {"locale": "en", "name": "Green package", "description": ""},
            ],
            "quota_policy": "expire",
            "items": [{"product_id": leafy, "quota_units": 2}],
            "price_options": [
                {"duration_months": 6, "monthly_price_vnd": 1_000_000, "is_active": True}
            ],
        },
        format="json",
    )
    assert package.status_code == 201, package.json()
    package_id = package.json()["data"]["id"]
    edited = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package_id}",
        {"items": [{"product_id": herbs, "quota_units": 3}]},
        format="json",
    )
    assert edited.status_code == 200, edited.json()
    assert edited.json()["data"]["items"][0]["product_id"] == herbs
    public = admin_api.get("/api/v1/public/membership-packages?locale=vi")
    assert public.status_code == 200
    assert public.json()["data"][0]["items"][0]["product_id"] == herbs
    assert public.json()["data"][0]["items"][0]["quota_units"] == 3


@pytest.mark.django_db
def test_admin_package_item_reports_inactive_quota_group(admin_api):
    group = admin_api.post(
        "/api/v1/admin/membership-products",
        {
            "translations": [
                {"locale": "vi", "name": "Rau", "short_description": ""},
                {"locale": "en", "name": "Vegetables", "short_description": ""},
            ],
            "unit_size": "1",
            "unit_label_vi": "kg",
            "unit_label_en": "kg",
        },
        format="json",
    ).json()["data"]
    package = admin_api.post(
        "/api/v1/admin/membership-packages",
        {
            "translations": [
                {"locale": "vi", "name": "Gói rau", "description": ""},
                {"locale": "en", "name": "Vegetable package", "description": ""},
            ],
            "quota_policy": "expire",
            "items": [{"product_id": group["id"], "quota_units": 2}],
            "price_options": [
                {"duration_months": 6, "monthly_price_vnd": 1_000_000, "is_active": True}
            ],
        },
        format="json",
    ).json()["data"]
    admin_api.patch(
        f"/api/v1/admin/membership-packages/{package['id']}",
        {"is_active": False},
        format="json",
    )
    disabled = admin_api.patch(
        f"/api/v1/admin/membership-products/{group['id']}",
        {"is_active": False},
        format="json",
    )
    assert disabled.status_code == 200, disabled.json()
    response = admin_api.get(f"/api/v1/admin/membership-packages/{package['id']}")
    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["product_is_active"] is False

    reactivated = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package['id']}",
        {"is_active": True},
        format="json",
    )
    assert reactivated.status_code == 400, reactivated.json()
    package_detail = admin_api.get(f"/api/v1/admin/membership-packages/{package['id']}")
    assert package_detail.json()["data"]["is_active"] is False


@pytest.mark.django_db
def test_active_package_prevents_quota_group_deactivation(admin_api):
    group = admin_api.post(
        "/api/v1/admin/membership-products",
        {
            "translations": [
                {"locale": "vi", "name": "Rau", "short_description": ""},
                {"locale": "en", "name": "Vegetables", "short_description": ""},
            ],
            "unit_size": "1",
            "unit_label_vi": "kg",
            "unit_label_en": "kg",
        },
        format="json",
    ).json()["data"]
    package = admin_api.post(
        "/api/v1/admin/membership-packages",
        {
            "translations": [
                {"locale": "vi", "name": "Gói rau", "description": ""},
                {"locale": "en", "name": "Vegetable package", "description": ""},
            ],
            "quota_policy": "expire",
            "items": [{"product_id": group["id"], "quota_units": 2}],
            "price_options": [
                {"duration_months": 6, "monthly_price_vnd": 1_000_000, "is_active": True}
            ],
        },
        format="json",
    ).json()["data"]
    response = admin_api.patch(
        f"/api/v1/admin/membership-products/{group['id']}",
        {"is_active": False},
        format="json",
    )
    assert response.status_code == 409, response.json()
    assert response.json()["errors"][0]["code"] == "csa_quota_group_in_use"
    group_detail = admin_api.get(f"/api/v1/admin/membership-products/{group['id']}")
    assert group_detail.json()["data"]["is_active"]
    public = admin_api.get("/api/v1/public/membership-packages?locale=vi")
    assert package["id"] in {item["id"] for item in public.json()["data"]}


@pytest.mark.django_db
def test_package_create_rechecks_group_after_serializer_validation(admin_api, monkeypatch):
    group = admin_api.post(
        "/api/v1/admin/membership-products",
        {
            "translations": [
                {"locale": "vi", "name": "Rau", "short_description": ""},
                {"locale": "en", "name": "Vegetables", "short_description": ""},
            ],
            "unit_size": "1",
            "unit_label_vi": "kg",
            "unit_label_en": "kg",
        },
        format="json",
    ).json()["data"]
    original_is_valid = PackageWriteSerializer.is_valid

    def validate_then_disable(self, *args, **kwargs):
        result = original_is_valid(self, *args, **kwargs)
        MembershipProduct.objects.filter(pk=group["id"]).update(is_active=False)
        return result

    monkeypatch.setattr(PackageWriteSerializer, "is_valid", validate_then_disable)
    response = admin_api.post(
        "/api/v1/admin/membership-packages",
        {
            "translations": [
                {"locale": "vi", "name": "Gói race", "description": ""},
                {"locale": "en", "name": "Race package", "description": ""},
            ],
            "quota_policy": "expire",
            "items": [{"product_id": group["id"], "quota_units": 2}],
            "price_options": [
                {"duration_months": 6, "monthly_price_vnd": 1_000_000, "is_active": True}
            ],
        },
        format="json",
    )
    assert response.status_code == 400, response.json()
    assert not MembershipPackage.objects.filter(name="Gói race").exists()
