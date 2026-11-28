from django.db.models import Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.settings import api_settings
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from commerce.models import ProductTranslation
from commerce.serializers import StrictSerializer
from content.permissions import IsWebsiteAdmin
from farms.farmer_links import (
    farm_for_token,
    farmer_link_payload,
    read_farmer_token,
    write_farmer_link,
)
from farms.models import Farm, FarmFarmerLink, FarmProduct, FarmSupplyRequest
from farms.services import FarmConflict
from farms.stock import stock_batch_source_q
from farms.supply_request_serializers import (
    FarmerHistoryQuerySerializer,
    FarmerProductListQuerySerializer,
    FarmerSupplyWriteSerializer,
    SupplyDecisionSerializer,
    SupplyRejectSerializer,
    SupplyRequestQuerySerializer,
)
from farms.supply_requests import (
    decide_supply_request,
    product_identity_fingerprint,
    submit_supply_request,
    supply_product_payload,
)
from farms.views import conflict, no_store, reauthorize_farm_admin

Status = FarmSupplyRequest.Status


def _validated(serializer_type, data):
    serializer = serializer_type(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _query(request, serializer_type=StrictSerializer):
    values = request.query_params
    repeated = {
        name: "This parameter may appear only once."
        for name, options in values.lists()
        if len(options) != 1
    }
    if repeated:
        raise ValidationError(repeated)
    return _validated(serializer_type, values.dict())


def _page(rows, page, payload):
    size = api_settings.PAGE_SIZE
    total = rows.count()
    rows = rows[(page - 1) * size : page * size]
    return Response(
        {
            "data": [payload(row) for row in rows],
            "meta": {"page": page, "page_size": size, "total": total},
        }
    )


def _entries():
    return FarmSupplyRequest.objects.select_related("farm").prefetch_related("items")


def _farm(farm):
    return {"name_vi": farm.name_vi, "name_en": farm.name_en, "is_active": farm.is_active}


def _source_payload(source):
    return {
        **supply_product_payload(source.product),
        "source_id": str(source.pk),
        "product_identity": product_identity_fingerprint(source.product),
        "supply_generation": source.supply_generation,
    }


def _item_payload(item, *, admin, approved):
    data = {**item.product_snapshot, "quantity_units": item.quantity_units}
    if admin:
        data.update(
            source_id=str(item.source_id),
            source_generation=item.source_generation,
            source_version=item.source_version,
            stock_batch_id=str(item.approved_batch_id)
            if approved and item.approved_batch_id
            else None,
        )
    return data


def _entry_payload(entry, *, admin=False):
    data = {
        "id": str(entry.pk),
        "code": entry.code,
        "status": entry.status,
        "start_date": entry.start_date.isoformat(),
        "end_date": entry.end_date.isoformat(),
        "submitted_at": entry.submitted_at.isoformat(),
        "processed_at": entry.processed_at.isoformat() if entry.processed_at else None,
        "rejection_reason": entry.rejection_reason or None,
        "items": [
            _item_payload(item, admin=admin, approved=entry.status == Status.APPROVED)
            for item in entry.items.all()
        ],
    }
    if admin:
        data.update(
            farm={"id": str(entry.farm_id), **_farm(entry.farm)},
            processed_by_id=str(entry.processed_by_id) if entry.processed_by_id else None,
        )
    return data


def _admin_entry(entry):
    return _entry_payload(entry, admin=True)


class PrivateJSONView(APIView):
    parser_classes = [JSONParser]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Referrer-Policy"] = "no-referrer"
        return no_store(response)


class FarmerIPThrottle(SimpleRateThrottle):
    rate = "120/minute"
    scope = "farm_farmer_read"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class FarmerSubmitThrottle(FarmerIPThrottle):
    rate = "15/minute"
    scope = "farm_farmer_submit"


class FarmerView(PrivateJSONView):
    authentication_classes = []
    permission_classes = []
    throttle_classes = [FarmerIPThrottle]

    def get_throttles(self):
        if self.request.method == "POST":
            return [FarmerSubmitThrottle()]
        return super().get_throttles()

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.digest = read_farmer_token(request.headers.get("X-Farm-Token"))
        self.farm = farm_for_token(self.digest)


class FarmerFarmView(FarmerView):
    def get(self, request):
        _query(request)
        return Response({"data": _farm(self.farm)})


class FarmerProductListView(FarmerView):
    def get(self, request):
        query = _query(request, FarmerProductListQuerySerializer)
        rows = (
            FarmProduct.objects.filter(stock_batch_source_q(), farm=self.farm)
            .select_related("product")
            .prefetch_related("product__translations")
        )
        if search := query["search"]:
            matching_translation = ProductTranslation.objects.filter(
                product_id=OuterRef("product_id"), name__icontains=search
            )
            rows = rows.filter(Q(product__sku__icontains=search) | Exists(matching_translation))
        return _page(rows, query["page"], _source_payload)


class FarmerSupplyListView(FarmerView):
    def get(self, request):
        query = _query(request, FarmerHistoryQuerySerializer)
        return _page(_entries().filter(farm=self.farm), query["page"], _entry_payload)

    def post(self, request):
        _query(request)
        data = _validated(FarmerSupplyWriteSerializer, request.data)
        try:
            entry, created = submit_supply_request(farm_id=self.farm.pk, digest=self.digest, **data)
        except FarmConflict as error:
            return conflict(error.code, error.context)
        return Response(
            {"data": _entry_payload(entry), "meta": {"idempotent_replay": not created}},
            status=201 if created else 200,
        )


class FarmerSupplyDetailView(FarmerView):
    def get(self, request, request_id):
        _query(request)
        entry = get_object_or_404(_entries(), farm=self.farm, pk=request_id)
        return Response({"data": _entry_payload(entry)})


class AdminSupplyView(PrivateJSONView):
    permission_classes = [IsWebsiteAdmin]

    authorize_write = reauthorize_farm_admin


class AdminFarmerLinkView(AdminSupplyView):
    rotate = False

    def get(self, request, farm_id):
        _query(request)
        link = get_object_or_404(FarmFarmerLink, farm_id=farm_id)
        try:
            return Response({"data": farmer_link_payload(link)})
        except FarmConflict as error:
            return conflict(error.code)

    def post(self, request, farm_id):
        _query(request)
        _validated(StrictSerializer, request.data)
        try:
            link, created = write_farmer_link(
                farm_id=farm_id, authorize_write=self.authorize_write, rotate=self.rotate
            )
            return Response({"data": farmer_link_payload(link)}, status=201 if created else 200)
        except (Farm.DoesNotExist, FarmFarmerLink.DoesNotExist) as error:
            raise Http404 from error
        except FarmConflict as error:
            return conflict(error.code)


class AdminSupplyListView(AdminSupplyView):
    def get(self, request):
        query = _query(request, SupplyRequestQuerySerializer)
        rows = _entries()
        if farm_id := query.get("farm_id"):
            rows = rows.filter(farm_id=farm_id)
        if state := query.get("status"):
            rows = rows.filter(status=state)
        if search := query["search"]:
            rows = rows.filter(
                Q(code__icontains=search)
                | Q(farm__name_vi__icontains=search)
                | Q(farm__name_en__icontains=search)
            )
        return _page(rows, query["page"], _admin_entry)


class AdminSupplyDetailView(AdminSupplyView):
    def get(self, request, request_id):
        _query(request)
        return Response({"data": _admin_entry(get_object_or_404(_entries(), pk=request_id))})


class AdminSupplyDecisionView(AdminSupplyView):
    outcome = Status.APPROVED

    def post(self, request, request_id):
        _query(request)
        serializer = (
            SupplyRejectSerializer if self.outcome == Status.REJECTED else SupplyDecisionSerializer
        )
        data = _validated(serializer, request.data)
        entry = get_object_or_404(FarmSupplyRequest, pk=request_id)
        try:
            entry, replayed = decide_supply_request(
                entry=entry, authorize_write=self.authorize_write, outcome=self.outcome, **data
            )
        except FarmConflict as error:
            return conflict(error.code, error.context)
        return Response({"data": _admin_entry(entry), "meta": {"idempotent_replay": replayed}})
