import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db.models import Prefetch, Q, prefetch_related_objects
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from commerce.models import (
    StoreGuestLookupSession,
    StoreOrder,
    StoreOrderLine,
    StoreStockAllocation,
)
from commerce.public_catalog import public_product_payload, public_product_queryset
from commerce.serializers import (
    PublicStoreLocaleSerializer,
    StoreActionSerializer,
    StoreApproveSerializer,
    StoreCancelSerializer,
    StoreCheckoutSerializer,
    StoreGuestLookupSerializer,
    StoreOrderAccessSerializer,
    StoreOrderCancelAccessSerializer,
    StoreOrderQuerySerializer,
    StoreRecordPaymentSerializer,
    StoreRefundSerializer,
    StoreRestockSerializer,
    StoreVoucherPreviewSerializer,
)
from commerce.store_orders import (
    StoreOrderError,
    approve_store_order,
    cancel_pending_store_order,
    cancel_store_order,
    create_store_checkout,
    deliver_store_order,
    expire_order_if_needed,
    preview_store_voucher,
    record_store_payment,
    refund_store_order,
    report_store_transfer,
    restock_store_order,
    restock_unavailable_reason,
    source_inventory,
    verify_order_access,
)
from commerce.store_proxy import trusted_store_client_ip
from content.permissions import IsWebsiteAdmin
from memberships.coupons import CouponError
from memberships.permissions import HasMembershipSelfScopes


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def store_error(error):
    if error.code == "store_payment_unavailable":
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif error.code == "store_order_not_found":
        http_status = status.HTTP_404_NOT_FOUND
    elif error.code in {
        "store_idempotency_conflict",
        "store_stock_changed",
        "store_quota_changed",
        "store_order_expired",
        "store_order_state_conflict",
        "store_allocation_stale",
        "store_allocation_incomplete",
        "store_allocation_duplicate",
        "store_payment_state_conflict",
        "store_refund_state_conflict",
        "store_restock_state_conflict",
        "store_restock_invalid",
        "store_restock_duplicate",
        "store_requote_required",
    }:
        http_status = status.HTTP_409_CONFLICT
    else:
        http_status = status.HTTP_400_BAD_REQUEST
    payload = {"code": error.code}
    if getattr(error, "context", None):
        payload["context"] = error.context
    return no_store(Response({"errors": [payload]}, status=http_status))


def public_store_scope_error(request):
    if not request.user or not request.user.is_authenticated:
        return None
    token = request.auth
    if token and {"profile", "email"}.issubset(set(token.scope.split())):
        return None
    response = no_store(
        Response({"errors": [{"code": "insufficient_scope"}]}, status=status.HTTP_403_FORBIDDEN)
    )
    response["WWW-Authenticate"] = 'Bearer error="insufficient_scope", scope="profile email"'
    return response


def _line_payload(line, request=None):
    return {
        "id": str(line.pk),
        "product_id": str(line.product_id),
        "image_url": (
            request.build_absolute_uri(line.product.primary_image.file.url)
            if request and line.product.primary_image_id
            else None
        ),
        "sku": line.sku_snapshot,
        "name": line.name_snapshot,
        "unit_size": str(line.unit_size_snapshot),
        "unit_label": line.unit_label_snapshot,
        "unit_price_vnd": str(line.unit_price_vnd_snapshot),
        "quantity": line.quantity,
        "quota_group_id": str(line.quota_group_id) if line.quota_group_id else None,
        "quota_cost": line.quota_cost_snapshot,
        "quota_quantity": line.quota_quantity,
        "quota_units": line.quota_units,
        "cash_quantity": line.cash_quantity,
        "cash_subtotal_vnd": str(line.cash_subtotal_vnd),
    }


def order_payload(order, *, guest_token=None, admin=False, request=None):
    viewer = getattr(request, "user", None)
    lines = getattr(order, "payload_lines", None)
    if lines is None:
        lines = list(
            order.lines.select_related("product__primary_image").order_by("position", "id")
        )
    payment = None
    if order.total_vnd:
        payment = {
            "status": order.payment_status,
            "bank_bin": order.bank_bin_snapshot,
            "bank_name": order.bank_name_snapshot,
            "account_number": order.account_number_snapshot,
            "account_name": order.account_name_snapshot,
            "transfer_content": order.transfer_content,
            "qr_payload": {
                "acqId": order.bank_bin_snapshot,
                "accountNo": order.account_number_snapshot,
                "accountName": order.account_name_snapshot,
                "amount": order.total_vnd,
                "addInfo": order.transfer_content,
            },
            "received_at": order.payment_received_at.isoformat()
            if order.payment_received_at
            else None,
            "refunded_at": order.refunded_at.isoformat() if order.refunded_at else None,
            "refund_amount_vnd": str(order.refund_amount_vnd)
            if order.refund_amount_vnd is not None
            else None,
            "refund_note": order.refund_note,
        }
    result = {
        "id": str(order.pk),
        "can_buy_again": bool(
            viewer and viewer.is_authenticated and order.buyer_user_id == viewer.pk
        ),
        "reference_code": order.reference_code,
        "status": order.status,
        "payment_status": order.payment_status,
        "locale": order.locale,
        "lines": [_line_payload(line, request) for line in lines],
        "shipping_address": {
            "recipient_name": order.recipient_name_snapshot,
            "phone_number": order.phone_e164_snapshot,
            "province_code": order.province_code_snapshot,
            "ward_code": order.ward_code_snapshot,
            "address": order.address_snapshot,
        },
        "subtotal_vnd": str(order.subtotal_vnd),
        "discount_amount_vnd": str(order.discount_amount_vnd),
        "coupon": order.coupon_snapshot,
        "shipping_fee_vnd": str(order.shipping_fee_vnd),
        "shipping_discount_reason": order.shipping_discount_reason or None,
        "total_vnd": str(order.total_vnd),
        "payment": payment,
        "expires_at": order.expires_at.isoformat() if order.expires_at else None,
        "server_time": timezone.now().isoformat(),
        "transfer_reported_at": order.transfer_reported_at.isoformat()
        if order.transfer_reported_at
        else None,
        "approved_at": order.approved_at.isoformat() if order.approved_at else None,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "cancellation_reason": order.cancellation_reason or None,
        "reconciliation_note": order.reconciliation_note,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }
    if guest_token:
        result["guest_access_token"] = guest_token
    if admin:
        prefetch_related_objects(
            lines,
            Prefetch(
                "stock_allocations",
                queryset=StoreStockAllocation.objects.select_related(
                    "batch__farm_product__farm"
                ).prefetch_related("movements"),
                to_attr="payload_allocations",
            ),
        )
        inventory = source_inventory(order)
        result["allocation_suggestion"] = inventory.pop("suggestion")
        result["source_inventory"] = inventory
        allocations = []
        today = timezone.localdate()
        for line in lines:
            for item in line.payload_allocations:
                restocked = sum(
                    movement.quantity
                    for movement in item.movements.all()
                    if movement.kind == "restock"
                )
                unavailable_reason = restock_unavailable_reason(
                    order, item.batch, item.quantity - restocked, as_of=today
                )
                allocations.append(
                    {
                        "id": str(item.pk),
                        "line_id": str(item.line_id),
                        "product_id": str(line.product_id),
                        "batch_id": str(item.batch_id),
                        "farm_id": str(item.batch.farm_product.farm_id),
                        "farm_name": (
                            item.batch.farm_product.farm.name_vi
                            if order.locale == "vi"
                            else item.batch.farm_product.farm.name_en
                        ),
                        "start_date": item.batch.start_date.isoformat(),
                        "end_date": item.batch.end_date.isoformat(),
                        "quantity": item.quantity,
                        "restocked_quantity": restocked,
                        "restock_remaining_quantity": item.quantity - restocked,
                        "restock_eligible": unavailable_reason is None,
                        "restock_unavailable_reason": unavailable_reason,
                    }
                )
        result["allocations"] = allocations
    return result


def order_list_queryset(rows):
    return rows.prefetch_related(
        Prefetch(
            "lines",
            queryset=StoreOrderLine.objects.select_related("product__primary_image").order_by(
                "position", "id"
            ),
            to_attr="payload_lines",
        )
    )


class SelfStoreVoucherPreviewView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def post(self, request):
        serializer = StoreVoucherPreviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            data = preview_store_voucher(user=request.user, payload=serializer.validated_data)
        except (StoreOrderError, CouponError) as error:
            return store_error(error)
        return no_store(Response({"data": data}))


class PublicStoreCheckoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if error_response := public_store_scope_error(request):
            return error_response
        serializer = StoreCheckoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order, guest_token, created = create_store_checkout(
                user=request.user, payload=serializer.validated_data
            )
        except (StoreOrderError, CouponError) as error:
            return store_error(error)
        return no_store(
            Response(
                {"data": order_payload(order, request=request, guest_token=guest_token)},
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )


class PublicStoreTransferReportView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        if error_response := public_store_scope_error(request):
            return error_response
        serializer = StoreOrderAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = report_store_transfer(
                order_id=order_id,
                user=request.user,
                guest_token=serializer.validated_data.get("access_token", ""),
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            if isinstance(error, StoreOrder.DoesNotExist):
                error = StoreOrderError("store_order_not_found")
            return store_error(error)
        return no_store(Response({"data": order_payload(order, request=request)}))


class PublicStoreOrderCancelView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        if error_response := public_store_scope_error(request):
            return error_response
        serializer = StoreOrderCancelAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = cancel_pending_store_order(
                order_id=order_id,
                user=request.user,
                guest_token=serializer.validated_data.get("access_token", ""),
                idempotency_key=serializer.validated_data["idempotency_key"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request)}))


class SelfStoreOrderListView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        query = StoreOrderQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = StoreOrder.objects.filter(buyer_user=request.user)
        if requested_status := query.validated_data.get("status"):
            rows = rows.filter(status=requested_status)
        page = query.validated_data["page"]
        page_size = query.validated_data["page_size"]
        total = rows.count()
        rows = order_list_queryset(rows)[(page - 1) * page_size : page * page_size]
        return no_store(
            Response(
                {
                    "data": [order_payload(item, request=request) for item in rows],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )


class SelfStoreOrderDetailView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request, order_id):
        order = get_object_or_404(StoreOrder, pk=order_id, buyer_user=request.user)
        expire_order_if_needed(order.pk)
        return no_store(
            Response({"data": order_payload(StoreOrder.objects.get(pk=order.pk), request=request)})
        )


class SelfStoreBuyAgainView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request, order_id):
        query = PublicStoreLocaleSerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        order = get_object_or_404(StoreOrder, pk=order_id, buyer_user=request.user)
        lines = list(order.lines.order_by("position", "id"))
        products = {
            product.pk: product
            for product in public_product_queryset().filter(
                pk__in=[line.product_id for line in lines]
            )
        }
        result = []
        for line in lines:
            product = products.get(line.product_id)
            reason = None
            if product is None:
                reason = "unavailable"
            elif product.available_quantity < line.quantity:
                reason = "out_of_stock" if product.available_quantity == 0 else "insufficient_stock"
            result.append(
                {
                    "product_id": str(line.product_id),
                    "quantity": line.quantity,
                    "name": line.name_snapshot,
                    "reason": reason,
                    "product": public_product_payload(
                        product, request, query.validated_data["locale"], detail=True
                    )
                    if product
                    else None,
                }
            )
        return no_store(Response({"data": {"lines": result}}))


class AdminStoreOrderListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = StoreOrderQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        rows = StoreOrder.objects.all()
        if requested_status := query.validated_data.get("status"):
            rows = rows.filter(status=requested_status)
        if payment_status := query.validated_data.get("payment_status"):
            rows = rows.filter(payment_status=payment_status)
        if search := query.validated_data["search"]:
            rows = rows.filter(
                Q(reference_code__icontains=search)
                | Q(recipient_name_snapshot__icontains=search)
                | Q(phone_e164_snapshot__icontains=search)
            )
        rows = rows.order_by(
            {
                "created_desc": "-created_at",
                "created_asc": "created_at",
                "deadline_asc": "expires_at",
                "total_desc": "-total_vnd",
            }[query.validated_data["sort"]],
            "id",
        )
        page = query.validated_data["page"]
        page_size = query.validated_data["page_size"]
        total = rows.count()
        rows = order_list_queryset(rows)[(page - 1) * page_size : page * page_size]
        return no_store(
            Response(
                {
                    "data": [order_payload(item, request=request) for item in rows],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )


class AdminStoreOrderDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, order_id):
        order = get_object_or_404(StoreOrder.objects.prefetch_related("lines"), pk=order_id)
        expire_order_if_needed(order.pk)
        return no_store(
            Response(
                {
                    "data": order_payload(
                        StoreOrder.objects.get(pk=order.pk), admin=True, request=request
                    )
                }
            )
        )


class AdminStoreOrderApproveView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = approve_store_order(
                order_id=order_id,
                actor=request.user,
                idempotency_key=serializer.validated_data["idempotency_key"],
                allocations=serializer.validated_data["allocations"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            if isinstance(error, StoreOrder.DoesNotExist):
                error = StoreOrderError("store_order_not_found")
            return store_error(error)
        return no_store(Response({"data": order_payload(order, request=request, admin=True)}))


class GuestLookupThrottle(SimpleRateThrottle):
    scope = "store_guest_lookup"

    def get_cache_key(self, request, view):
        client = trusted_store_client_ip(request)
        ident = (
            f"bff:{client}" if client is not None else request.META.get("REMOTE_ADDR", "unknown")
        )
        return self.cache_format % {"scope": self.scope, "ident": ident}

    def get_rate(self):
        return "10/min"


class PublicStoreOrderLookupView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [GuestLookupThrottle]

    def post(self, request):
        serializer = StoreGuestLookupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone

        try:
            phone = normalize_vietnam_phone(serializer.validated_data["phone_number"])
        except InvalidPhoneNumber:
            phone = ""
        order = StoreOrder.objects.filter(
            reference_code=serializer.validated_data["reference_code"].upper(),
            phone_e164_snapshot=phone,
            buyer_user__isnull=True,
        ).first()
        if order is None:
            return store_error(StoreOrderError("store_order_not_found"))
        expire_order_if_needed(order.pk)
        order.refresh_from_db()
        token = secrets.token_urlsafe(32)
        StoreGuestLookupSession.objects.create(
            order=order,
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        response = no_store(Response({"data": order_payload(order, request=request)}))
        response.set_cookie(
            "nfv_store_lookup",
            token,
            max_age=15 * 60,
            path="/api/v1/public/store-order-lookups",
            secure=(not settings.DEBUG or settings.SESSION_COOKIE_SECURE),
            httponly=True,
            samesite="Lax",
        )
        return response


class PublicStoreOrderLookupCurrentView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        token = request.COOKIES.get("nfv_store_lookup", "")
        session = (
            StoreGuestLookupSession.objects.select_related("order")
            .filter(
                token_hash=hashlib.sha256(token.encode()).hexdigest() if token else "",
                expires_at__gt=timezone.now(),
            )
            .first()
        )
        if session is None:
            return store_error(StoreOrderError("store_order_not_found"))
        expire_order_if_needed(session.order_id)
        session.order.refresh_from_db()
        return no_store(Response({"data": order_payload(session.order, request=request)}))


class PublicStoreOrderDetailView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, order_id):
        if error_response := public_store_scope_error(request):
            return error_response
        order = StoreOrder.objects.filter(pk=order_id).first()
        if order is None:
            return store_error(StoreOrderError("store_order_not_found"))
        try:
            verify_order_access(
                order,
                user=request.user,
                guest_token=request.headers.get("X-Store-Access-Token", ""),
            )
        except (StoreOrderError, CouponError) as error:
            return store_error(error)
        expire_order_if_needed(order.pk)
        order.refresh_from_db()
        return no_store(Response({"data": order_payload(order, request=request)}))


class AdminStoreOrderCancelView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = cancel_store_order(
                order_id=order_id,
                actor=request.user,
                idempotency_key=serializer.validated_data["idempotency_key"],
                reason=serializer.validated_data["reason"],
                reconciliation_note=serializer.validated_data["reconciliation_note"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request, admin=True)}))


class AdminStoreOrderRecordPaymentView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreRecordPaymentSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = record_store_payment(
                order_id=order_id,
                actor=request.user,
                idempotency_key=serializer.validated_data["idempotency_key"],
                note=serializer.validated_data["note"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request, admin=True)}))


class AdminStoreOrderRefundView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreRefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = refund_store_order(
                order_id=order_id,
                actor=request.user,
                **serializer.validated_data,
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request, admin=True)}))


class AdminStoreOrderDeliverView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = deliver_store_order(
                order_id=order_id,
                actor=request.user,
                idempotency_key=serializer.validated_data["idempotency_key"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request, admin=True)}))


class AdminStoreOrderRestockView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreRestockSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = restock_store_order(
                order_id=order_id,
                actor=request.user,
                idempotency_key=serializer.validated_data["idempotency_key"],
                allocations=serializer.validated_data["allocations"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request, admin=True)}))
