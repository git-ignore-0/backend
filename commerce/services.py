from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import F
from django.db.models.deletion import ProtectedError
from django.utils.text import slugify
from rest_framework.exceptions import ValidationError

from commerce.models import (
    PUBLIC_STORE_MAX_PRICE_VND,
    CatalogTreeLock,
    Category,
    CategoryTranslation,
    Product,
    ProductStatus,
    ProductTranslation,
    StoreOrderLine,
)
from commerce.product_descriptions import prepare_product_translations
from content.models import MediaAsset
from content.services import audit, delete_orphan_media_asset, request_frontend_revalidation
from memberships.models import Product as MembershipProduct
from memberships.quota_points import POINT_INPUT_ERROR, is_allowed_point_input


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
    if (
        not active
        and Product.objects.filter(category=category, status=ProductStatus.ACTIVE).exists()
    ):
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


def _get_category_for_product(pk, *, selling=False):
    try:
        category = Category.objects.select_for_update().get(pk=pk)
    except Category.DoesNotExist as error:
        raise ValidationError({"category_id": "Record does not exist."}) from error
    if not category.is_active or Category.objects.filter(parent=category).exists():
        raise ValidationError({"category_id": "Choose an active leaf category."})
    if selling:
        ancestor = category.parent
        while ancestor:
            if not ancestor.is_active:
                raise ValidationError({"category_id": "Choose a category with active ancestors."})
            ancestor = ancestor.parent
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


def _validate_product(data, *, instance=None, translations=None, selling=False):
    group = data.get("csa_quota_group", instance.csa_quota_group if instance else None)
    cost = data.get("csa_quota_cost", instance.csa_quota_cost if instance else 0)
    try:
        cost = Decimal(str(cost))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValidationError({"csa_quota_cost": POINT_INPUT_ERROR}) from error
    if not cost.is_finite() or (cost != 0 and not is_allowed_point_input(cost)):
        raise ValidationError({"csa_quota_cost": POINT_INPUT_ERROR})
    if (group is None and cost != 0) or (group is not None and cost <= 0):
        raise ValidationError({"csa_quota_cost": "Quota cost must match CSA eligibility."})
    if group is not None and not group.is_active:
        raise ValidationError({"csa_quota_group_id": "Choose an active CSA quota group."})
    if selling:
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


def legacy_product_flags(status):
    return {
        "is_active": status != ProductStatus.DISCONTINUED,
        "is_published": status == ProductStatus.ACTIVE,
    }


def _check_product_write_status(data, status):
    if "status" in data:
        raise CatalogConflict("product_transition_required")
    # Old editors echo the flags with unrelated form edits. A stale form
    # cannot reliably express an intentional transition.
    for name, expected in legacy_product_flags(status).items():
        if data.pop(name, expected) != expected:
            raise CatalogConflict("product_transition_required")


@transaction.atomic
def create_product(*, actor, payload):
    _lock_category_tree()
    data = payload.copy()
    _check_product_write_status(data, ProductStatus.DRAFT)
    translations = prepare_product_translations(data.pop("translations", []))
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


PRODUCT_IDENTITY_FIELDS = ("sku", "unit_size", "unit_label_vi", "unit_label_en")
PRODUCT_CSA_FIELDS = ("csa_quota_group_id", "csa_quota_cost")


def _product_stock_holds(product):
    from memberships.csa_rounds import active_stock_holds
    from memberships.models import CSAStockHold

    return CSAStockHold.objects.filter(product=product, selection__in=active_stock_holds())


def _identity_lock_references(product):
    from farms.models import FarmProductStockBatch, LegacyFarmWeeklySupply

    reasons = []
    for code, model in (
        ("stock_batch", FarmProductStockBatch),
        ("legacy_supply", LegacyFarmWeeklySupply),
    ):
        ref = (
            model.objects.filter(farm_product__product=product)
            .order_by("pk")
            .values(
                "id",
                farm_id=F("farm_product__farm_id"),
                farm_name_vi=F("farm_product__farm__name_vi"),
                farm_name_en=F("farm_product__farm__name_en"),
            )
            .first()
        )
        if ref:
            reasons.append(
                {**ref, "code": code, "id": str(ref["id"]), "farm_id": str(ref["farm_id"])}
            )
    order = (
        StoreOrderLine.objects.filter(product=product)
        .order_by("pk")
        .values("order_id", "order__reference_code")
        .first()
    )
    if order:
        reasons.append(
            {
                "code": "store_order",
                "id": str(order["order_id"]),
                "label": order["order__reference_code"],
            }
        )
    return reasons


def _csa_round_reason(rows, code):
    ref = rows.order_by("pk").values("selection__round_id", "selection__round__name").first()
    if ref is None:
        return None
    return {
        "code": code,
        "id": str(ref["selection__round_id"]),
        "label": ref["selection__round__name"],
    }


def product_capabilities(product):
    hold = _csa_round_reason(_product_stock_holds(product), "csa_hold")
    csa = [hold] if hold else []
    identity = _identity_lock_references(product)
    deletion = [*identity, *csa]
    offer = (
        product.farm_offers.order_by("pk")
        .values("id", "farm_id", farm_name_vi=F("farm__name_vi"), farm_name_en=F("farm__name_en"))
        .first()
    )
    if offer:
        deletion.append(
            {**offer, "code": "farm_link", "id": str(offer["id"]), "farm_id": str(offer["farm_id"])}
        )
    if not csa:
        history = _csa_round_reason(
            product.csaselectionline_set, "csa_history"
        ) or _csa_round_reason(product.csastockhold_set, "csa_history")
        if history:
            deletion.append(history)
    if not deletion:
        reservation = (
            product.store_stock_reservations.order_by("pk")
            .values("line__order_id", "line__order__reference_code")
            .first()
        )
        if reservation:
            deletion.append(
                {
                    "code": "store_reservation",
                    "id": str(reservation["line__order_id"]),
                    "label": reservation["line__order__reference_code"],
                }
            )
    return {
        "edit_identity": {"allowed": not (identity or csa), "reasons": [*identity, *csa]},
        "edit_csa": {"allowed": not csa, "reasons": csa},
        "delete": {"allowed": not deletion, "reasons": deletion},
    }


@transaction.atomic
def update_product(*, actor, product, payload):
    _lock_category_tree()
    product = Product.objects.select_for_update().get(pk=product.pk)
    data = payload.copy()
    _check_product_write_status(data, product.status)
    previous_slug = product.slug
    if "sku" in data:
        data["sku"] = data["sku"].strip().upper()
    changes_identity = any(
        field in data and data[field] != getattr(product, field)
        for field in PRODUCT_IDENTITY_FIELDS
    )
    changes_csa = any(
        field in data and data[field] != getattr(product, field) for field in PRODUCT_CSA_FIELDS
    )
    if (changes_identity or changes_csa) and _product_stock_holds(product).exists():
        raise CatalogConflict("product_csa_hold_in_use")
    if changes_identity and _identity_lock_references(product):
        raise CatalogConflict("product_unit_in_use")
    translations = prepare_product_translations(data.pop("translations", None), instance=product)
    previous_image = product.primary_image
    requested_category_id = data.pop("category_id", product.category_id)
    keeping_draft_category = (
        str(requested_category_id) == str(product.category_id)
        and product.status != ProductStatus.ACTIVE
        and not product.category.is_active
    )
    if not keeping_draft_category:
        category = _get_category_for_product(
            requested_category_id, selling=product.status == ProductStatus.ACTIVE
        )
        if str(category.pk) != str(product.category_id):
            data["category"] = category
    if "csa_quota_group_id" in data:
        data["csa_quota_group"] = _get_quota_group(data.pop("csa_quota_group_id"))
    elif product.csa_quota_group_id:
        data["csa_quota_group"] = _get_quota_group(product.csa_quota_group_id)
    if "primary_image_id" in data:
        data["primary_image"] = _get_product_image(data.pop("primary_image_id"))
    if "sku" in data:
        if Product.objects.filter(sku=data["sku"]).exclude(pk=product.pk).exists():
            raise ValidationError({"sku": "This SKU is already in use."})
    if "slug" in data:
        data["slug"] = data["slug"].lower()
        if Product.objects.filter(slug=data["slug"]).exclude(pk=product.pk).exists():
            raise ValidationError({"slug": "This slug is already in use."})
    _validate_product(
        data,
        instance=product,
        translations=translations,
        selling=product.status == ProductStatus.ACTIVE,
    )
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
def transition_product(*, actor, product_id, expected_status, target_status):
    _lock_category_tree()
    product = Product.objects.select_for_update().get(pk=product_id)
    if product.status != expected_status:
        raise CatalogConflict("product_status_changed")
    transitions = {
        ProductStatus.DRAFT: ProductStatus.ACTIVE,
        ProductStatus.ACTIVE: ProductStatus.DISCONTINUED,
        ProductStatus.DISCONTINUED: ProductStatus.ACTIVE,
    }
    if transitions.get(product.status) != target_status:
        raise CatalogConflict("product_transition_invalid")
    if target_status == ProductStatus.ACTIVE:
        _get_category_for_product(product.category_id, selling=True)
        group = _get_quota_group(product.csa_quota_group_id)
        image = _get_product_image(product.primary_image_id)
        _validate_product(
            {"csa_quota_group": group, "primary_image": image}, instance=product, selling=True
        )
    product.status = target_status
    legacy_flags = legacy_product_flags(target_status)
    for field, value in legacy_flags.items():
        setattr(product, field, value)
    product.save(update_fields=["status", *legacy_flags, "updated_at"])
    audit(
        actor=actor,
        action="store_product.transition",
        entity=product,
        context={"from": expected_status, "to": target_status},
    )
    schedule_store_revalidation(slugs=[product.slug], categories=True)
    return product


@transaction.atomic
def delete_product(*, actor, product):
    product = Product.objects.select_for_update().get(pk=product.pk)
    image_id = product.primary_image_id
    slug = product.slug
    try:
        product.delete()
    except ProtectedError as error:
        raise CatalogConflict("product_in_use") from error
    if image_id:
        delete_orphan_media_asset(asset_id=image_id, storage_prefix="products")
    schedule_store_revalidation(slugs=[slug], categories=True)
