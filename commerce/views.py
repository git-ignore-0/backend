from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from commerce.models import Category, Product
from commerce.serializers import (
    CategoryQuerySerializer,
    CategoryWriteSerializer,
    ProductQuerySerializer,
    ProductWriteSerializer,
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
from content.models import MediaAsset
from content.permissions import IsWebsiteAdmin
from content.serializers import media_payload
from content.services import delete_orphan_media_asset
from content.views import AdminMediaUploadView


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


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
        update_product(actor=request.user, product=product, payload=serializer.validated_data)
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
