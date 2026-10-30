from django.db.models import Prefetch, Q

from commerce.models import PUBLIC_STORE_MAX_PRICE_VND, Category, Product
from content.serializers import media_payload
from farms.models import FarmProduct


def _public_product_filter():
    return (
        Q(
            is_active=True,
            is_published=True,
            retail_price_vnd__lte=PUBLIC_STORE_MAX_PRICE_VND,
            category__is_active=True,
        )
        & (Q(category__parent__isnull=True) | Q(category__parent__is_active=True))
        & (Q(category__parent__parent__isnull=True) | Q(category__parent__parent__is_active=True))
    )


def public_product_queryset():
    active_suppliers = FarmProduct.objects.filter(
        is_active=True, farm__is_active=True
    ).select_related("farm")
    return (
        Product.objects.filter(_public_product_filter())
        .select_related(
            "category",
            "category__parent",
            "category__parent__parent",
            "primary_image",
            "csa_quota_group",
        )
        .prefetch_related(
            "translations",
            "category__translations",
            "csa_quota_group__translations",
            Prefetch("farm_offers", queryset=active_suppliers, to_attr="public_farm_offers"),
        )
    )


def public_category_queryset():
    leaf_ids = set(public_product_queryset().values_list("category_id", flat=True).distinct())
    if not leaf_ids:
        return Category.objects.none()
    categories = list(
        Category.objects.filter(is_active=True)
        .select_related("parent__parent")
        .prefetch_related("translations")
    )
    by_id = {category.pk: category for category in categories}
    visible_ids = set()
    for category_id in leaf_ids:
        category = by_id.get(category_id)
        while category is not None and category.is_active:
            visible_ids.add(category.pk)
            category = category.parent
    return Category.objects.filter(pk__in=visible_ids).prefetch_related("translations")


def _translation(item, locale):
    by_locale = {translation.locale: translation for translation in item.translations.all()}
    selected = by_locale.get(locale) or by_locale.get("vi") or by_locale.get("en")
    return selected, sorted(by_locale)


def _ordered_public_categories():
    categories = list(public_category_queryset())
    children = {}
    for category in categories:
        children.setdefault(category.parent_id, []).append(category)
    for siblings in children.values():
        siblings.sort(key=lambda item: (item.sort_order, item.slug, str(item.pk)))
    ordered = []

    def visit(parent_id):
        for category in children.get(parent_id, []):
            ordered.append(category)
            visit(category.pk)

    visit(None)
    return ordered


def public_category_payload(category, locale):
    translation, _available_locales = _translation(category, locale)
    return {
        "id": str(category.pk),
        "parent_id": str(category.parent_id) if category.parent_id else None,
        "slug": category.slug,
        "sort_order": category.sort_order,
        "name": translation.name,
        "requested_locale": locale,
        "content_locale": translation.locale,
        "is_fallback": translation.locale != locale,
    }


def public_category_payloads(locale):
    return [public_category_payload(category, locale) for category in _ordered_public_categories()]


def _farm_name(farm, locale):
    return farm.name_vi if locale == "vi" else farm.name_en


def _farm_location(farm, locale):
    return farm.location_vi if locale == "vi" else farm.location_en


def _public_media_payload(asset, request):
    payload = media_payload(asset, request)
    return {
        "id": payload["id"],
        "url": payload["url"],
        "width": payload["width"],
        "height": payload["height"],
        "variants": [
            {"url": item["url"], "width": item["width"], "height": item["height"]}
            for item in payload["variants"]
        ],
    }


def _public_csa_quota_group_payload(product, locale):
    group = product.csa_quota_group
    if group is None or not group.is_active or product.csa_quota_cost <= 0:
        return None
    translation, _available_locales = _translation(group, locale)
    return {
        "id": str(group.pk),
        "name": translation.name if translation else group.name,
        "cost": product.csa_quota_cost,
    }


def public_product_payload(product, request, locale, *, detail=False):
    translation, available_locales = _translation(product, locale)
    alternate_locale = "en" if translation.locale == "vi" else "vi"
    alternate = next(
        (item for item in product.translations.all() if item.locale == alternate_locale), None
    )
    category_translation, _category_locales = _translation(product.category, locale)
    offers = sorted(
        product.public_farm_offers,
        key=lambda offer: (_farm_name(offer.farm, locale), str(offer.farm_id)),
    )
    suppliers = [
        {
            "id": str(offer.farm_id),
            "name": _farm_name(offer.farm, locale),
            **({"location": _farm_location(offer.farm, locale)} if detail else {}),
        }
        for offer in offers
    ]
    result = {
        "id": str(product.pk),
        "slug": product.slug,
        "sku": product.sku,
        "name": translation.name,
        "alternate_name": alternate.name if alternate else None,
        "short_description": translation.short_description,
        "retail_price_vnd": product.retail_price_vnd,
        "unit_size": str(product.unit_size),
        "unit_label": product.unit_label_vi if locale == "vi" else product.unit_label_en,
        "category": {
            "id": str(product.category_id),
            "slug": product.category.slug,
            "name": category_translation.name,
        },
        "primary_image": (
            _public_media_payload(product.primary_image, request) if product.primary_image else None
        ),
        "csa_quota_group": _public_csa_quota_group_payload(product, locale),
        "suppliers": suppliers,
        "requested_locale": locale,
        "content_locale": translation.locale,
        "available_locales": available_locales,
        "is_fallback": translation.locale != locale,
    }
    if detail:
        result["description"] = translation.description
    return result
