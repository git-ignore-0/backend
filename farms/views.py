from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, Subquery
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotAuthenticated, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from commerce.inventory import inventory_wire_quantity
from commerce.models import Product, ProductStatus, ProductTranslation
from config.phones import (
    InvalidPhoneNumber,
    format_vietnam_phone,
    normalize_vietnam_phone,
    phone_search,
)
from content.authentication import CoreBearerAuthentication
from content.creation_receipts import (
    CreationIdempotencyConflict,
    creation_receipt_meta,
    find_creation_receipt,
)
from content.models import AdminCreationReceipt, ContentAuditLog, MediaAsset, TrackerFarm
from content.permissions import IsWebsiteAdmin
from content.serializers import (
    TrackerFarmDeleteSerializer,
    admin_tracker_farm_payload,
    media_payload,
)
from content.services import delete_orphan_media_asset, tracker_farm_queryset
from content.views import AdminMediaUploadView, admin_tracker_write_payload
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from farms.serializers import (
    BulkStockBatchWriteSerializer,
    FarmCreationRecoverySerializer,
    FarmProductHistoryQuerySerializer,
    FarmProductLinkSerializer,
    FarmProductQuerySerializer,
    FarmProductReaddSerializer,
    FarmProductRevisionSerializer,
    FarmQuerySerializer,
    FarmSupplyListQuerySerializer,
    FarmUpdateSerializer,
    FarmWriteSerializer,
    GrowthTrackerUpdateSerializer,
    GrowthTrackerWriteSerializer,
    StockBatchCancelSerializer,
    StockBatchQuerySerializer,
    StockBatchUpdateSerializer,
    VersionedStockBatchWriteSerializer,
)
from farms.services import (
    FarmConflict,
    cancel_stock_batch,
    create_farm,
    create_growth_tracker_profile,
    create_stock_batch,
    create_stock_batches_bulk,
    delete_farm,
    delete_growth_tracker_profile,
    farm_capabilities,
    farm_product_queryset,
    farm_product_removal_blocker,
    farm_queryset,
    link_farm_products,
    readd_farm_product,
    remove_farm_product,
    stock_batch_queryset,
    update_farm,
    update_growth_tracker_profile,
    update_stock_batch,
)
from farms.stock import (
    is_current_stock_batch,
    is_historical_stock_batch,
    stock_batch_source_blocker,
    stock_batch_status,
    stock_batch_status_q,
)


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def conflict_detail(code, context=None):
    return {"code": code, **({"context": context} if context else {})}


def conflict(code, context=None):
    return no_store(
        Response({"errors": [conflict_detail(code, context)]}, status=status.HTTP_409_CONFLICT)
    )


def farm_payload(farm, request, *, detail=False):
    photos = [media_payload(photo.image, request) for photo in farm.photos.all()]
    capabilities = (
        {
            "capabilities": {
                **farm_capabilities(farm.pk),
                "can_add_supply": farm.can_add_supply,
            }
        }
        if detail
        else {}
    )
    return {
        "id": str(farm.pk),
        "contact_phone": format_vietnam_phone(farm.contact_phone),
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
        **capabilities,
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
        "product_status": row.product.status,
        "supply_status": "removed" if row.removed_at is not None else "supplying",
        "removed_at": row.removed_at.isoformat() if row.removed_at is not None else None,
        "version": row.version,
        "supply_generation": row.supply_generation,
        "initial_quantity": inventory_wire_quantity(row.initial_quantity),
        "net_sold_quantity": inventory_wire_quantity(row.net_sold_quantity),
        "available_quantity": inventory_wire_quantity(row.available_quantity),
        "batch_count": row.batch_count,
        "active_batch_count": row.active_batch_count,
        "has_supply_history": row.batch_count > 0 or row.has_legacy_supply_history,
        "next_expiry_date": row.next_expiry_date.isoformat() if row.next_expiry_date else None,
    }


def stock_batch_payload(batch, *, today=None):
    today = today or timezone.localdate()
    remaining = batch.remaining_quantity
    available = remaining if is_current_stock_batch(batch, today=today) else 0
    return {
        "id": str(batch.pk),
        "farm_product_id": str(batch.farm_product_id),
        "start_date": batch.start_date.isoformat(),
        "end_date": batch.end_date.isoformat(),
        "quantity_units": batch.quantity_units,
        "net_sold_quantity": batch.net_sold_quantity,
        "remaining_quantity": remaining,
        "available_quantity": available,
        "status": stock_batch_status(batch, today=today),
        "version": batch.version,
        "source_version": batch.farm_product.version,
        "supply_generation": batch.supply_generation,
        "is_historical": is_historical_stock_batch(batch),
        "created_at": batch.created_at.isoformat(),
        "updated_at": batch.updated_at.isoformat(),
    }


def available_product_payload(product):
    names = {item.locale: item.name for item in product.translations.all()}
    removed_source = (
        {
            "id": str(product.removed_source_id),
            "version": product.removed_source_version,
            "supply_generation": product.removed_source_generation,
        }
        if product.removed_source_id is not None
        else None
    )
    return {
        "id": str(product.pk),
        "sku": product.sku,
        "name_vi": names.get("vi") or names.get("en") or product.sku,
        "name_en": names.get("en") or names.get("vi") or product.sku,
        "unit_size": str(product.unit_size),
        "unit_label_vi": product.unit_label_vi,
        "unit_label_en": product.unit_label_en,
        "link_state": "removed" if removed_source else "unlinked",
        "removed_source": removed_source,
    }


def source_write_response(source_id, *, receipt=None, replayed=False):
    row = farm_product_queryset().filter(pk=source_id).first()
    response = {"data": farm_product_payload(row) if row is not None else None}
    if receipt is not None:
        response["meta"] = creation_receipt_meta(receipt, replayed=replayed)
    return no_store(Response(response))


def farm_creation_receipt(request, action):
    if len(request.query_params.getlist("creation_key")) > 1:
        raise ValidationError({"creation_key": "Provide one receipt key."})
    query = FarmCreationRecoverySerializer(data=request.query_params.dict())
    query.is_valid(raise_exception=True)
    return find_creation_receipt(
        actor=request.user, action=action, key=query.validated_data["creation_key"]
    )


def farm_creation_receipt_not_found():
    return no_store(Response({"errors": [{"code": "create_receipt_not_found"}]}, status=404))


def reauthorize_farm_admin(view):
    # A write can wait on locks while the token or Admin permission changes.
    credentials = CoreBearerAuthentication().authenticate(view.request)
    if credentials is None or credentials[0].pk != view.request.user.pk:
        raise NotAuthenticated()
    view.request.user, view.request.auth = credentials
    view.check_permissions(view.request)
    return view.request.user


class AdminFarmCreationView(APIView):
    permission_classes = [IsWebsiteAdmin]
    authorize_write = reauthorize_farm_admin

    def finalize_response(self, request, response, *args, **kwargs):
        return no_store(super().finalize_response(request, response, *args, **kwargs))


def source_capabilities(source, *, now):
    blockers = {}
    if batch_blocker := stock_batch_source_blocker(source):
        blockers["create_batch"] = conflict_detail(batch_blocker)
    if source.removed_at is not None:
        blockers["remove"] = conflict_detail("farm_product_removed")
        if source.product.status != ProductStatus.ACTIVE:
            blockers["readd"] = conflict_detail("product_not_selling")
    else:
        if blocker := farm_product_removal_blocker(source, now=now):
            blockers["remove"] = conflict_detail(blocker.code, blocker.context)
        blockers["readd"] = conflict_detail("farm_product_not_removed")
    return {
        "can_remove": "remove" not in blockers,
        "can_readd": "readd" not in blockers,
        "can_create_batch": "create_batch" not in blockers,
        "blockers": blockers,
    }


def source_history_payload(event):
    context = {}
    raw_context = event.context if isinstance(event.context, dict) else {}
    for field in ("farm_id", "product_id", "stock_batch_id"):
        value = raw_context.get(field)
        if isinstance(value, str):
            try:
                context[field] = str(UUID(value))
            except ValueError:
                pass
    generation = raw_context.get("supply_generation")
    if type(generation) is int and generation > 0:
        context["supply_generation"] = generation
    is_active = raw_context.get("is_active")
    if isinstance(is_active, bool):
        context["is_active"] = is_active
    if raw_context.get("provenance") == "legacy_disabled_link":
        context["provenance"] = "legacy_disabled_link"
        classified_at = raw_context.get("classified_at")
        if isinstance(classified_at, str):
            try:
                context["classified_at"] = datetime.fromisoformat(classified_at).isoformat()
            except ValueError:
                pass
    return {
        "id": str(event.pk),
        "action": event.action,
        "created_at": event.created_at.isoformat(),
        "actor": {"id": str(event.actor_id), "name": event.actor.name} if event.actor_id else None,
        "context": context,
    }


class AdminFarmListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = FarmQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = farm_queryset()
        search = query.validated_data["search"].strip()
        if search:
            phone_match = phone_search(search, "contact_phone")
            try:
                canonical_phone = normalize_vietnam_phone(search)
            except InvalidPhoneNumber:
                pass
            else:
                phone_match |= Q(contact_phone=canonical_phone)
            rows = rows.filter(
                Q(name_en__icontains=search)
                | Q(name_vi__icontains=search)
                | Q(location_en__icontains=search)
                | Q(location_vi__icontains=search)
                | phone_match
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
        farm = get_object_or_404(farm_queryset(include_supply_capability=True), pk=farm_id)
        return no_store(Response({"data": farm_payload(farm, request, detail=True)}))

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
        farm = get_object_or_404(farm_queryset(include_supply_capability=True), pk=farm.pk)
        return no_store(Response({"data": farm_payload(farm, request, detail=True)}))

    def delete(self, request, farm_id):
        try:
            delete_farm(farm_id=farm_id, actor=request.user)
        except FarmConflict as error:
            return conflict(error.code, error.context)
        except Farm.DoesNotExist as error:
            raise Http404 from error
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminFarmProductListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        query = FarmSupplyListQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = farm_product_queryset().filter(farm_id=farm_id)
        if product_status := query.validated_data.get("product_status"):
            rows = rows.filter(product__status=product_status)
        supply_status = query.validated_data["supply_status"]
        if supply_status != "all":
            rows = rows.filter(removed_at__isnull=supply_status == "supplying")
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
        return conflict("farm_product_legacy_write_disabled")


class AdminFarmAvailableProductListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        get_object_or_404(Farm, pk=farm_id)
        query = FarmProductQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        sources = FarmProduct.objects.filter(farm_id=farm_id, product_id=OuterRef("pk"))
        rows = Product.objects.filter(status=ProductStatus.ACTIVE).filter(
            ~Exists(sources.filter(removed_at__isnull=True))
        )
        search = query.validated_data["search"].strip()
        if search:
            matching_translation = ProductTranslation.objects.filter(
                product_id=OuterRef("pk"), name__icontains=search
            )
            rows = rows.filter(Q(sku__icontains=search) | Exists(matching_translation))
        page = query.validated_data["page"]
        page_size = 30
        total = rows.count()
        removed = sources.filter(removed_at__isnull=False).order_by()
        rows = (
            rows.annotate(
                removed_source_id=Subquery(removed.values("pk")[:1]),
                removed_source_version=Subquery(removed.values("version")[:1]),
                removed_source_generation=Subquery(removed.values("supply_generation")[:1]),
            )
            .prefetch_related("translations")
            .order_by("sku", "id")[(page - 1) * page_size : page * page_size]
        )
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

    def get(self, request, farm_id, product_id):
        now = timezone.now()
        source = get_object_or_404(
            farm_product_queryset(today=timezone.localdate(now)),
            farm_id=farm_id,
            product_id=product_id,
        )
        payload = farm_product_payload(source)
        payload["capabilities"] = source_capabilities(source, now=now)
        return no_store(Response({"data": payload}))

    def patch(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct, farm_id=farm_id, product_id=product_id)
        return conflict("farm_product_legacy_write_disabled")

    def delete(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct, farm_id=farm_id, product_id=product_id)
        return conflict("farm_product_legacy_write_disabled")


class AdminFarmProductRemoveView(APIView):
    permission_classes = [IsWebsiteAdmin]

    @transaction.atomic
    def post(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct.objects.only("pk"), farm_id=farm_id, product_id=product_id)
        serializer = FarmProductRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            source = remove_farm_product(
                farm_id=farm_id,
                product_id=product_id,
                actor=request.user,
                **serializer.validated_data,
            )
        except FarmConflict as error:
            return conflict(error.code, error.context)
        return source_write_response(source.pk)


class AdminFarmProductReaddView(AdminFarmCreationView):
    def get(self, request, farm_id, product_id):
        receipt = farm_creation_receipt(request, AdminCreationReceipt.Action.FARM_PRODUCT_READD)
        scope = {"farm_id": str(farm_id), "product_id": str(product_id)}
        if receipt is None or receipt.resource_scope != scope:
            return farm_creation_receipt_not_found()
        return source_write_response(receipt.resource_id, receipt=receipt, replayed=True)

    def post(self, request, farm_id, product_id):
        serializer = FarmProductReaddSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            receipt, created = readd_farm_product(
                farm_id=farm_id,
                product_id=product_id,
                actor=request.user,
                authorize_write=self.authorize_write,
                **serializer.validated_data,
            )
        except CreationIdempotencyConflict:
            return conflict("idempotency_conflict")
        except FarmConflict as error:
            return conflict(error.code, error.context)
        except (Farm.DoesNotExist, FarmProduct.DoesNotExist) as error:
            raise Http404 from error
        return source_write_response(receipt.resource_id, receipt=receipt, replayed=not created)


class AdminFarmProductHistoryView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id, product_id):
        source = get_object_or_404(
            FarmProduct.objects.only("pk"), farm_id=farm_id, product_id=product_id
        )
        query = FarmProductHistoryQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = ContentAuditLog.objects.filter(
            entity_type="farmproduct",
            entity_id=str(source.pk),
            action__in=(
                "farm.product.add",
                "farm.product.remove",
                "farm.product.readd",
                "farm.product.lifecycle_backfill",
                "farm.product.update",
                "farm.product.delete",
            ),
        )
        page = query.validated_data["page"]
        page_size = 20
        total = rows.count()
        rows = (
            rows.select_related("actor")
            .only("id", "action", "created_at", "context", "actor__id", "actor__name")
            .order_by("-created_at", "id")[(page - 1) * page_size : page * page_size]
        )
        return no_store(
            Response(
                {
                    "data": [source_history_payload(row) for row in rows],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )


class AdminFarmStockBatchListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id, product_id):
        relation = get_object_or_404(
            FarmProduct.objects.select_related("farm"), farm_id=farm_id, product_id=product_id
        )
        serializer = StockBatchQuerySerializer(data=request.query_params.dict())
        serializer.is_valid(raise_exception=True)
        today = timezone.localdate()
        rows = (
            stock_batch_queryset()
            .select_related(None)
            .filter(farm_product=relation, supply_generation__lte=relation.supply_generation)
        )
        scope = serializer.validated_data["scope"]
        if scope == "current":
            rows = (
                rows.none()
                if relation.removed_at
                else rows.filter(supply_generation=relation.supply_generation)
            )
        elif scope == "history" and relation.removed_at is None:
            rows = rows.exclude(supply_generation=relation.supply_generation)
        if requested_status := serializer.validated_data.get("status"):
            rows = rows.filter(stock_batch_status_q(requested_status, today=today))
        page = serializer.validated_data["page"]
        page_size = 20
        total = rows.count()
        rows = rows.order_by("-start_date", "-created_at", "id")[
            (page - 1) * page_size : page * page_size
        ]
        data = []
        for row in rows:
            row.farm_product = relation
            data.append(stock_batch_payload(row, today=today))
        return no_store(
            Response(
                {
                    "data": data,
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )

    @transaction.atomic
    def post(self, request, farm_id, product_id):
        get_object_or_404(FarmProduct.objects.only("pk"), farm_id=farm_id, product_id=product_id)
        serializer = VersionedStockBatchWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        expected_link_id = values.pop("expected_link_id")
        expected_version = values.pop("expected_version")
        try:
            batch = create_stock_batch(
                farm_id=farm_id,
                product_id=product_id,
                expected_link_id=expected_link_id,
                expected_version=expected_version,
                actor=request.user,
                data=values,
            )
        except FarmConflict as error:
            return conflict(error.code, error.context)
        batch = stock_batch_queryset().get(pk=batch.pk)
        return no_store(
            Response({"data": stock_batch_payload(batch)}, status=status.HTTP_201_CREATED)
        )


class AdminFarmStockBatchDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    @transaction.atomic
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
            return conflict(error.code, error.context)
        batch = stock_batch_queryset().get(pk=batch.pk)
        return no_store(Response({"data": stock_batch_payload(batch)}))


class AdminFarmStockBatchCancelView(APIView):
    permission_classes = [IsWebsiteAdmin]

    @transaction.atomic
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
            return conflict(error.code, error.context)
        batch = stock_batch_queryset().get(pk=batch.pk)
        return no_store(Response({"data": stock_batch_payload(batch)}))


class AdminFarmGrowthTrackerView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, farm_id):
        profile = get_object_or_404(tracker_farm_queryset(), farm_id=farm_id)
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
                {"data": admin_tracker_write_payload(profile, request)},
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
        except Farm.DoesNotExist as error:
            raise Http404 from error
        return no_store(Response({"data": admin_tracker_write_payload(profile, request)}))

    def delete(self, request, farm_id):
        get_object_or_404(TrackerFarm, farm_id=farm_id)
        serializer = TrackerFarmDeleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            delete_growth_tracker_profile(
                farm_id=farm_id,
                actor=request.user,
                expected_version=serializer.validated_data["version"],
            )
        except FarmConflict as error:
            return conflict(error.code)
        except Farm.DoesNotExist as error:
            raise Http404 from error
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


class AdminFarmBulkCreationView(AdminFarmCreationView):
    def get(self, request, farm_id):
        receipt = farm_creation_receipt(request, self.action)
        if receipt is None or receipt.resource_id != farm_id:
            return farm_creation_receipt_not_found()
        return self.receipt_response(receipt, created=False)

    def post(self, request, farm_id):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            receipt, created = self.write(
                farm_id=farm_id, actor=request.user, **serializer.validated_data
            )
        except CreationIdempotencyConflict:
            return conflict("idempotency_conflict")
        except FarmConflict as error:
            if error.code == "not_found":
                return no_store(
                    Response({"errors": [conflict_detail(error.code, error.context)]}, status=404)
                )
            return conflict(error.code, error.context)
        except Farm.DoesNotExist as error:
            raise Http404 from error
        return self.receipt_response(receipt, created=created)

    def receipt_response(self, receipt, *, created):
        return no_store(
            Response(
                {
                    "data": {
                        "farm_id": str(receipt.resource_id),
                        "created_count": receipt.created_count,
                    },
                    "meta": creation_receipt_meta(receipt, replayed=not created),
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )


class AdminFarmBulkStockBatchView(AdminFarmBulkCreationView):
    action = AdminCreationReceipt.Action.FARM_STOCK_BATCHES
    serializer_class = BulkStockBatchWriteSerializer
    write = staticmethod(create_stock_batches_bulk)


class AdminFarmBulkProductLinkView(AdminFarmBulkCreationView):
    action = AdminCreationReceipt.Action.FARM_PRODUCT_LINKS
    serializer_class = FarmProductLinkSerializer

    def write(self, **command):
        return link_farm_products(authorize_write=self.authorize_write, **command)
