from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

from commerce.models import (
    PUBLIC_STORE_MAX_PRICE_VND,
    CatalogTreeLock,
    Category,
    CategoryTranslation,
    Product,
    ProductTranslation,
)
from content.models import MediaAsset
from content.services import delete_orphan_media_asset, request_frontend_revalidation
from memberships.models import Product as MembershipProduct


class CatalogConflict(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def schedule_store_revalidation(*, slugs=(), categories=False, all_details=False):
    tags = {"store-products"}
    if categories:
        tags.add("store-categories")
    slug_tags = {f"store-product:{slug}" for slug in slugs if slug}
    if all_details or len(tags | slug_tags) > 20:
        tags.add("store-product-details")
    else:
        tags.update(slug_tags)
    transaction.on_commit(lambda: request_frontend_revalidation(sorted(tags)))


def _automatic_slug(model, translations, *, fallback=""):
    names = {item["locale"]: item["name"] for item in translations}
    source = names.get("en") or names.get("vi") or fallback
    base = slugify(source.replace("đ", "d").replace("Đ", "D"))[:180].strip("-")
    if not base:
        raise ValidationError({"translations": "A name is required to create the URL."})
    candidate = base
    suffix = 2
    while model.objects.filter(slug=candidate).exists():
        marker = f"-{suffix}"
        candidate = f"{base[: 180 - len(marker)].rstrip('-')}{marker}"
        suffix += 1
    return candidate


def _get_record(model, pk, field):
    try:
        return model.objects.get(pk=pk)
    except model.DoesNotExist as error:
        raise ValidationError({field: "Record does not exist."}) from error


def _lock_category_tree():
    CatalogTreeLock.objects.select_for_update().get(pk=1)


def _translation_map(instance, submitted):
    if submitted is None:
        return {item.locale: item.name for item in instance.translations.all()} if instance else {}
    return {item["locale"]: item["name"] for item in submitted}


def _save_translations(model, parent_name, parent, submitted):
    if submitted is None:
        return
    model.objects.filter(**{parent_name: parent}).exclude(
        locale__in=[item["locale"] for item in submitted]
    ).delete()
    for item in submitted:
        model.objects.update_or_create(
            **{parent_name: parent, "locale": item["locale"]},
            defaults={key: value for key, value in item.items() if key != "locale"},
        )


def _category_depth(category):
    depth = 0
    seen = set()
    while category:
        if category.pk in seen:
            raise ValidationError({"parent_id": "Category cycle detected."})
        seen.add(category.pk)
        depth += 1
        category = category.parent
    return depth


def _subtree_height(category):
    children = Category.objects.filter(parent=category)
    return 1 + max((_subtree_height(child) for child in children), default=0)


def validate_category_parent(*, category, parent, require_active=True):
    if parent is None:
        return
    parent = Category.objects.select_for_update().get(pk=parent.pk)
    if require_active and not parent.is_active:
        raise ValidationError({"parent_id": "Parent category must be active."})
    if parent.products.exists():
        raise CatalogConflict("category_has_products")
    ancestor = parent
    while ancestor is not None:
        if category and ancestor.pk == category.pk:
            raise ValidationError({"parent_id": "A category cannot be its own descendant."})
        ancestor = ancestor.parent
    height = _subtree_height(category) if category else 1
    if _category_depth(parent) + height > 3:
        raise ValidationError({"parent_id": "Category depth cannot exceed three levels."})


@transaction.atomic
def create_category(*, actor, payload):
    _lock_category_tree()
    data = payload.copy()
    translations = data.pop("translations", [])
    parent_id = data.pop("parent_id", None)
    parent = _get_record(Category, parent_id, "parent_id") if parent_id else None
    validate_category_parent(category=None, parent=parent)
    if data.get("is_active", False) and set(_translation_map(None, translations)) != {"vi", "en"}:
        raise ValidationError({"translations": "Vietnamese and English names are required."})
    data["slug"] = data.get("slug", "").lower() or _automatic_slug(Category, translations)
    if Category.objects.filter(slug=data["slug"]).exists():
        raise ValidationError({"slug": "This slug is already in use."})
    try:
        category = Category.objects.create(parent=parent, **data)
    except IntegrityError as error:
        raise ValidationError({"slug": "This slug is already in use."}) from error
    _save_translations(CategoryTranslation, "category", category, translations)
    schedule_store_revalidation(categories=True)
    return category


@transaction.atomic
def update_category(*, actor, category, payload):
    _lock_category_tree()
    category = Category.objects.select_for_update().get(pk=category.pk)
    data = payload.copy()
    translations = data.pop("translations", None)
    parent_id = data.pop("parent_id", category.parent_id)
    parent = _get_record(Category, parent_id, "parent_id") if parent_id else None
    active = data.get("is_active", category.is_active)
    parent_changed = parent_id != category.parent_id
    validate_category_parent(
        category=category,
        parent=parent,
        require_active=active or parent_changed,
    )
    if active and set(_translation_map(category, translations)) != {"vi", "en"}:
        raise ValidationError({"translations": "Vietnamese and English names are required."})
    if not active and Category.objects.filter(parent=category, is_active=True).exists():
        raise CatalogConflict("category_has_active_children")
    if not active and Product.objects.filter(category=category, is_published=True).exists():
        raise CatalogConflict("category_has_published_products")
    if "slug" in data:
        data["slug"] = data["slug"].lower()
        if Category.objects.filter(slug=data["slug"]).exclude(pk=category.pk).exists():
            raise ValidationError({"slug": "This slug is already in use."})
    for field, value in data.items():
        setattr(category, field, value)
    category.parent = parent
    try:
        category.save()
    except IntegrityError as error:
        raise ValidationError({"slug": "This slug is already in use."}) from error
    _save_translations(CategoryTranslation, "category", category, translations)
    schedule_store_revalidation(categories=True, all_details=True)
    return category


@transaction.atomic
def delete_category(*, actor, category):
    _lock_category_tree()
    category = Category.objects.select_for_update().get(pk=category.pk)
    if category.children.exists() or category.products.exists():
        raise CatalogConflict("category_in_use")
    try:
        category.delete()
    except ProtectedError as error:
        raise CatalogConflict("category_in_use") from error
    schedule_store_revalidation(categories=True)


def _get_category_for_product(pk):
    try:
        category = Category.objects.select_for_update().get(pk=pk)
    except Category.DoesNotExist as error:
        raise ValidationError({"category_id": "Record does not exist."}) from error
    if not category.is_active or Category.objects.filter(parent=category).exists():
        raise ValidationError({"category_id": "Choose an active leaf category."})
    return category


def _get_quota_group(pk):
    if pk is None:
        return None
    try:
        group = MembershipProduct.objects.select_for_update().get(pk=pk)
    except MembershipProduct.DoesNotExist as error:
        raise ValidationError({"csa_quota_group_id": "Record does not exist."}) from error
    if not group.is_active:
        raise ValidationError({"csa_quota_group_id": "Choose an active CSA quota group."})
    return group


def _get_product_image(pk):
    if pk is None:
        return None
    image = _get_record(MediaAsset, pk, "primary_image_id")
    if not image.file.name.startswith("products/"):
        raise ValidationError({"primary_image_id": "Choose a product image."})
    return image


def _validate_product(data, *, instance=None, translations=None):
    group = data.get("csa_quota_group", instance.csa_quota_group if instance else None)
    cost = data.get("csa_quota_cost", instance.csa_quota_cost if instance else 0)
    if (group is None and cost != 0) or (group is not None and cost <= 0):
        raise ValidationError({"csa_quota_cost": "Quota cost must match CSA eligibility."})
    if group is not None and not group.is_active:
        raise ValidationError({"csa_quota_group_id": "Choose an active CSA quota group."})
    active = data.get("is_active", instance.is_active if instance else True)
    published = data.get("is_published", instance.is_published if instance else False)
    if published and not active:
        raise ValidationError({"is_active": "Published products must be active."})
    if published:
        price = data.get("retail_price_vnd", instance.retail_price_vnd if instance else 0)
        if price > PUBLIC_STORE_MAX_PRICE_VND:
            raise ValidationError(
                {"retail_price_vnd": "Published products exceed the public price limit."}
            )
        locales = set(_translation_map(instance, translations))
        if locales != {"vi", "en"}:
            raise ValidationError({"translations": "Both languages are required to publish."})
        image = data.get("primary_image", instance.primary_image if instance else None)
        if image is None:
            raise ValidationError({"primary_image_id": "An image is required to publish."})


@transaction.atomic
def create_product(*, actor, payload):
    _lock_category_tree()
    data = payload.copy()
    translations = data.pop("translations", [])
    required = {
        "sku",
        "category_id",
        "unit_size",
        "unit_label_vi",
        "unit_label_en",
        "retail_price_vnd",
    }
    missing = sorted(required - set(data))
    if missing:
        raise ValidationError({key: "This field is required." for key in missing})
    data["sku"] = data["sku"].strip().upper()
    data["slug"] = data.get("slug", "").lower() or _automatic_slug(
        Product, (), fallback=data["sku"]
    )
    data["category"] = _get_category_for_product(data.pop("category_id"))
    data["csa_quota_group"] = _get_quota_group(data.pop("csa_quota_group_id", None))
    data["primary_image"] = _get_product_image(data.pop("primary_image_id", None))
    if Product.objects.filter(sku=data["sku"]).exists():
        raise ValidationError({"sku": "This SKU is already in use."})
    if Product.objects.filter(slug=data["slug"]).exists():
        raise ValidationError({"slug": "This slug is already in use."})
    _validate_product(data, translations=translations)
    try:
        product = Product.objects.create(**data)
    except IntegrityError as error:
        raise ValidationError({"sku": "SKU or slug is already in use."}) from error
    _save_translations(ProductTranslation, "product", product, translations)
    schedule_store_revalidation(slugs=[product.slug], categories=True)
    return product


@transaction.atomic
def update_product(*, actor, product, payload):
    data = payload.copy()
    previous_slug = product.slug
    from farms.models import FarmWeeklySupply

    if (
        any(
            field in data and data[field] != getattr(product, field)
            for field in ("unit_size", "unit_label_vi", "unit_label_en")
        )
        and FarmWeeklySupply.objects.filter(farm_product__product=product).exists()
    ):
        raise CatalogConflict("product_unit_in_use")
    translations = data.pop("translations", None)
    previous_image = product.primary_image
    requested_category_id = data.pop("category_id", product.category_id)
    keeping_draft_category = (
        str(requested_category_id) == str(product.category_id)
        and not data.get("is_published", product.is_published)
        and not product.category.is_active
    )
    if not keeping_draft_category:
        category = _get_category_for_product(requested_category_id)
        if str(category.pk) != str(product.category_id):
            data["category"] = category
    if "csa_quota_group_id" in data:
        data["csa_quota_group"] = _get_quota_group(data.pop("csa_quota_group_id"))
    elif product.csa_quota_group_id:
        data["csa_quota_group"] = _get_quota_group(product.csa_quota_group_id)
    if "primary_image_id" in data:
        data["primary_image"] = _get_product_image(data.pop("primary_image_id"))
    if "sku" in data:
        data["sku"] = data["sku"].strip().upper()
        if Product.objects.filter(sku=data["sku"]).exclude(pk=product.pk).exists():
            raise ValidationError({"sku": "This SKU is already in use."})
    if "slug" in data:
        data["slug"] = data["slug"].lower()
        if Product.objects.filter(slug=data["slug"]).exclude(pk=product.pk).exists():
            raise ValidationError({"slug": "This slug is already in use."})
    _validate_product(data, instance=product, translations=translations)
    for field, value in data.items():
        setattr(product, field, value)
    try:
        product.save()
    except IntegrityError as error:
        raise ValidationError({"sku": "SKU or slug is already in use."}) from error
    _save_translations(ProductTranslation, "product", product, translations)
    if previous_image and previous_image.pk != product.primary_image_id:
        delete_orphan_media_asset(asset_id=previous_image.pk, storage_prefix="products")
    schedule_store_revalidation(slugs=[previous_slug, product.slug], categories=True)
    return product


@transaction.atomic
def delete_product(*, actor, product):
    image_id = product.primary_image_id
    slug = product.slug
    try:
        product.delete()
    except ProtectedError as error:
        raise CatalogConflict("product_in_use") from error
    if image_id:
        delete_orphan_media_asset(asset_id=image_id, storage_prefix="products")
    schedule_store_revalidation(slugs=[slug], categories=True)
