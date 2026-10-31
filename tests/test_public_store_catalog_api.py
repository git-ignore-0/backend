from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from commerce.models import Category, CategoryTranslation, Product, ProductTranslation
from commerce.services import update_product
from content.models import MediaAsset
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships.models import Product as MembershipProduct
from memberships.models import ProductTranslation as MembershipProductTranslation


def make_category(slug, *, parent=None, active=True, sort_order=0):
    category = Category.objects.create(
        slug=slug, parent=parent, is_active=active, sort_order=sort_order
    )
    CategoryTranslation.objects.bulk_create(
        [
            CategoryTranslation(category=category, locale="vi", name=f"VI {slug}", description=""),
            CategoryTranslation(category=category, locale="en", name=f"EN {slug}", description=""),
        ]
    )
    return category


def make_image(name="tomato"):
    return MediaAsset.objects.create(
        file=f"products/{name}.webp",
        variants=[{"file": f"products/{name}-640.webp", "width": 640, "height": 480}],
        width=1600,
        height=1200,
        original_name=f"{name}.webp",
    )


def make_product(
    slug,
    category,
    *,
    active=True,
    published=True,
    price=45_000,
    image=None,
    quota_group=None,
):
    product = Product.objects.create(
        category=category,
        csa_quota_group=quota_group,
        sku=slug.upper(),
        slug=slug,
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=price,
        csa_quota_cost=2 if quota_group else 0,
        primary_image=image or make_image(slug),
        is_active=active,
        is_published=published,
    )
    ProductTranslation.objects.bulk_create(
        [
            ProductTranslation(
                product=product,
                locale="vi",
                name=f"VI {slug}",
                short_description="Mô tả ngắn",
                description="Mô tả đầy đủ",
            ),
            ProductTranslation(
                product=product,
                locale="en",
                name=f"EN {slug}",
                short_description="Short description",
                description="Full description",
            ),
        ]
    )
    return product


def make_quota_group(*, vi_name="Rau", en_name="Vegetables"):
    group = MembershipProduct.objects.create(
        name=vi_name,
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        is_active=True,
    )
    translations = [
        MembershipProductTranslation(
            product=group, locale="vi", name=vi_name, short_description=""
        )
    ]
    if en_name is not None:
        translations.append(
            MembershipProductTranslation(
                product=group, locale="en", name=en_name, short_description=""
            )
        )
    MembershipProductTranslation.objects.bulk_create(translations)
    return group


@pytest.mark.django_db
def test_public_store_hides_non_public_products_and_inactive_ancestors():
    client = APIClient()
    active_root = make_category("active-root")
    active_leaf = make_category("active-leaf", parent=active_root)
    inactive_root = make_category("inactive-root", active=False)
    hidden_leaf = make_category("hidden-leaf", parent=inactive_root)
    public = make_product("public", active_leaf)
    make_product("draft", active_leaf, published=False)
    make_product("inactive", active_leaf, active=False, published=False)
    ancestor_hidden = make_product("ancestor-hidden", hidden_leaf)

    response = client.get("/api/v1/public/store-products?locale=vi")

    assert response.status_code == 200
    assert [item["slug"] for item in response.json()["data"]] == [public.slug]
    detail = client.get(f"/api/v1/public/store-products/{ancestor_hidden.slug}?locale=vi")
    assert detail.status_code == 404


@pytest.mark.django_db
def test_public_store_categories_include_only_usable_tree():
    client = APIClient()
    root = make_category("root", sort_order=2)
    branch = make_category("branch", parent=root, sort_order=1)
    leaf = make_category("leaf", parent=branch, sort_order=3)
    unused = make_category("unused", sort_order=0)
    make_product("tomato", leaf)

    response = client.get("/api/v1/public/store-categories?locale=en")

    assert response.status_code == 200
    assert response.json() == {
        "data": [
            {
                "id": str(root.pk),
                "parent_id": None,
                "slug": "root",
                "sort_order": 2,
                "name": "EN root",
                "requested_locale": "en",
                "content_locale": "en",
                "is_fallback": False,
            },
            {
                "id": str(branch.pk),
                "parent_id": str(root.pk),
                "slug": "branch",
                "sort_order": 1,
                "name": "EN branch",
                "requested_locale": "en",
                "content_locale": "en",
                "is_fallback": False,
            },
            {
                "id": str(leaf.pk),
                "parent_id": str(branch.pk),
                "slug": "leaf",
                "sort_order": 3,
                "name": "EN leaf",
                "requested_locale": "en",
                "content_locale": "en",
                "is_fallback": False,
            },
        ]
    }
    assert str(unused.pk) not in response.content.decode()


@pytest.mark.django_db
def test_public_store_list_returns_localized_card_contract():
    client = APIClient()
    category = make_category("vegetables")
    quota_group = make_quota_group()
    product = make_product("tomato", category, quota_group=quota_group)
    farm = Farm.objects.create(
        name_vi="Nông trại A",
        name_en="Farm A",
        location_vi="Đà Lạt",
        location_en="Da Lat",
        is_active=True,
    )
    FarmProduct.objects.create(farm=farm, product=product, is_active=True)

    response = client.get("/api/v1/public/store-products?locale=vi")

    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response.json()["meta"] == {"page": 1, "page_size": 12, "total": 1}, response.json()
    row = response.json()["data"][0]
    assert row == {
        "id": str(product.pk),
        "slug": "tomato",
        "sku": "TOMATO",
        "name": "VI tomato",
        "alternate_name": "EN tomato",
        "short_description": "Mô tả ngắn",
        "retail_price_vnd": 45_000,
        "available_quantity": 0,
        "unit_size": "1.000",
        "unit_label": "kg",
        "category": {
            "id": str(category.pk),
            "slug": "vegetables",
            "name": "VI vegetables",
        },
        "primary_image": {
            "id": str(product.primary_image_id),
            "url": f"http://testserver/media/{product.primary_image.file.name}",
            "width": 1600,
            "height": 1200,
            "variants": [
                {
                    "url": "http://testserver/media/products/tomato-640.webp",
                    "width": 640,
                    "height": 480,
                }
            ],
        },
        "csa_quota_group": {
            "id": str(quota_group.pk),
            "name": "Rau",
            "cost": 2,
        },
        "suppliers": [{"id": str(farm.pk), "name": "Nông trại A"}],
        "requested_locale": "vi",
        "content_locale": "vi",
        "available_locales": ["en", "vi"],
        "is_fallback": False,
    }


@pytest.mark.django_db
def test_public_store_sums_only_current_stock_from_active_farms_and_links():
    category = make_category("stocked-vegetables")
    product = make_product("stocked-tomato", category)
    today = timezone.localdate()

    def add_batch(
        name,
        quantity,
        *,
        farm_active=True,
        link_active=True,
        start_offset=-1,
        end_offset=1,
        cancelled=False,
    ):
        farm = Farm.objects.create(
            name_vi=name,
            name_en=name,
            location_vi="Đà Lạt",
            location_en="Da Lat",
            is_active=farm_active,
        )
        relation = FarmProduct.objects.create(
            farm=farm, product=product, is_active=link_active
        )
        FarmProductStockBatch.objects.create(
            farm_product=relation,
            start_date=today + timedelta(days=start_offset),
            end_date=today + timedelta(days=end_offset),
            quantity_units=quantity,
            cancelled_at=timezone.now() if cancelled else None,
        )

    add_batch("Farm A", 10)
    add_batch("Farm B", 15, start_offset=0, end_offset=0)
    add_batch("Inactive farm", 100, farm_active=False)
    add_batch("Inactive link", 100, link_active=False)
    add_batch("Upcoming", 100, start_offset=1, end_offset=2)
    add_batch("Expired", 100, start_offset=-2, end_offset=-1)
    add_batch("Cancelled", 100, cancelled=True)

    client = APIClient()
    listed = client.get("/api/v1/public/store-products?locale=vi")
    detail = client.get(f"/api/v1/public/store-products/{product.slug}?locale=vi")

    assert listed.status_code == 200
    assert detail.status_code == 200
    assert listed["Cache-Control"] == "private, no-store"
    assert detail["Cache-Control"] == "private, no-store"
    assert listed.json()["data"][0]["available_quantity"] == 25
    assert detail.json()["data"]["available_quantity"] == 25


@pytest.mark.django_db
def test_public_store_detail_returns_description_and_active_supplier_summaries():
    client = APIClient()
    category = make_category("vegetables")
    quota_group = make_quota_group()
    product = make_product("tomato", category, quota_group=quota_group)
    visible = Farm.objects.create(
        name_vi="Nông trại A",
        name_en="Farm A",
        location_vi="Đà Lạt",
        location_en="Da Lat",
        is_active=True,
    )
    hidden = Farm.objects.create(
        name_vi="Nông trại B",
        name_en="Farm B",
        location_vi="Hà Nội",
        location_en="Ha Noi",
        is_active=False,
    )
    FarmProduct.objects.create(farm=visible, product=product, is_active=True)
    FarmProduct.objects.create(farm=hidden, product=product, is_active=True)

    response = client.get("/api/v1/public/store-products/tomato?locale=en")

    assert response.status_code == 200
    row = response.json()["data"]
    assert row["description"] == "Full description"
    assert row["suppliers"] == [{"id": str(visible.pk), "name": "Farm A", "location": "Da Lat"}]
    assert row["csa_quota_group"] == {
        "id": str(quota_group.pk),
        "name": "Vegetables",
        "cost": 2,
    }
    assert "csa_eligible" not in row
    assert "csa_quota_cost" not in row
    assert "is_published" not in row
    assert "weekly_supplies" not in row


@pytest.mark.django_db
def test_public_store_returns_null_csa_group_for_retail_product():
    category = make_category("vegetables")
    product = make_product("tomato", category)

    response = APIClient().get(f"/api/v1/public/store-products/{product.slug}?locale=vi")

    assert response.status_code == 200
    row = response.json()["data"]
    assert row["csa_quota_group"] is None
    assert "csa_eligible" not in row


@pytest.mark.django_db
def test_public_store_csa_group_name_falls_back_to_vietnamese():
    category = make_category("vegetables")
    quota_group = make_quota_group(vi_name="Củ quả", en_name=None)
    product = make_product("carrot", category, quota_group=quota_group)

    response = APIClient().get(f"/api/v1/public/store-products/{product.slug}?locale=en")

    assert response.status_code == 200
    assert response.json()["data"]["csa_quota_group"] == {
        "id": str(quota_group.pk),
        "name": "Củ quả",
        "cost": 2,
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query",
    [
        "",
        "?locale=fr",
        "?locale=vi&unknown=1",
        "?locale=vi&search=" + "x" * 101,
        "?locale=vi&page=0",
        "?locale=vi&page=10001",
        "?locale=vi&page_size=0",
        "?locale=vi&page_size=49",
        "?locale=vi&price_min=-1",
        "?locale=vi&price_max=1000000001",
        "?locale=vi&price_min=50000&price_max=40000",
        "?locale=vi&sort=featured",
    ],
)
def test_public_store_rejects_invalid_or_unbounded_queries(query):
    response = APIClient().get(f"/api/v1/public/store-products{query}")

    assert response.status_code == 400


@pytest.mark.django_db
def test_public_store_filters_searches_and_sorts_on_public_fields():
    client = APIClient()
    root = make_category("root")
    first_leaf = make_category("first", parent=root)
    second_leaf = make_category("second", parent=root)
    quota_group = MembershipProduct.objects.create(
        name="Quota",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        is_active=True,
    )
    cheap = make_product("cheap", first_leaf, price=20_000)
    eligible = make_product("eligible", first_leaf, price=30_000, quota_group=quota_group)
    make_product("expensive", second_leaf, price=60_000)
    farm = Farm.objects.create(
        name_vi="Vườn Mặt Trời",
        name_en="Sun Farm",
        location_vi="Huế",
        location_en="Hue",
        is_active=True,
    )
    FarmProduct.objects.create(farm=farm, product=eligible, is_active=True)

    by_tree = client.get(
        f"/api/v1/public/store-products?locale=vi&category_id={root.pk}&sort=price_desc"
    )
    assert [item["slug"] for item in by_tree.json()["data"]] == [
        "expensive",
        "eligible",
        "cheap",
    ]

    filtered = client.get(
        "/api/v1/public/store-products?locale=vi&csa_eligible=true&price_min=25000&price_max=35000"
    )
    assert [item["slug"] for item in filtered.json()["data"]] == [eligible.slug]

    search = client.get("/api/v1/public/store-products?locale=en&search=Sun%20Farm")
    assert [item["slug"] for item in search.json()["data"]] == [eligible.slug]

    ineligible = client.get(
        "/api/v1/public/store-products?locale=vi&csa_eligible=false&sort=price_asc"
    )
    assert [item["slug"] for item in ineligible.json()["data"]] == [
        cheap.slug,
        "expensive",
    ]


@pytest.mark.django_db
def test_public_store_name_sort_keeps_legacy_locale_fallback_rows():
    client = APIClient()
    category = make_category("vegetables")
    fallback = make_product("fallback", category)
    ProductTranslation.objects.filter(product=fallback, locale="en").delete()
    english = make_product("english", category)

    response = client.get("/api/v1/public/store-products?locale=en&sort=name")

    assert response.status_code == 200
    assert {item["slug"] for item in response.json()["data"]} == {
        fallback.slug,
        english.slug,
    }
    fallback_row = next(item for item in response.json()["data"] if item["slug"] == fallback.slug)
    assert fallback_row["content_locale"] == "vi"
    assert fallback_row["is_fallback"] is True


@pytest.mark.django_db
def test_public_store_list_query_count_is_bounded_for_a_full_page():
    category = make_category("vegetables")
    for index in range(12):
        make_product(f"product-{index:02d}", category)

    with CaptureQueriesContext(connection) as queries:
        response = APIClient().get("/api/v1/public/store-products?locale=vi&page=1&page_size=12")

    assert response.status_code == 200
    assert len(response.json()["data"]) == 12
    assert len(queries) <= 7


@pytest.mark.django_db
def test_public_store_rejects_loose_booleans_and_hides_prices_outside_public_contract():
    category = make_category("root_vegetables")
    product = make_product("premium_item", category)
    client = APIClient()

    assert client.get("/api/v1/public/store-products?locale=vi&csa_eligible=yes").status_code == 400
    assert client.get("/api/v1/public/store-products?locale=vi&csa_eligible=1").status_code == 400
    assert (
        client.get("/api/v1/public/store-products?locale=vi&csa_eligible=true").status_code == 200
    )
    Product.objects.filter(pk=product.pk).update(retail_price_vnd=1_000_000_001)
    response = client.get("/api/v1/public/store-products?locale=vi")
    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.django_db
def test_published_product_cannot_be_saved_above_public_price_limit():
    category = make_category("vegetables")
    product = make_product("premium", category)

    with pytest.raises(ValidationError) as error:
        update_product(actor=None, product=product, payload={"retail_price_vnd": 1_000_000_001})

    assert "Published products exceed the public price limit" in str(error.value)
