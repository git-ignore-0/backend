from django.db import transaction
from django.db.models import CharField, Count, Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from commerce.models import Category, Product, ProductTranslation, ShippingAddress
from commerce.public_catalog import (
    public_category_payloads,
    public_product_payload,
    public_product_queryset,
)
from commerce.serializers import (
    CategoryQuerySerializer,
    CategoryWriteSerializer,
    ProductQuerySerializer,
    ProductWriteSerializer,
    PublicStoreLocaleSerializer,
    PublicStoreProductQuerySerializer,
    ShippingAddressSerializer,
    ShippingLocaleSerializer,
    ShippingQuoteSerializer,
    ShippingSettingsSerializer,
)
from commerce.services import (
    CatalogConflict,
    create_category,
    create_product,
    delete_category,
    delete_product,
    update_category,
    update_product,
)
from commerce.shipping import (
    UnsupportedShippingArea,
    active_shipping_area,
    public_shipping_area_payloads,
    save_shipping_address,
    save_shipping_settings,
    shipping_address_payload,
    shipping_settings_payload,
)
from content.models import MediaAsset
from content.permissions import IsWebsiteAdmin
from content.serializers import media_payload
from content.services import delete_orphan_media_asset
from content.views import AdminMediaUploadView
from members.administrative_units import province_exists
from memberships.permissions import HasMembershipSelfScopes


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def public_cache(response):
    response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return response


class PublicStoreCategoryListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        query = PublicStoreLocaleSerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        return public_cache(
            Response({"data": public_category_payloads(query.validated_data["locale"])})
        )


class PublicStoreProductListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        query = PublicStoreProductQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        data = query.validated_data
        rows = public_product_queryset()
        if search := data["search"]:
            rows = rows.filter(
                Q(sku__icontains=search)
                | Q(slug__icontains=search)
                | Q(translations__name__icontains=search)
                | Q(category__translations__name__icontains=search)
                | Q(
                    farm_offers__is_active=True,
                    farm_offers__farm__is_active=True,
                    farm_offers__farm__name_vi__icontains=search,
                )
                | Q(
                    farm_offers__is_active=True,
                    farm_offers__farm__is_active=True,
                    farm_offers__farm__name_en__icontains=search,
                )
            ).distinct()
        if category_id := data.get("category_id"):
            rows = rows.filter(
                Q(category_id=category_id)
                | Q(category__parent_id=category_id)
                | Q(category__parent__parent_id=category_id)
            )
        if "csa_eligible" in data:
            eligible = Q(
                csa_quota_group__isnull=False,
                csa_quota_group__is_active=True,
                csa_quota_cost__gt=0,
            )
            rows = rows.filter(eligible if data["csa_eligible"] else ~eligible)
        if "price_min" in data:
            rows = rows.filter(retail_price_vnd__gte=data["price_min"])
        if "price_max" in data:
            rows = rows.filter(retail_price_vnd__lte=data["price_max"])
        ordering = {
            "default": ("-updated_at", "id"),
            "price_asc": ("retail_price_vnd", "id"),
            "price_desc": ("-retail_price_vnd", "id"),
            "name": ("localized_name", "id"),
        }[data["sort"]]
        if data["sort"] == "name":
            fallback_locale = "vi" if data["locale"] == "en" else "en"
            requested_name = ProductTranslation.objects.filter(
                product_id=OuterRef("pk"), locale=data["locale"]
            ).values("name")[:1]
            fallback_name = ProductTranslation.objects.filter(
                product_id=OuterRef("pk"), locale=fallback_locale
            ).values("name")[:1]
            rows = rows.annotate(
                localized_name=Coalesce(
                    Subquery(requested_name),
                    Subquery(fallback_name),
                    output_field=CharField(),
                )
            )
        rows = rows.order_by(*ordering)
        total = rows.count()
        page = data["page"]
        page_size = data["page_size"]
        items = rows[(page - 1) * page_size : page * page_size]
        return no_store(
            Response(
                {
                    "data": [
                        public_product_payload(item, request, data["locale"]) for item in items
                    ],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )


class PublicStoreProductDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, slug):
        query = PublicStoreLocaleSerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        item = get_object_or_404(public_product_queryset(), slug=slug)
        return no_store(
            Response(
                {
                    "data": public_product_payload(
                        item, request, query.validated_data["locale"], detail=True
                    )
                }
            )
        )


class PublicShippingAreaListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = ShippingLocaleSerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        return public_cache(
            Response({"data": public_shipping_area_payloads(query.validated_data["locale"])})
        )


class PublicShippingQuoteView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        serializer = ShippingQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        area = active_shipping_area(**serializer.validated_data)
        return no_store(
            Response(
                {
                    "data": {
                        "supported": area is not None,
                        "shipping_fee_vnd": str(area.shipping_fee_vnd) if area else None,
                    }
                }
            )
        )


class SelfShippingAddressView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        item = ShippingAddress.objects.filter(user=request.user).first()
        return no_store(Response({"data": shipping_address_payload(item) if item else None}))

    def put(self, request):
        serializer = ShippingAddressSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = save_shipping_address(user=request.user, data=serializer.validated_data)
        except UnsupportedShippingArea:
            return no_store(
                Response(
                    {
                        "errors": [
                            {
                                "code": "shipping_area_unsupported",
                                "detail": "The selected shipping area is not supported.",
                            }
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        return no_store(Response({"data": shipping_address_payload(item)}))


class AdminShippingSettingsView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, province_code):
        if not province_exists(province_code):
            raise NotFound
        return no_store(Response({"data": shipping_settings_payload(province_code=province_code)}))

    def put(self, request, province_code):
        if not province_exists(province_code):
            raise NotFound
        serializer = ShippingSettingsSerializer(
            data=request.data,
            context={"province_code": province_code},
        )
        serializer.is_valid(raise_exception=True)
        payload = save_shipping_settings(
            province_code=province_code,
            data=serializer.validated_data,
        )
        return no_store(Response({"data": payload}))


def category_queryset():
    return (
        Category.objects.select_related("parent__parent")
        .prefetch_related("translations")
        .annotate(
            has_children=Exists(Category.objects.filter(parent_id=OuterRef("pk"))),
            product_count=Count("products", distinct=True),
        )
        .order_by("sort_order", "slug", "id")
    )


def product_queryset():
    return Product.objects.select_related(
        "category", "csa_quota_group", "primary_image"
    ).prefetch_related("translations", "csa_quota_group__translations")


def _translations(item):
    return [
        {
            "locale": translation.locale,
            "name": translation.name,
            "description": translation.description,
            **(
                {"short_description": translation.short_description}
                if hasattr(translation, "short_description")
                else {}
            ),
        }
        for translation in item.translations.all()
    ]


def _category_depth(category):
    return (
        1
        + (1 if category.parent_id else 0)
        + (1 if category.parent_id and category.parent.parent_id else 0)
    )


def category_payload(item):
    return {
        "id": str(item.pk),
        "parent_id": str(item.parent_id) if item.parent_id else None,
        "slug": item.slug,
        "sort_order": item.sort_order,
        "is_active": item.is_active,
        "depth": _category_depth(item),
        "has_children": item.has_children
        if hasattr(item, "has_children")
        else item.children.exists(),
        "product_count": (
            item.product_count if hasattr(item, "product_count") else item.products.count()
        ),
        "translations": _translations(item),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def product_payload(item, request):
    group = item.csa_quota_group
    return {
        "id": str(item.pk),
        "category_id": str(item.category_id),
        "category_slug": item.category.slug,
        "sku": item.sku,
        "slug": item.slug,
        "unit_size": str(item.unit_size),
        "unit_label_vi": item.unit_label_vi,
        "unit_label_en": item.unit_label_en,
        "retail_price_vnd": str(item.retail_price_vnd),
        "csa_quota_group_id": str(item.csa_quota_group_id) if group else None,
        "csa_quota_group": (
            {
                "id": str(group.pk),
                "name": group.name,
                "translations": [
                    {
                        "locale": translation.locale,
                        "name": translation.name,
                        "short_description": translation.short_description,
                    }
                    for translation in group.translations.all()
                ],
            }
            if group
            else None
        ),
        "csa_quota_cost": item.csa_quota_cost,
        "primary_image_id": str(item.primary_image_id) if item.primary_image_id else None,
        "primary_image": media_payload(item.primary_image, request) if item.primary_image else None,
        "is_active": item.is_active,
        "is_published": item.is_published,
        "translations": _translations(item),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def conflict_response(error):
    return no_store(Response({"errors": [{"code": error.code}]}, status=status.HTTP_409_CONFLICT))


class AdminCategoryViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]
    lookup_value_converter = "uuid"

    def list(self, request):
        query = CategoryQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = category_queryset()
        if search := query.validated_data["search"]:
            rows = rows.filter(
                Q(slug__icontains=search) | Q(translations__name__icontains=search)
            ).distinct()
        if "is_active" in query.validated_data:
            rows = rows.filter(is_active=query.validated_data["is_active"])
        return no_store(Response({"data": [category_payload(item) for item in rows]}))

    def create(self, request):
        serializer = CategoryWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            category = create_category(actor=request.user, payload=serializer.validated_data)
        except CatalogConflict as error:
            return conflict_response(error)
        item = category_queryset().get(pk=category.pk)
        return no_store(Response({"data": category_payload(item)}, status=status.HTTP_201_CREATED))

    def retrieve(self, request, pk=None):
        item = get_object_or_404(category_queryset(), pk=pk)
        return no_store(Response({"data": category_payload(item)}))

    def partial_update(self, request, pk=None):
        category = get_object_or_404(Category, pk=pk)
        serializer = CategoryWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            update_category(
                actor=request.user, category=category, payload=serializer.validated_data
            )
        except CatalogConflict as error:
            return conflict_response(error)
        return no_store(Response({"data": category_payload(category_queryset().get(pk=pk))}))

    def destroy(self, request, pk=None):
        category = get_object_or_404(Category, pk=pk)
        try:
            delete_category(actor=request.user, category=category)
        except CatalogConflict as error:
            return conflict_response(error)
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminProductViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]
    lookup_value_converter = "uuid"

    def list(self, request):
        query = ProductQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        data = query.validated_data
        rows = product_queryset()
        if search := data["search"]:
            rows = rows.filter(
                Q(sku__icontains=search)
                | Q(slug__icontains=search)
                | Q(translations__name__icontains=search)
                | Q(translations__short_description__icontains=search)
            ).distinct()
        if "category_id" in data:
            category_id = data["category_id"]
            rows = rows.filter(
                Q(category_id=category_id)
                | Q(category__parent_id=category_id)
                | Q(category__parent__parent_id=category_id)
            )
        if "is_active" in data:
            rows = rows.filter(is_active=data["is_active"])
        if "is_published" in data:
            rows = rows.filter(is_published=data["is_published"])
        if "csa_eligible" in data:
            rows = rows.filter(csa_quota_group__isnull=not data["csa_eligible"])
        total = rows.count()
        page = data["page"]
        page_size = 30
        items = rows[(page - 1) * page_size : page * page_size]
        return no_store(
            Response(
                {
                    "data": [product_payload(item, request) for item in items],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )

    def create(self, request):
        serializer = ProductWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = create_product(actor=request.user, payload=serializer.validated_data)
        item = product_queryset().get(pk=product.pk)
        return no_store(
            Response({"data": product_payload(item, request)}, status=status.HTTP_201_CREATED)
        )

    def retrieve(self, request, pk=None):
        item = get_object_or_404(product_queryset(), pk=pk)
        return no_store(Response({"data": product_payload(item, request)}))

    @transaction.atomic
    def partial_update(self, request, pk=None):
        product = get_object_or_404(Product.objects.select_for_update(), pk=pk)
        serializer = ProductWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            update_product(actor=request.user, product=product, payload=serializer.validated_data)
        except CatalogConflict as error:
            return conflict_response(error)
        return no_store(Response({"data": product_payload(product_queryset().get(pk=pk), request)}))

    def destroy(self, request, pk=None):
        product = get_object_or_404(Product, pk=pk)
        try:
            delete_product(actor=request.user, product=product)
        except CatalogConflict as error:
            return conflict_response(error)
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminProductMediaUploadView(AdminMediaUploadView):
    storage_prefix = "products"
    audit_action = "store_product.media.upload"

    def post(self, request):
        return no_store(super().post(request))


class AdminProductMediaDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def delete(self, request, media_id):
        image = MediaAsset.objects.filter(pk=media_id).first()
        if image is None or not image.file.name.startswith("products/"):
            raise NotFound()
        if not delete_orphan_media_asset(asset_id=media_id, storage_prefix="products"):
            return conflict_response(CatalogConflict("product_media_in_use"))
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))
