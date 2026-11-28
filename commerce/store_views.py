import hashlib
import secrets
from datetime import UTC, timedelta
from uuid import UUID

from django.conf import settings
from django.db.models import Prefetch, Q, prefetch_related_objects
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from commerce.admin_store_orders import (
    CREATION_ACTION,
    create_admin_store_order,
    quote_admin_store_order,
)
from commerce.models import (
    StoreGuestLookupSession,
    StoreOrder,
    StoreOrderAction,
    StoreOrderLine,
    StoreStockAllocation,
)
from commerce.public_catalog import (
    public_product_payload,
    public_product_queryset,
    with_public_product_capacity,
)
from commerce.serializers import (
    AdminCreationQuerySerializer,
    AdminStoreOrderCreateSerializer,
    AdminStoreOrderQuoteSerializer,
    PublicStoreLocaleSerializer,
    StoreActionSerializer,
    StoreApproveSerializer,
    StoreCancelSerializer,
    StoreCheckoutSerializer,
    StoreGuestLookupSerializer,
    StoreOrderAccessSerializer,
    StoreOrderCancelAccessSerializer,
    StoreOrderQuerySerializer,
    StoreQuoteSerializer,
    StoreRecordPaymentSerializer,
    StoreRefundRequestAccessSerializer,
    StoreRefundRequestCloseSerializer,
    StoreRefundSerializer,
    StoreRestockSerializer,
    StoreVoucherPreviewSerializer,
)
from commerce.store_orders import (
    StoreOrderError,
    approve_store_order,
    cancel_pending_store_order,
    cancel_store_order,
    close_store_refund_request,
    create_store_checkout,
    deliver_store_order,
    expire_order_if_needed,
    preview_store_voucher,
    record_store_payment,
    refund_store_order,
    report_store_transfer,
    request_store_refund,
    restock_store_order,
    restock_unavailable_reason,
    source_inventory,
    verify_order_access,
)
from commerce.store_proxy import trusted_store_client_ip
from config.phones import InvalidPhoneNumber, normalize_vietnam_phone, phone_search
from content.creation_receipts import creation_receipt_meta, find_creation_receipt
from content.permissions import IsWebsiteAdmin
from memberships.coupons import CouponError
from memberships.csa_payloads import round_payload, selection_payload
from memberships.permissions import HasMembershipSelfScopes


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def store_error(error):
    if error.code == "store_payment_unavailable":
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif error.code in {
        "store_order_not_found",
        "store_buyer_not_found",
        "create_receipt_not_found",
    }:
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
        "store_allocation_mixed_farm",
        "store_payment_state_conflict",
        "store_refund_state_conflict",
        "store_restock_state_conflict",
        "store_restock_invalid",
        "store_restock_duplicate",
        "store_requote_required",
        "store_membership_frozen",
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


def _can_buy_again(order, viewer):
    return bool(
        viewer
        and viewer.is_authenticated
        and order.buyer_user_id == viewer.pk
        and not getattr(order, "csa_selection", None)
    )


def order_payload(order, *, guest_token=None, admin=False, admin_list=False, request=None):
    viewer = getattr(request, "user", None)
    lines = getattr(order, "payload_lines", None)
    if lines is None:
        lines = list(
            order.lines.select_related("product__primary_image").order_by("position", "id")
        )
    payment = None
    if order.total_vnd and (order.workflow_version == 1 or order.approved_at is not None):
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
        "can_buy_again": _can_buy_again(order, viewer),
        "reference_code": order.reference_code,
        "status": order.status,
        "workflow_version": order.workflow_version,
        "payment_status": order.payment_status,
        "refund_status": order.refund_status,
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
        "delivery_date": order.delivery_date.isoformat() if order.delivery_date else None,
        "shipping_benefit": order.shipping_benefit_snapshot,
        "shipping_discount_reason": order.shipping_discount_reason or None,
        "total_vnd": str(order.total_vnd),
        "payment": payment,
        "expires_at": order.expires_at.astimezone(UTC).isoformat() if order.expires_at else None,
        "server_time": timezone.now().isoformat(),
        "transfer_reported_at": order.transfer_reported_at.isoformat()
        if order.transfer_reported_at
        else None,
        "transfer_reported_by": str(order.transfer_reported_by_id)
        if order.transfer_reported_by_id
        else None,
        "approved_at": order.approved_at.isoformat() if order.approved_at else None,
        "approved_by": str(order.approved_by_id) if order.approved_by_id else None,
        "payment_received_at": order.payment_received_at.isoformat()
        if order.payment_received_at
        else None,
        "payment_received_by": str(order.payment_received_by_id)
        if order.payment_received_by_id
        else None,
        "refund_requested_at": order.refund_requested_at.isoformat()
        if order.refund_requested_at
        else None,
        "refund_requested_by": str(order.refund_requested_by_id)
        if order.refund_requested_by_id
        else None,
        "refund_request_closed_at": order.refund_request_closed_at.isoformat()
        if order.refund_request_closed_at
        else None,
        "refund_request_closed_by": str(order.refund_request_closed_by_id)
        if order.refund_request_closed_by_id
        else None,
        "refund_request_closed_reason": order.refund_request_closed_reason or None,
        "refunded_at": order.refunded_at.isoformat() if order.refunded_at else None,
        "refunded_by": str(order.refunded_by_id) if order.refunded_by_id else None,
        "refund_amount_vnd": str(order.refund_amount_vnd)
        if order.refund_amount_vnd is not None
        else None,
        "refund_note": order.refund_note,
        "delivered_at": order.delivered_at.isoformat() if order.delivered_at else None,
        "delivered_by": str(order.delivered_by_id) if order.delivered_by_id else None,
        "cancelled_at": order.cancelled_at.isoformat() if order.cancelled_at else None,
        "cancelled_by": str(order.cancelled_by_id) if order.cancelled_by_id else None,
        "cancellation_reason": order.cancellation_reason or None,
        "reconciliation_note": order.reconciliation_note,
        "created_at": order.created_at.isoformat(),
        "updated_at": order.updated_at.isoformat(),
    }
    csa = getattr(order, "csa_selection", None)
    if csa:
        result["csa"] = {"round": round_payload(csa.round), "selection": selection_payload(csa)}
    if guest_token:
        result["guest_access_token"] = guest_token
    if (admin or admin_list) and order.admin_creation_receipt_id:
        actor_id = order.admin_creation_receipt.actor_id
        result["creation"] = {
            "origin": "admin_historical" if order.is_historical else "admin",
            "actor_id": str(actor_id) if actor_id is not None else None,
        }
    if admin:
        inventory = source_inventory(order, lines=lines)
        result["allocation_suggestion"] = inventory.pop("suggestion")
        result["source_inventory"] = inventory
        result["allocations"] = []
        if order.is_historical:
            return result
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
        allocations = result["allocations"]
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
    return result


def order_list_queryset(rows, *, admin=False):
    rows = rows.select_related("csa_selection__round")
    if admin:
        rows = rows.select_related("admin_creation_receipt")
    return rows.prefetch_related(
        "csa_selection__lines",
        Prefetch(
            "lines",
            queryset=StoreOrderLine.objects.select_related("product__primary_image").order_by(
                "position", "id"
            ),
            to_attr="payload_lines",
        ),
    )


def overdue_csa_order_filter(*, now):
    """One deadline predicate for list status projection and page reconciliation."""
    return Q(
        status="pending_review",
        csa_selection__state="reselecting",
        csa_selection__reopen_until__lte=now,
        csa_selection__round__published_at__isnull=False,
        csa_selection__round__cancelled_at__isnull=True,
    )


def overdue_order_filter(*, now):
    return overdue_csa_order_filter(now=now) | Q(
        status__in=["pending_review", "payment_pending", "payment_review"],
        expires_at__lte=now,
        csa_selection__isnull=True,
    )


def filter_order_list_status(rows, requested_status, *, now):
    if not requested_status:
        return rows
    # Count/filter overdue CSA orders as cancelled even when they are off-page.
    # The worker owns global reconciliation; reads only reconcile the returned page.
    overdue = overdue_order_filter(now=now)
    if requested_status == "cancelled":
        return rows.filter(Q(status="cancelled") | overdue)
    if requested_status in {"pending_review", "payment_pending", "payment_review"}:
        return rows.filter(status=requested_status).exclude(overdue)
    return rows.filter(status=requested_status)


def filter_order_list_payment_status(rows, requested_status, *, now):
    # Expiry can turn an existing receipt into a refund before the worker runs.
    # Apply the same effective state to filtering/counts and returned rows.
    overdue_received = overdue_order_filter(now=now) & Q(payment_status="received")
    if requested_status == "refund_required":
        return rows.filter(Q(payment_status="refund_required") | overdue_received)
    if requested_status == "received":
        return rows.filter(payment_status="received").exclude(overdue_received)
    return rows.filter(payment_status=requested_status)


def order_list_page(rows, *, page, page_size, now, admin=False):
    rows = rows.annotate(csa_reselection_overdue=overdue_order_filter(now=now))
    items = list(order_list_queryset(rows, admin=admin)[(page - 1) * page_size : page * page_size])
    overdue_ids = []
    for order in items:
        if order.csa_reselection_overdue:
            expire_order_if_needed(order.pk, now=now)
            overdue_ids.append(order.pk)
    if overdue_ids:
        refreshed = {
            order.pk: order
            for order in order_list_queryset(
                StoreOrder.objects.filter(pk__in=overdue_ids), admin=admin
            )
        }
        items = [refreshed.get(order.pk, order) for order in items]
    return items


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


class PublicStoreDeliveryDatesView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        from commerce.store_delivery import delivery_calendar

        if error_response := public_store_scope_error(request):
            return error_response
        try:
            return no_store(Response({"data": delivery_calendar(user=request.user)}))
        except StoreOrderError as error:
            return store_error(error)


class PublicStoreQuoteView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        if error_response := public_store_scope_error(request):
            return error_response
        serializer = StoreQuoteSerializer(data=request.data)
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
        if hasattr(request.data, "get") and "delivery_date" not in request.data:
            # Requests created before delivery dates were introduced must replay
            # their exact original payload. The service still verifies owner and
            # fingerprint before returning that order; new checkout stays strict.
            try:
                replay_key = UUID(str(request.data.get("idempotency_key", "")))
            except (ValueError, TypeError, AttributeError):
                replay_key = None
            if (
                replay_key
                and StoreOrder.objects.filter(
                    create_idempotency_key=replay_key, workflow_version=1
                ).exists()
            ):
                serializer.fields["delivery_date"].required = False
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
                request_refund=serializer.validated_data["request_refund"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return no_store(Response({"data": order_payload(order, request=request)}))


class PublicStoreRefundRequestView(APIView):
    permission_classes = [AllowAny]

    def post(self, request, order_id):
        if error_response := public_store_scope_error(request):
            return error_response
        serializer = StoreRefundRequestAccessSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = request_store_refund(
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
        now = timezone.now()
        rows = filter_order_list_status(
            StoreOrder.objects.filter(buyer_user=request.user),
            query.validated_data.get("status"),
            now=now,
        )
        page = query.validated_data["page"]
        page_size = query.validated_data["page_size"]
        total = rows.count()
        rows = order_list_page(rows, page=page, page_size=page_size, now=now)
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
        if not _can_buy_again(order, request.user):
            return store_error(StoreOrderError("store_order_not_found"))
        lines = list(order.lines.order_by("position", "id"))
        products = {
            product.pk: product
            for product in with_public_product_capacity(
                public_product_queryset(detail=True).filter(
                    pk__in=[line.product_id for line in lines]
                )
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
        return no_store(Response({"data": {"order_id": str(order.pk), "lines": result}}))


class AdminStoreOrderListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        if "creation_key" in request.query_params:
            return self.recover_creation(request)

        query = StoreOrderQuerySerializer(data=request.query_params.dict())
        query.is_valid(raise_exception=True)
        now = timezone.now()
        rows = filter_order_list_status(
            StoreOrder.objects.all(),
            query.validated_data.get("status"),
            now=now,
        )
        if payment_status := query.validated_data.get("payment_status"):
            rows = filter_order_list_payment_status(rows, payment_status, now=now)
        if search := query.validated_data["search"]:
            rows = rows.filter(
                Q(reference_code__icontains=search)
                | Q(recipient_name_snapshot__icontains=search)
                | phone_search(search, "phone_e164_snapshot")
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
        rows = order_list_page(rows, page=page, page_size=page_size, now=now, admin=True)
        return no_store(
            Response(
                {
                    "data": [
                        order_payload(item, admin_list=True, request=request) for item in rows
                    ],
                    "meta": {"page": page, "page_size": page_size, "total": total},
                }
            )
        )

    def post(self, request):
        serializer = AdminStoreOrderCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            receipt, created = create_admin_store_order(
                actor=request.user, payload=serializer.validated_data
            )
        except StoreOrderError as error:
            return store_error(error)
        return self.creation_response(request, receipt, created=created)

    def recover_creation(self, request):
        query = AdminCreationQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        if len(request.query_params.getlist("creation_key")) != 1:
            raise ValidationError({"creation_key": "Provide this parameter once."})
        receipt = find_creation_receipt(
            actor=request.user, action=CREATION_ACTION, key=query.validated_data["creation_key"]
        )
        if receipt is None:
            return store_error(StoreOrderError("create_receipt_not_found"))
        return self.creation_response(request, receipt, created=False)

    def creation_response(self, request, receipt, *, created):
        order = get_object_or_404(
            StoreOrder.objects.select_related("admin_creation_receipt"),
            pk=receipt.resource_id,
            admin_creation_receipt=receipt,
        )
        return no_store(
            Response(
                {
                    "data": order_payload(order, admin=True, request=request),
                    "meta": creation_receipt_meta(receipt, replayed=not created),
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )


class AdminStoreOrderQuoteView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request):
        serializer = AdminStoreOrderQuoteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            quote = quote_admin_store_order(actor=request.user, payload=serializer.validated_data)
        except StoreOrderError as error:
            return store_error(error)
        return no_store(Response({"data": quote}))


class AdminStoreOrderDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, order_id):
        rows = StoreOrder.objects.select_related("admin_creation_receipt")
        order = get_object_or_404(rows, pk=order_id)
        if not order.is_historical:
            expire_order_if_needed(order_id)
            order = rows.get(pk=order_id)
        return no_store(Response({"data": order_payload(order, admin=True, request=request)}))


def _admin_action_response(request, order, *, action, idempotency_key):
    # The service has applied the operation or validated its replay fingerprint.
    # Confirm it from the persisted ledger, scoped to this exact action.
    try:
        persisted = StoreOrderAction.objects.values("order_id", "action", "idempotency_key").get(
            order_id=order.pk, action=action, idempotency_key=idempotency_key
        )
    except StoreOrderAction.DoesNotExist:
        return no_store(
            Response(
                {"errors": [{"code": "store_action_receipt_unavailable"}]},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        )
    receipt = {
        "order_id": str(persisted["order_id"]),
        "action": persisted["action"],
        "idempotency_key": str(persisted["idempotency_key"]),
    }
    return no_store(
        Response(
            {
                "data": order_payload(order, request=request, admin=True),
                "meta": {"action_receipt": receipt},
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
                **serializer.validated_data,
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            if isinstance(error, StoreOrder.DoesNotExist):
                error = StoreOrderError("store_order_not_found")
            return store_error(error)
        return _admin_action_response(
            request,
            order,
            action="approve",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )


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
                reconciliation_note=serializer.validated_data.get("reconciliation_note"),
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return _admin_action_response(
            request,
            order,
            action="cancel",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )


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
        return _admin_action_response(
            request,
            order,
            action="record_payment",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )


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
        return _admin_action_response(
            request,
            order,
            action="refund",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )


class AdminStoreRefundRequestCloseView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, order_id):
        serializer = StoreRefundRequestCloseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order = close_store_refund_request(
                order_id=order_id,
                actor=request.user,
                idempotency_key=serializer.validated_data["idempotency_key"],
                reason=serializer.validated_data["reason"],
            )
        except (StoreOrder.DoesNotExist, StoreOrderError) as error:
            return store_error(
                error
                if isinstance(error, StoreOrderError)
                else StoreOrderError("store_order_not_found")
            )
        return _admin_action_response(
            request,
            order,
            action="close_refund_request",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )


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
        return _admin_action_response(
            request,
            order,
            action="deliver",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )


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
        return _admin_action_response(
            request,
            order,
            action="restock",
            idempotency_key=serializer.validated_data["idempotency_key"],
        )
