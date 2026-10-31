from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from commerce.models import Product, ProductTranslation
from content.models import MediaAsset, TrackerFarm
from content.permissions import IsWebsiteAdmin
from content.serializers import admin_tracker_farm_payload, media_payload
from content.services import delete_orphan_media_asset
from content.views import AdminMediaUploadView
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from farms.serializers import (
    FarmProductQuerySerializer,
    FarmProductUpdateSerializer,
    FarmProductWriteSerializer,
    FarmQuerySerializer,
    FarmUpdateSerializer,
    FarmWriteSerializer,
    GrowthTrackerUpdateSerializer,
    GrowthTrackerWriteSerializer,
    StockBatchCancelSerializer,
    StockBatchQuerySerializer,
    StockBatchUpdateSerializer,
    StockBatchWriteSerializer,
)
from farms.services import (
    FarmConflict,
    add_farm_product,
    cancel_stock_batch,
    create_farm,
    create_growth_tracker_profile,
    create_stock_batch,
    delete_farm,
    delete_farm_product,
    delete_growth_tracker_profile,
    farm_product_queryset,
    farm_queryset,
    update_farm,
    update_farm_product,
    update_growth_tracker_profile,
    update_stock_batch,
)
from farms.stock import stock_batch_status, stock_batch_status_q


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def conflict(code):
    return no_store(Response({"errors": [{"code": code}]}, status=status.HTTP_409_CONFLICT))


def farm_payload(farm, request):
    photos = [media_payload(photo.image, request) for photo in farm.photos.all()]
    return {
        "id": str(farm.pk),
        "name_en": farm.name_en,
        "name_vi": farm.name_vi,
        "location_en": farm.location_en,
        "location_vi": farm.location_vi,
        "description_en": farm.description_en,
        "description_vi": farm.description_vi,
        "image": media_payload(farm.image, request) if farm.image else None,
        "photos": photos,
        "is_active": farm.is_active,
        "in_stock_product_count": farm.in_stock_product_count,
        "out_of_stock_product_count": farm.out_of_stock_product_count,
        "version": farm.version,
        "created_at": farm.created_at.isoformat(),
        "updated_at": farm.updated_at.isoformat(),
    }


def farm_product_payload(row):
    names = {item.locale: item.name for item in row.product.translations.all()}
    return {
        "id": str(row.pk),
        "farm_id": str(row.farm_id),
        "product_id": str(row.product_id),
        "sku": row.product.sku,
        "name_vi": names.get("vi") or names.get("en") or row.product.sku,
        "name_en": names.get("en") or names.get("vi") or row.product.sku,
        "unit_size": str(row.product.unit_size),
        "unit_label_vi": row.product.unit_label_vi,
        "unit_label_en": row.product.unit_label_en,
        "is_active": row.is_active,
        "available_quantity": row.available_quantity,
        "batch_count": row.batch_count,
        "active_batch_count": row.active_batch_count,
        "has_supply_history": row.batch_count > 0 or row.has_legacy_supply_history,
        "next_expiry_date": row.next_expiry_date.isoformat() if row.next_expiry_date else None,
    }


def stock_batch_payload(batch):
    return {
        "id": str(batch.pk),
        "farm_product_id": str(batch.farm_product_id),
        "start_date": batch.start_date.isoformat(),
        "end_date": batch.end_date.isoformat(),
        "quantity_units": batch.quantity_units,
        "status": stock_batch_status(batch),
        "version": batch.version,
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
    }


def available_product_payload(product):
    names = {item.locale: item.name for item in product.translations.all()}
    return {
        "id": str(product.pk),
        "sku": product.sku,
        "name_vi": names.get("vi") or names.get("en") or product.sku,
        "name_en": names.get("en") or names.get("vi") or product.sku,
    }


class AdminFarmListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = FarmQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = farm_queryset()
        search = query.validated_data["search"].strip()
        if search:
            from django.db.models import Q

            rows = rows.filter(
                Q(name_en__icontains=search)
                | Q(name_vi__icontains=search)
                | Q(location_en__icontains=search)
                | Q(location_vi__icontains=search)
            )
        if "is_active" in query.validated_data:
            rows = rows.filter(is_active=query.validated_data["is_active"])
        page = query.validated_data["page"]
        page_size = 20
        count = rows.count()
        rows = rows.order_by("name_en", "id")[(page - 1) * page_size : page * page_size]
        return no_store(
            Response(
                {
                    "data": [farm_payload(row, request) for row in rows],
                    "meta": {"page": page, "page_size": page_size, "total": count},
                }
            )
        )

    def post(self, request):
        serializer = FarmWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        farm = create_farm(actor=request.user, data=serializer.validated_data)
        farm = get_object_or_404(farm_queryset(), pk=farm.pk)
        return no_store(
            Response({"data": farm_payload(farm, request)}, status=status.HTTP_201_CREATED)
        )


class AdminFarmDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        farm = get_object_or_404(farm_queryset(), pk=farm_id)
        return no_store(Response({"data": farm_payload(farm, request)}))

    def patch(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        serializer = FarmUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        version = data.pop("version")
        try:
            farm = update_farm(
                farm_id=farm_id, actor=request.user, expected_version=version, data=data
            )
        except FarmConflict as error:
            return conflict(error.code)
        except Farm.DoesNotExist as error:
            raise Http404 from error
        farm = get_object_or_404(farm_queryset(), pk=farm.pk)
        return no_store(Response({"data": farm_payload(farm, request)}))

    def delete(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        try:
            delete_farm(farm_id=farm_id, actor=request.user)
        except FarmConflict as error:
            return conflict(error.code)
        except Farm.DoesNotExist as error:
            raise Http404 from error
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminFarmProductListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        query = FarmProductQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = farm_product_queryset().filter(farm_id=farm_id)
        search = query.validated_data["search"].strip()
        if search:
            matching_translation = ProductTranslation.objects.filter(
                product_id=OuterRef("product_id"),
                name__icontains=search,
            )
            rows = rows.filter(Q(product__sku__icontains=search) | Exists(matching_translation))
        page = query.validated_data["page"]
        page_size = 20
        total = rows.count()
        rows = rows.order_by("product__sku", "id")[(page - 1) * page_size : page * page_size]
        return no_store(
            Response(
                {
                    "data": [farm_product_payload(row) for row in rows],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )

    def post(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        serializer = FarmProductWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        product_id = data.pop("product_id")
        get_object_or_404(Product, pk=product_id)
        try:
            row = add_farm_product(
                farm_id=farm_id,
                product_id=product_id,
                actor=request.user,
                **data,
            )
        except FarmConflict as error:
            return conflict(error.code)
        except (Farm.DoesNotExist, Product.DoesNotExist) as error:
            raise Http404 from error
        row = farm_product_queryset().filter(pk=row.pk).first()
        if row is None:
            return conflict("farm_product_changed")
        return no_store(
            Response({"data": farm_product_payload(row)}, status=status.HTTP_201_CREATED)
        )


class AdminFarmAvailableProductListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        query = FarmProductQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = Product.objects.filter(is_active=True).exclude(farm_offers__farm_id=farm_id)
        search = query.validated_data["search"].strip()
        if search:
            rows = rows.filter(
                Q(sku__icontains=search) | Q(translations__name__icontains=search)
            ).distinct()
        page = query.validated_data["page"]
        page_size = 30
        total = rows.count()
        rows = rows.prefetch_related("translations").order_by("sku", "id")[
            (page - 1) * page_size : page * page_size
        ]
        return no_store(
            Response(
                {
                    "data": [available_product_payload(product) for product in rows],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )


class AdminFarmProductDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def patch(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct, farm_id=farm_id, product_id=product_id)
        serializer = FarmProductUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            row = update_farm_product(
                farm_id=farm_id,
                product_id=product_id,
                is_active=serializer.validated_data["is_active"],
                actor=request.user,
            )
        except FarmConflict as error:
            return conflict(error.code)
        row = farm_product_queryset().filter(pk=row.pk).first()
        if row is None:
            return conflict("farm_product_changed")
        return no_store(Response({"data": farm_product_payload(row)}))

    def delete(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct, farm_id=farm_id, product_id=product_id)
        try:
            delete_farm_product(farm_id=farm_id, product_id=product_id, actor=request.user)
        except FarmConflict as error:
            return conflict(error.code)
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminFarmStockBatchListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id, product_id):
        relation = get_object_or_404(FarmProduct, farm_id=farm_id, product_id=product_id)
        serializer = StockBatchQuerySerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        rows = FarmProductStockBatch.objects.filter(farm_product=relation)
        if requested_status := serializer.validated_data.get("status"):
            rows = rows.filter(stock_batch_status_q(requested_status))
        page = serializer.validated_data["page"]
        page_size = 20
        total = rows.count()
        rows = rows.select_related("farm_product").order_by("-start_date", "-created_at", "id")[
            (page - 1) * page_size : page * page_size
        ]
        return no_store(
            Response(
                {
                    "data": [stock_batch_payload(row) for row in rows],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )

    def post(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct, farm_id=farm_id, product_id=product_id)
        serializer = StockBatchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            batch = create_stock_batch(
                farm_id=farm_id,
                product_id=product_id,
                actor=request.user,
                data=serializer.validated_data,
            )
        except FarmConflict as error:
            return conflict(error.code)
        return no_store(
            Response({"data": stock_batch_payload(batch)}, status=status.HTTP_201_CREATED)
        )


class AdminFarmStockBatchDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def patch(self, request, farm_id, product_id, batch_id):
        get_object_or_404(
            FarmProductStockBatch,
            pk=batch_id,
            farm_product__farm_id=farm_id,
            farm_product__product_id=product_id,
        )
        serializer = StockBatchUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        version = data.pop("version")
        try:
            batch = update_stock_batch(
                farm_id=farm_id,
                product_id=product_id,
                batch_id=batch_id,
                actor=request.user,
                expected_version=version,
                data=data,
            )
        except FarmConflict as error:
            return conflict(error.code)
        return no_store(Response({"data": stock_batch_payload(batch)}))


class AdminFarmStockBatchCancelView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, farm_id, product_id, batch_id):
        get_object_or_404(
            FarmProductStockBatch,
            pk=batch_id,
            farm_product__farm_id=farm_id,
            farm_product__product_id=product_id,
        )
        serializer = StockBatchCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            batch = cancel_stock_batch(
                farm_id=farm_id,
                product_id=product_id,
                batch_id=batch_id,
                actor=request.user,
                expected_version=serializer.validated_data["version"],
            )
        except FarmConflict as error:
            return conflict(error.code)
        return no_store(Response({"data": stock_batch_payload(batch)}))


class AdminFarmGrowthTrackerView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        profile = get_object_or_404(
            TrackerFarm.objects.select_related("farm__image"), farm_id=farm_id
        )
        return no_store(Response({"data": admin_tracker_farm_payload(profile, request)}))

    def post(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        serializer = GrowthTrackerWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            profile = create_growth_tracker_profile(
                farm_id=farm_id, actor=request.user, data=serializer.validated_data
            )
        except FarmConflict as error:
            return conflict(error.code)
        except Farm.DoesNotExist as error:
            raise Http404 from error
        return no_store(
            Response(
                {"data": admin_tracker_farm_payload(profile, request)},
                status=status.HTTP_201_CREATED,
            )
        )

    def patch(self, request, farm_id):
        get_object_or_404(TrackerFarm, farm_id=farm_id)
        serializer = GrowthTrackerUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        version = data.pop("version")
        try:
            profile = update_growth_tracker_profile(
                farm_id=farm_id, actor=request.user, expected_version=version, data=data
            )
        except FarmConflict as error:
            return conflict(error.code)
        return no_store(Response({"data": admin_tracker_farm_payload(profile, request)}))

    def delete(self, request, farm_id):
        get_object_or_404(TrackerFarm, farm_id=farm_id)
        try:
            delete_growth_tracker_profile(farm_id=farm_id, actor=request.user)
        except FarmConflict as error:
            return conflict(error.code)
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminFarmMediaUploadView(AdminMediaUploadView):
    storage_prefix = "farms"
    audit_action = "farm.media.upload"

    def post(self, request):
        return no_store(super().post(request))


class AdminFarmMediaDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def delete(self, request, media_id):
        get_object_or_404(MediaAsset, pk=media_id)
        if not delete_orphan_media_asset(asset_id=media_id, storage_prefix="farms"):
            return conflict("media_in_use_or_invalid")
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))
