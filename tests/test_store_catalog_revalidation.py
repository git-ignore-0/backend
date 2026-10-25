from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import transaction
from rest_framework.test import APIClient

from commerce.models import Category, CategoryTranslation, Product, ProductTranslation
from commerce.services import schedule_store_revalidation, update_category, update_product
from content.models import MediaAsset, TrackerFarm
from content.services import update_tracker_farm
from farms.models import Farm, FarmProduct
from farms.services import update_farm, update_farm_product
from memberships.models import Product as MembershipProduct
from memberships.models import ProductTranslation as MembershipProductTranslation


def category_with_product(*, slug="vegetables", product_slug="tomato", quota_group=None):
    category = Category.objects.create(slug=slug, is_active=True)
    CategoryTranslation.objects.bulk_create(
        [
            CategoryTranslation(category=category, locale="vi", name="Rau", description=""),
            CategoryTranslation(category=category, locale="en", name="Vegetables", description=""),
        ]
    )
    image = MediaAsset.objects.create(
        file=f"products/{product_slug}.webp",
        variants=[],
        width=100,
        height=100,
        original_name=f"{product_slug}.webp",
    )
    product = Product.objects.create(
        category=category,
        csa_quota_group=quota_group,
        sku=product_slug.upper(),
        slug=product_slug,
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=40_000,
        csa_quota_cost=1 if quota_group else 0,
        primary_image=image,
        is_active=True,
        is_published=True,
    )
    ProductTranslation.objects.bulk_create(
        [
            ProductTranslation(product=product, locale="vi", name="Cà chua"),
            ProductTranslation(product=product, locale="en", name="Tomato"),
        ]
    )
    return category, product


@pytest.mark.django_db
def test_commerce_mutations_revalidate_lists_and_old_and_new_product_slugs(
    django_capture_on_commit_callbacks,
):
    category, product = category_with_product()

    with patch("commerce.services.request_frontend_revalidation") as request_revalidation:
        with django_capture_on_commit_callbacks(execute=True):
            update_category(actor=None, category=category, payload={"sort_order": 5})
        assert request_revalidation.call_args.args[0] == [
            "store-categories",
            "store-product-details",
            "store-products",
        ]

        request_revalidation.reset_mock()
        with django_capture_on_commit_callbacks(execute=True):
            update_product(actor=None, product=product, payload={"slug": "red-tomato"})
        assert request_revalidation.call_args.args[0] == [
            "store-categories",
            "store-product:red-tomato",
            "store-product:tomato",
            "store-products",
        ]


@pytest.mark.django_db
def test_store_revalidation_runs_after_commit_and_is_discarded_on_rollback(
    django_capture_on_commit_callbacks,
):
    _category, product = category_with_product()

    with patch("commerce.services.request_frontend_revalidation") as request_revalidation:
        with django_capture_on_commit_callbacks(execute=True):
            update_product(actor=None, product=product, payload={"retail_price_vnd": 41_000})
            assert request_revalidation.call_count == 0
        assert request_revalidation.call_count == 1

        request_revalidation.reset_mock()
        with django_capture_on_commit_callbacks(execute=True):
            with pytest.raises(RuntimeError):
                with transaction.atomic():
                    update_product(
                        actor=None, product=product, payload={"retail_price_vnd": 42_000}
                    )
                    raise RuntimeError("roll back")
        assert request_revalidation.call_count == 0


@pytest.mark.django_db
def test_store_revalidation_uses_one_shared_detail_tag_instead_of_unbounded_fanout(
    django_capture_on_commit_callbacks,
):
    slugs = [f"product-{index:02d}" for index in range(30)]
    with patch("commerce.services.request_frontend_revalidation") as request_revalidation:
        with django_capture_on_commit_callbacks(execute=True):
            schedule_store_revalidation(slugs=slugs, categories=True)

    assert request_revalidation.call_count == 1
    assert request_revalidation.call_args.args[0] == [
        "store-categories",
        "store-product-details",
        "store-products",
    ]


@pytest.mark.django_db
def test_category_update_revalidates_all_details_without_loading_every_product_slug(
    django_capture_on_commit_callbacks,
):
    category, first = category_with_product()
    Product.objects.bulk_create(
        [
            Product(
                category=category,
                sku=f"PRODUCT-{index:02d}",
                slug=f"product-{index:02d}",
                unit_size=Decimal("1.000"),
                unit_label_vi="kg",
                unit_label_en="kg",
                retail_price_vnd=40_000,
                is_active=False,
                is_published=False,
            )
            for index in range(24)
        ]
    )

    with patch("commerce.services.request_frontend_revalidation") as request_revalidation:
        with django_capture_on_commit_callbacks(execute=True):
            update_category(actor=None, category=category, payload={"sort_order": 7})

    assert first.slug == "tomato"
    assert request_revalidation.call_count == 1
    assert request_revalidation.call_args.args[0] == [
        "store-categories",
        "store-product-details",
        "store-products",
    ]


@pytest.mark.django_db
def test_legacy_tracker_update_revalidates_linked_store_products(
    django_capture_on_commit_callbacks,
):
    _category, product = category_with_product()
    farm = Farm.objects.create(
        name_vi="Nông trại A",
        name_en="Farm A",
        location_vi="Đà Lạt",
        location_en="Da Lat",
        is_active=True,
    )
    FarmProduct.objects.create(farm=farm, product=product, is_active=True)
    profile = TrackerFarm.objects.create(
        farm=farm,
        name_vi=farm.name_vi,
        name_en=farm.name_en,
        location_vi=farm.location_vi,
        location_en=farm.location_en,
    )

    with (
        patch("content.services.request_frontend_revalidation"),
        patch("commerce.services.request_frontend_revalidation") as store_revalidation,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            update_tracker_farm(
                farm_id=profile.pk,
                actor=None,
                expected_version=profile.version,
                data={"name_en": "Renamed Farm"},
            )

    assert store_revalidation.call_args.args[0] == [
        "store-product:tomato",
        "store-products",
    ]


@pytest.mark.django_db
def test_farm_presentation_and_offer_status_revalidate_only_linked_product(
    active_user, django_capture_on_commit_callbacks
):
    _category, product = category_with_product()
    farm = Farm.objects.create(
        name_vi="Nông trại A",
        name_en="Farm A",
        location_vi="Đà Lạt",
        location_en="Da Lat",
        is_active=True,
    )
    FarmProduct.objects.create(farm=farm, product=product, is_active=True)

    with patch("commerce.services.request_frontend_revalidation") as request_revalidation:
        with django_capture_on_commit_callbacks(execute=True):
            update_farm(
                farm_id=farm.pk,
                actor=active_user,
                expected_version=farm.version,
                data={"name_en": "New Farm A"},
            )
        assert request_revalidation.call_args.args[0] == [
            "store-product:tomato",
            "store-products",
        ]

        request_revalidation.reset_mock()
        with django_capture_on_commit_callbacks(execute=True):
            update_farm_product(
                farm_id=farm.pk,
                product_id=product.pk,
                is_active=False,
                actor=active_user,
            )
        assert request_revalidation.call_args.args[0] == [
            "store-product:tomato",
            "store-products",
        ]


@pytest.mark.django_db
def test_quota_group_update_revalidates_linked_store_products(
    active_user, access_token, django_capture_on_commit_callbacks
):
    group = MembershipProduct.objects.create(
        name="Greens",
        short_description="",
        unit_size=Decimal("1.000"),
        unit_label_vi="kg",
        unit_label_en="kg",
        is_active=True,
    )
    MembershipProductTranslation.objects.bulk_create(
        [
            MembershipProductTranslation(
                product=group, locale="vi", name="Rau", short_description=""
            ),
            MembershipProductTranslation(
                product=group, locale="en", name="Greens", short_description=""
            ),
        ]
    )
    category_with_product(quota_group=group)
    admin_group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(admin_group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    with patch("commerce.services.request_frontend_revalidation") as request_revalidation:
        with django_capture_on_commit_callbacks(execute=True):
            response = client.patch(
                f"/api/v1/admin/membership-products/{group.pk}",
                {
                    "translations": [
                        {"locale": "vi", "name": "Rau mới", "short_description": ""},
                        {"locale": "en", "name": "New greens", "short_description": ""},
                    ]
                },
                format="json",
            )
        assert response.status_code == 200, response.json()
        assert request_revalidation.call_args.args[0] == [
            "store-product:tomato",
            "store-products",
        ]
