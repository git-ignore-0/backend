import hashlib
import json
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Round
from django.utils import timezone
from django.utils.crypto import salted_hmac

from commerce.models import (
    PUBLIC_STORE_MAX_PRICE_VND,
    CatalogTreeLock,
    Product,
    ShippingAddress,
    StoreOrder,
    StoreOrderAction,
    StoreOrderLine,
    StoreOrderStatus,
    StorePaymentStatus,
    StoreQuotaReservation,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockMovementKind,
    StoreStockReservation,
)
from commerce.shipping import active_shipping_area
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from members.administrative_units import province_exists, ward_choices
from memberships.models import (
    Membership,
    MembershipCycleQuota,
    MembershipPaymentSettings,
    MembershipStatus,
    MembershipUsageAllocation,
    MembershipUsageLine,
    MembershipUsageOrder,
    MembershipUsageReversal,
    QuotaPolicy,
    UsageOrderStatus,
)
from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone
from memberships.services import ensure_due_cycles, quota_availability_rows, sync_membership_status
from referrals.store_rewards import (
    evaluate_store_referral_reward,
    lock_store_referral_referrer,
    store_referral_rewards_active,
)

PAYMENT_WINDOW = timedelta(minutes=5)
REVIEW_WINDOW = timedelta(hours=8)


class StoreOrderError(Exception):
    code = "store_order_invalid"

    def __init__(self, code=None, *, context=None):
        self.code = code or self.code
        self.context = context or {}
        super().__init__(self.code)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _guest_access_token(order_id, idempotency_key):
    return salted_hmac(
        "commerce.store-order-access",
        f"{order_id}:{idempotency_key}",
    ).hexdigest()


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def request_fingerprint(payload):
    material = dict(payload)
    material.pop("idempotency_key", None)
    return _hash(json.dumps(_jsonable(material), sort_keys=True, separators=(",", ":")))


def _active_membership_for_user(user, *, lock=False):
    if not user or not user.is_authenticated:
        return None
    today = timezone.localdate()
    rows = Membership.objects
    if lock:
        rows = rows.select_for_update()
    membership = (
        rows.select_related("customer")
        .filter(
            customer__user=user,
            current_slot=True,
            start_date__lte=today,
            end_date__gt=today,
        )
        .order_by("start_date", "id")
        .first()
    )
    if membership is None:
        return None
    membership = sync_membership_status(membership, as_of=today)
    return membership if membership.status == MembershipStatus.ACTIVE else None


def _translation(product, locale):
    translations = {item.locale: item for item in product.translations.all()}
    return translations.get(locale) or translations.get("vi") or translations.get("en")


def _net_sold_by_batch(batch_ids):
    result = defaultdict(int)
    rows = (
        StoreStockMovement.objects.filter(batch_id__in=batch_ids)
        .values("batch_id", "kind")
        .annotate(total=Sum("quantity"))
    )
    for row in rows:
        sign = 1 if row["kind"] == StoreStockMovementKind.SALE else -1
        result[row["batch_id"]] += sign * row["total"]
    return result


def active_product_stock(product_ids, *, lock=False):
    today = timezone.localdate()
    batches = (
        FarmProductStockBatch.objects.filter(
            farm_product__product_id__in=product_ids,
            farm_product__farm__is_active=True,
            farm_product__is_active=True,
            cancelled_at__isnull=True,
            start_date__lte=today,
            end_date__gte=today,
        )
        .select_related("farm_product__farm")
        .order_by("farm_product__product_id", "end_date", "start_date", "id")
    )
    if lock:
        batches = batches.select_for_update()
    batches = list(batches)
    net_sold = _net_sold_by_batch([item.pk for item in batches])
    totals = defaultdict(int)
    for batch in batches:
        totals[batch.farm_product.product_id] += max(0, batch.quantity_units - net_sold[batch.pk])
    return totals, batches


def _active_reserved_stock(product_ids, *, exclude_order=None):
    rows = StoreStockReservation.objects.filter(
        product_id__in=product_ids,
        released_at__isnull=True,
        line__order__status__in=[
            StoreOrderStatus.PAYMENT_PENDING,
            StoreOrderStatus.PENDING_REVIEW,
        ],
        line__order__expires_at__gt=timezone.now(),
    )
    if exclude_order is not None:
        rows = rows.exclude(line__order=exclude_order)
    return {
        row["product_id"]: row["total"]
        for row in rows.values("product_id").annotate(total=Sum("quantity"))
    }


def ensure_batch_change_preserves_reservations(
    batch, *, quantity_units=None, start_date=None, end_date=None, cancelling=False
):
    product_id = batch.farm_product.product_id
    totals, _batches = active_product_stock([product_id], lock=True)
    reserved = _active_reserved_stock([product_id]).get(product_id, 0)
    net_sold = _net_sold_by_batch([batch.pk]).get(batch.pk, 0)
    new_quantity = batch.quantity_units if quantity_units is None else quantity_units
    if new_quantity < net_sold:
        raise StoreOrderError("stock_sold")
    today = timezone.localdate()
    currently_eligible = (
        batch.cancelled_at is None
        and batch.farm_product.is_active
        and batch.farm_product.farm.is_active
        and batch.start_date <= today <= batch.end_date
    )
    next_eligible = (
        not cancelling
        and batch.farm_product.is_active
        and batch.farm_product.farm.is_active
        and (start_date or batch.start_date) <= today <= (end_date or batch.end_date)
    )
    current_remaining = max(0, batch.quantity_units - net_sold) if currently_eligible else 0
    next_remaining = max(0, new_quantity - net_sold) if next_eligible else 0
    if totals.get(product_id, 0) - current_remaining + next_remaining < reserved:
        raise StoreOrderError("stock_reserved")


def ensure_offer_change_preserves_reservations(offer, *, active):
    if active or not offer.is_active:
        return
    product_id = offer.product_id
    totals, batches = active_product_stock([product_id], lock=True)
    reserved = _active_reserved_stock([product_id]).get(product_id, 0)
    net_sold = _net_sold_by_batch([item.pk for item in batches])
    removed = sum(
        _batch_available(batch, net_sold) for batch in batches if batch.farm_product_id == offer.pk
    )
    if totals.get(product_id, 0) - removed < reserved:
        raise StoreOrderError("stock_reserved")


def ensure_farm_deactivation_preserves_reservations(farm):
    offers = list(farm.products.filter(is_active=True).order_by("product_id"))
    product_ids = [item.product_id for item in offers]
    if not product_ids:
        return
    totals, batches = active_product_stock(product_ids, lock=True)
    reserved = _active_reserved_stock(product_ids)
    net_sold = _net_sold_by_batch([item.pk for item in batches])
    removed = defaultdict(int)
    for batch in batches:
        if batch.farm_product.farm_id == farm.pk:
            removed[batch.farm_product.product_id] += _batch_available(batch, net_sold)
    if any(
        totals.get(product_id, 0) - removed[product_id] < reserved.get(product_id, 0)
        for product_id in product_ids
    ):
        raise StoreOrderError("stock_reserved")


def _validate_address(payload):
    province = payload["province_code"].strip()
    ward = payload["ward_code"].strip()
    if not province_exists(province) or ward not in dict(ward_choices(province, "vi")):
        raise StoreOrderError("store_shipping_address_invalid")
    area = active_shipping_area(province_code=province, ward_code=ward)
    if area is None:
        raise StoreOrderError("store_shipping_area_unsupported")
    try:
        phone = normalize_vietnam_phone(payload["phone_number"])
    except InvalidPhoneNumber as error:
        raise StoreOrderError("store_phone_invalid") from error
    return {
        "recipient_name_snapshot": payload["recipient_name"].strip(),
        "phone_e164_snapshot": phone,
        "province_code_snapshot": province,
        "ward_code_snapshot": ward,
        "address_snapshot": payload["address"].strip(),
    }, area


def _address_for_checkout(user, guest):
    if user and user.is_authenticated:
        try:
            address = ShippingAddress.objects.select_for_update().get(user=user)
        except ShippingAddress.DoesNotExist as error:
            raise StoreOrderError("store_shipping_address_required") from error
        address_values, area = _validate_address(
            {
                "recipient_name": address.recipient_name,
                "phone_number": address.phone_number,
                "province_code": address.province_code,
                "ward_code": address.ward_code,
                "address": address.address,
            }
        )
        return address_values, area, address.updated_at
    if not guest:
        raise StoreOrderError("store_guest_address_required")
    address_values, area = _validate_address(guest)
    return address_values, area, None


def _available_quota_rows(membership, group_ids):
    return [
        (row, available)
        for row, _reserved, available, _current, _available_for_order in quota_availability_rows(
            membership,
            product_ids=group_ids,
            lock=True,
        )
        if row.remaining_units > 0
    ]


def _reference_code():
    return f"SO{timezone.localdate():%y%m%d}{secrets.token_hex(4).upper()}"


@transaction.atomic
def create_store_checkout(*, user, payload):
    fingerprint = request_fingerprint(payload)
    key = payload["idempotency_key"]
    existing = StoreOrder.objects.filter(create_idempotency_key=key).first()
    if existing is None:
        # A concurrent request may create this key while we wait for inventory.
        # Re-read under the shared lock before validating a new quote.
        CatalogTreeLock.objects.select_for_update().get(pk=1)
        existing = StoreOrder.objects.select_for_update().filter(create_idempotency_key=key).first()
    if existing:
        requester_user_id = user.pk if user and user.is_authenticated else None
        if existing.buyer_user_id != requester_user_id:
            raise StoreOrderError("store_idempotency_conflict")
        if existing.request_fingerprint != fingerprint:
            raise StoreOrderError("store_idempotency_conflict")
        expire_order_if_needed(existing.pk)
        guest_token = (
            _guest_access_token(existing.pk, existing.create_idempotency_key)
            if existing.buyer_user_id is None
            else None
        )
        return StoreOrder.objects.get(pk=existing.pk), guest_token, False

    requested = payload["lines"]
    product_ids = [item["product_id"] for item in requested]
    if len(product_ids) != len(set(product_ids)):
        raise StoreOrderError("store_duplicate_product")
    products = list(
        Product.objects.select_for_update()
        .filter(
            pk__in=product_ids,
            is_active=True,
            is_published=True,
            retail_price_vnd__lte=PUBLIC_STORE_MAX_PRICE_VND,
            category__is_active=True,
        )
        .filter(
            Q(category__parent__isnull=True) | Q(category__parent__is_active=True),
            Q(category__parent__parent__isnull=True) | Q(category__parent__parent__is_active=True),
        )
        .prefetch_related("translations")
        .order_by("pk")
    )
    by_id = {item.pk: item for item in products}
    if len(products) != len(product_ids):
        raise StoreOrderError("store_product_changed")
    address, area, address_updated_at = _address_for_checkout(user, payload.get("guest"))
    if user and user.is_authenticated:
        expected_address_updated_at = payload.get("shipping_address_updated_at")
        if expected_address_updated_at is None or expected_address_updated_at != address_updated_at:
            raise StoreOrderError(
                "store_requote_required", context={"reason": "shipping_address_changed"}
            )
    membership = _active_membership_for_user(user, lock=True)
    has_csa_product = any(by_id[item["product_id"]].csa_quota_group_id for item in requested)
    free_shipping = membership is not None and has_csa_product
    shipping_fee = 0 if free_shipping else area.shipping_fee_vnd

    offers = list(
        FarmProduct.objects.select_for_update().filter(product_id__in=product_ids).order_by("pk")
    )
    list(
        Farm.objects.select_for_update()
        .filter(pk__in=[offer.farm_id for offer in offers])
        .order_by("pk")
    )
    stock_totals, _batches = active_product_stock(product_ids, lock=True)
    active_reserved = _active_reserved_stock(product_ids)
    for item in requested:
        available = stock_totals[item["product_id"]] - active_reserved.get(item["product_id"], 0)
        product = by_id[item["product_id"]]
        if item["expected_unit_price_vnd"] != product.retail_price_vnd:
            raise StoreOrderError(
                "store_requote_required",
                context={
                    "reason": "price_changed",
                    "product_id": str(product.pk),
                    "unit_price_vnd": product.retail_price_vnd,
                },
            )
        if item["expected_available_quantity"] != max(0, available):
            raise StoreOrderError(
                "store_requote_required",
                context={
                    "reason": "stock_changed",
                    "product_id": str(product.pk),
                    "available_quantity": max(0, available),
                },
            )
        if item["quantity"] > available:
            raise StoreOrderError(
                "store_stock_changed",
                context={
                    "product_id": str(item["product_id"]),
                    "available_quantity": max(0, available),
                },
            )

    group_ids = {
        by_id[item["product_id"]].csa_quota_group_id
        for item in requested
        if item["use_quota"] and by_id[item["product_id"]].csa_quota_group_id
    }
    if membership and group_ids:
        ensure_due_cycles(membership)
    quota_rows = _available_quota_rows(membership, group_ids) if membership else []
    quota_by_group = defaultdict(list)
    for quota, available in quota_rows:
        quota_by_group[quota.product_snapshot.product_id].append([quota, available])

    line_values = []
    subtotal = 0
    for position, item in enumerate(requested):
        product = by_id[item["product_id"]]
        quota_quantity = 0
        reservations = []
        if item["use_quota"]:
            if membership is None or product.csa_quota_group_id is None:
                raise StoreOrderError(
                    "store_quota_unavailable", context={"product_id": str(product.pk)}
                )
            needed_per_item = product.csa_quota_cost
            available_units = sum(row[1] for row in quota_by_group[product.csa_quota_group_id])
            quota_quantity = min(item["quantity"], int(available_units // needed_per_item))
            units_needed = quota_quantity * needed_per_item
            for quota_row in quota_by_group[product.csa_quota_group_id]:
                quota, available = quota_row
                take = min(available, units_needed)
                if take:
                    reservations.append((quota, take))
                    quota_row[1] -= take
                    units_needed -= take
                if not units_needed:
                    break
        cash_quantity = item["quantity"] - quota_quantity
        if item["expected_quota_quantity"] != quota_quantity:
            raise StoreOrderError(
                "store_requote_required",
                context={
                    "reason": "quota_changed",
                    "product_id": str(product.pk),
                    "quota_quantity": quota_quantity,
                },
            )
        cash_subtotal = cash_quantity * product.retail_price_vnd
        subtotal += cash_subtotal
        line_values.append(
            (position, item, product, quota_quantity, cash_quantity, cash_subtotal, reservations)
        )

    total = subtotal + shipping_fee
    if (
        payload["expected_shipping_fee_vnd"] != shipping_fee
        or payload["expected_total_vnd"] != total
    ):
        raise StoreOrderError(
            "store_requote_required",
            context={
                "reason": "total_changed",
                "shipping_fee_vnd": shipping_fee,
                "total_vnd": total,
            },
        )
    payment = None
    if total:
        payment = (
            MembershipPaymentSettings.objects.select_for_update().filter(key="default").first()
        )
        if payment is None or not payment.direct_transfer_enabled:
            raise StoreOrderError("store_payment_unavailable")
    order_id = uuid.uuid4()
    guest_token = (
        _guest_access_token(order_id, key) if not user or not user.is_authenticated else None
    )
    now = timezone.now()
    order = StoreOrder.objects.create(
        id=order_id,
        reference_code=_reference_code(),
        buyer_user=user if user and user.is_authenticated else None,
        referral_rewards_eligible=store_referral_rewards_active(),
        create_idempotency_key=key,
        request_fingerprint=fingerprint,
        locale=payload["locale"],
        **address,
        status=StoreOrderStatus.PAYMENT_PENDING if total else StoreOrderStatus.PENDING_REVIEW,
        payment_status=StorePaymentStatus.UNCONFIRMED if total else StorePaymentStatus.NOT_REQUIRED,
        subtotal_vnd=subtotal,
        shipping_fee_vnd=shipping_fee,
        total_vnd=total,
        shipping_discount_reason="active_csa_with_csa_product" if free_shipping else "",
        bank_bin_snapshot=payment.bank_bin if payment else "",
        bank_name_snapshot=payment.bank_name if payment else "",
        account_number_snapshot=payment.account_number if payment else "",
        account_name_snapshot=payment.account_name if payment else "",
        payment_config_version=payment.version if payment else None,
        transfer_content=_reference_code() if payment else None,
        guest_access_token_hash=_hash(guest_token) if guest_token else "",
        expires_at=now + (PAYMENT_WINDOW if total else REVIEW_WINDOW),
    )
    for (
        position,
        item,
        product,
        quota_quantity,
        cash_quantity,
        cash_subtotal,
        reservations,
    ) in line_values:
        translation = _translation(product, payload["locale"])
        line = StoreOrderLine.objects.create(
            order=order,
            product=product,
            position=position,
            sku_snapshot=product.sku,
            name_snapshot=translation.name if translation else product.sku,
            unit_size_snapshot=product.unit_size,
            unit_label_snapshot=product.unit_label_vi
            if payload["locale"] == "vi"
            else product.unit_label_en,
            unit_price_vnd_snapshot=product.retail_price_vnd,
            quantity=item["quantity"],
            quota_group=product.csa_quota_group,
            quota_cost_snapshot=product.csa_quota_cost,
            quota_quantity=quota_quantity,
            quota_units=quota_quantity * product.csa_quota_cost,
            cash_quantity=cash_quantity,
            cash_subtotal_vnd=cash_subtotal,
        )
        StoreStockReservation.objects.create(line=line, product=product, quantity=item["quantity"])
        StoreQuotaReservation.objects.bulk_create(
            [
                StoreQuotaReservation(line=line, cycle_quota=quota, units=units)
                for quota, units in reservations
            ]
        )
    return order, guest_token, True


def _release_reservations(order, reason):
    _lock_order_memberships(order)
    now = timezone.now()
    StoreStockReservation.objects.filter(line__order=order, released_at__isnull=True).update(
        released_at=now, release_reason=reason
    )
    quota_reservations = list(
        StoreQuotaReservation.objects.select_for_update()
        .filter(line__order=order, released_at__isnull=True, consumed_at__isnull=True)
        .order_by("cycle_quota_id", "pk")
    )
    quota_ids = sorted({item.cycle_quota_id for item in quota_reservations})
    if quota_ids:
        quotas = {
            quota.pk: quota
            for quota in MembershipCycleQuota.objects.select_for_update()
            .select_related("cycle__membership")
            .filter(pk__in=quota_ids)
            .order_by("pk")
        }
        today = timezone.localdate()
        for quota_id in quota_ids:
            quota = quotas[quota_id]
            if (
                quota.cycle.membership.quota_policy_snapshot == QuotaPolicy.EXPIRE
                and quota.cycle.end_date <= today
            ):
                held_elsewhere = (
                    StoreQuotaReservation.objects.filter(
                        cycle_quota_id=quota_id,
                        released_at__isnull=True,
                        consumed_at__isnull=True,
                        line__order__status__in=[
                            StoreOrderStatus.PAYMENT_PENDING,
                            StoreOrderStatus.PENDING_REVIEW,
                        ],
                        line__order__expires_at__gt=now,
                    )
                    .exclude(line__order=order)
                    .aggregate(total=Round(Sum("units"), precision=2))["total"]
                    or 0
                )
                kept = min(quota.remaining_units, held_elsewhere)
                quota.expired_units += quota.remaining_units - kept
                quota.remaining_units = kept
                quota.save(update_fields=["expired_units", "remaining_units"])
        StoreQuotaReservation.objects.filter(
            pk__in=[item.pk for item in quota_reservations]
        ).update(released_at=now)


def _expire_locked_order(order, *, now):
    """Expire a pending order once; caller holds its product and membership locks."""
    if order.status not in {StoreOrderStatus.PAYMENT_PENDING, StoreOrderStatus.PENDING_REVIEW}:
        return False
    if order.expires_at is None or now < order.expires_at:
        return False
    _release_reservations(order, "expired")
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = now
    order.cancellation_reason = "expired"
    if order.payment_status == StorePaymentStatus.RECEIVED:
        order.payment_status = StorePaymentStatus.REFUND_REQUIRED
    order.save(
        update_fields=[
            "status",
            "cancelled_at",
            "cancellation_reason",
            "payment_status",
            "updated_at",
        ]
    )
    return True


@transaction.atomic
def expire_order_if_needed(order_id, *, now=None):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    if order.status not in {StoreOrderStatus.PAYMENT_PENDING, StoreOrderStatus.PENDING_REVIEW}:
        return order, False
    _lock_order_products(order)
    _lock_order_memberships(order)
    return order, _expire_locked_order(order, now=now if now is not None else timezone.now())


def _lock_order_products(order):
    product_ids = list(order.lines.order_by("product_id").values_list("product_id", flat=True))
    list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))


def _lock_order_memberships(order):
    # CSA services lock Membership before quota rows. Take that same lock before
    # inventory sources too, matching checkout and avoiding cross-product cycles.
    membership_ids = StoreQuotaReservation.objects.filter(line__order=order).values_list(
        "cycle_quota__cycle__membership_id", flat=True
    )
    list(Membership.objects.select_for_update().filter(pk__in=membership_ids).order_by("pk"))


def _lock_order_quotas(order):
    _lock_order_memberships(order)
    quota_ids = list(
        StoreQuotaReservation.objects.filter(
            line__order=order, released_at__isnull=True, consumed_at__isnull=True
        )
        .order_by("cycle_quota_id")
        .values_list("cycle_quota_id", flat=True)
    )
    list(MembershipCycleQuota.objects.select_for_update().filter(pk__in=quota_ids).order_by("pk"))


def verify_order_access(order, *, user=None, guest_token=""):
    if order.buyer_user_id:
        if not user or not user.is_authenticated or order.buyer_user_id != user.pk:
            raise StoreOrderError("store_order_not_found")
    elif not guest_token or not secrets.compare_digest(
        order.guest_access_token_hash, _hash(guest_token)
    ):
        raise StoreOrderError("store_order_not_found")


def report_store_transfer(*, order_id, user, guest_token=""):
    order = StoreOrder.objects.get(pk=order_id)
    verify_order_access(order, user=user, guest_token=guest_token)
    order, expired = expire_order_if_needed(order.pk)
    if expired:
        raise StoreOrderError("store_order_expired")
    with transaction.atomic():
        order = StoreOrder.objects.select_for_update().get(pk=order_id)
        verify_order_access(order, user=user, guest_token=guest_token)
        _lock_order_products(order)
        _lock_order_quotas(order)
        if order.status == StoreOrderStatus.PENDING_REVIEW and order.transfer_reported_at:
            return order
        if order.status != StoreOrderStatus.PAYMENT_PENDING:
            raise StoreOrderError("store_order_state_conflict")
        now = timezone.now()
        if order.expires_at is None or now >= order.expires_at:
            # A concurrent expiry won the transition; finish it outside this rollback path.
            expired_id = order.pk
        else:
            order.status = StoreOrderStatus.PENDING_REVIEW
            order.transfer_reported_at = now
            order.expires_at = now + REVIEW_WINDOW
            order.save(update_fields=["status", "transfer_reported_at", "expires_at", "updated_at"])
            return order
    expire_order_if_needed(expired_id)
    raise StoreOrderError("store_order_expired")


@transaction.atomic
def cancel_pending_store_order(*, order_id, user, guest_token, idempotency_key):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    verify_order_access(order, user=user, guest_token=guest_token)
    fingerprint = request_fingerprint({})
    if _action_replay(order, "buyer_cancel", idempotency_key, fingerprint):
        return order
    if order.status != StoreOrderStatus.PAYMENT_PENDING:
        raise StoreOrderError("store_order_state_conflict")
    _release_reservations(order, "buyer_cancelled")
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancellation_reason = "buyer_cancelled"
    order.expires_at = None
    if order.payment_status == StorePaymentStatus.RECEIVED:
        order.payment_status = StorePaymentStatus.REFUND_REQUIRED
    order.save(
        update_fields=[
            "status",
            "cancelled_at",
            "cancellation_reason",
            "expires_at",
            "payment_status",
            "updated_at",
        ]
    )
    _record_action(order, "buyer_cancel", idempotency_key, fingerprint, None)
    return order


def _batch_available(batch, net_sold):
    return max(0, batch.quantity_units - net_sold.get(batch.pk, 0))


def allocation_suggestion(order):
    return source_inventory(order)["suggestion"]


def source_inventory(order):
    product_ids = list(order.lines.values_list("product_id", flat=True))
    _totals, batches = active_product_stock(product_ids)
    net_sold = _net_sold_by_batch([item.pk for item in batches])
    by_product = defaultdict(list)
    for batch in batches:
        by_product[batch.farm_product.product_id].append(batch)
    suggestion = []
    candidates = []
    lines = list(order.lines.order_by("position", "id"))
    for line in lines:
        needed = line.quantity
        for batch in by_product[line.product_id]:
            available = _batch_available(batch, net_sold)
            candidates.append(
                {
                    "line_id": str(line.pk),
                    "product_id": str(line.product_id),
                    "farm_id": str(batch.farm_product.farm_id),
                    "farm_name": (
                        batch.farm_product.farm.name_vi
                        if order.locale == "vi"
                        else batch.farm_product.farm.name_en
                    ),
                    "batch_id": str(batch.pk),
                    "start_date": batch.start_date.isoformat(),
                    "end_date": batch.end_date.isoformat(),
                    "available_quantity": available,
                }
            )
            take = min(needed, available)
            if take:
                suggestion.append(
                    {"line_id": str(line.pk), "batch_id": str(batch.pk), "quantity": take}
                )
                needed -= take
            if not needed:
                # Keep iterating so the read contract still exposes alternative sources.
                continue
    reserved = _active_reserved_stock(product_ids)
    current_order_reserved = {
        row["product_id"]: row["total"]
        for row in StoreStockReservation.objects.filter(
            line__order=order,
            released_at__isnull=True,
            line__order__status__in=[
                StoreOrderStatus.PAYMENT_PENDING,
                StoreOrderStatus.PENDING_REVIEW,
            ],
            line__order__expires_at__gt=timezone.now(),
        )
        .values("product_id")
        .annotate(total=Sum("quantity"))
    }
    return {
        "refresh_required_before_approve": True,
        "product_reservations": [
            {
                "product_id": str(product_id),
                "reserved_quantity": reserved.get(product_id, 0),
                "this_order_quantity": current_order_reserved.get(product_id, 0),
                "other_orders_quantity": max(
                    0, reserved.get(product_id, 0) - current_order_reserved.get(product_id, 0)
                ),
            }
            for product_id in sorted(set(product_ids), key=str)
        ],
        "candidates": candidates,
        "suggestion": suggestion,
    }


def _action_replay(order, action, key, fingerprint):
    existing = StoreOrderAction.objects.filter(
        order=order, action=action, idempotency_key=key
    ).first()
    if existing and existing.request_fingerprint != fingerprint:
        raise StoreOrderError("store_idempotency_conflict")
    return existing is not None


def _consume_reserved_quota(order, actor):
    reservations = list(
        StoreQuotaReservation.objects.select_for_update()
        .filter(line__order=order, released_at__isnull=True, consumed_at__isnull=True)
        .select_related("cycle_quota__cycle__membership__customer", "cycle_quota__product_snapshot")
        .order_by("cycle_quota__cycle__sequence", "cycle_quota_id")
    )
    if not reservations:
        return
    membership = reservations[0].cycle_quota.cycle.membership
    usage = MembershipUsageOrder.objects.create(
        store_order=order,
        membership=membership,
        customer=membership.customer,
        idempotency_key=uuid.uuid4(),
        note=f"Store order {order.reference_code}",
        created_by=actor,
    )
    by_snapshot = defaultdict(list)
    for reservation in reservations:
        by_snapshot[reservation.cycle_quota.product_snapshot_id].append(reservation)
    for snapshot_id, items in by_snapshot.items():
        line = MembershipUsageLine.objects.create(
            order=usage,
            product_snapshot_id=snapshot_id,
            units=sum(item.units for item in items),
        )
        by_quota = {}
        for reservation in items:
            quota = reservation.cycle_quota
            entry = by_quota.setdefault(quota.pk, [quota, 0])
            entry[1] += reservation.units
        for quota, units in by_quota.values():
            if quota.remaining_units < units:
                raise StoreOrderError("store_quota_changed")
            # The cycle quota is already locked; subtract Decimals in Python so
            # SQLite numeric affinity cannot leave a tiny nonzero balance.
            quota.remaining_units -= units
            quota.save(update_fields=["remaining_units"])
            MembershipUsageAllocation.objects.create(line=line, cycle_quota=quota, units=units)
    StoreQuotaReservation.objects.filter(pk__in=[item.pk for item in reservations]).update(
        consumed_at=timezone.now()
    )


@transaction.atomic
def _approve_store_order_atomic(*, order_id, actor, idempotency_key, allocations):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint({"allocations": allocations})
    if _action_replay(order, "approve", idempotency_key, fingerprint):
        return order, False
    allocation_pairs = [(item["line_id"], item["batch_id"]) for item in allocations]
    if len(allocation_pairs) != len(set(allocation_pairs)):
        raise StoreOrderError("store_allocation_duplicate")
    _lock_order_products(order)
    _lock_order_memberships(order)
    if _expire_locked_order(order, now=timezone.now()):
        return order, True
    if order.status != StoreOrderStatus.PENDING_REVIEW:
        raise StoreOrderError("store_order_state_conflict")
    lines = {line.pk: line for line in order.lines.select_for_update().all()}
    # Lock Product -> Membership -> FarmProduct -> Farm -> stock batch -> quota rows.
    batch_ids = [item["batch_id"] for item in allocations]
    batch_sources = list(
        FarmProductStockBatch.objects.filter(pk__in=batch_ids)
        .order_by("pk")
        .values("farm_product_id", "farm_product__farm_id")
    )
    list(
        FarmProduct.objects.select_for_update()
        .filter(pk__in=[item["farm_product_id"] for item in batch_sources])
        .order_by("pk")
    )
    list(
        Farm.objects.select_for_update()
        .filter(pk__in=[item["farm_product__farm_id"] for item in batch_sources])
        .order_by("pk")
    )
    batches = {
        item.pk: item
        for item in FarmProductStockBatch.objects.select_for_update()
        .select_related("farm_product__farm")
        .filter(pk__in=batch_ids)
        .order_by("pk")
    }
    _lock_order_quotas(order)
    # Waiting for a source or quota lock may have crossed the deadline. This is
    # the approval decision time; keep it for the recorded approval timestamp.
    decision_at = timezone.now()
    if _expire_locked_order(order, now=decision_at):
        return order, True
    if len(batches) != len(set(batch_ids)):
        raise StoreOrderError("store_allocation_stale")
    net_sold = _net_sold_by_batch(batch_ids)
    allocated_by_line = defaultdict(int)
    allocated_by_batch = defaultdict(int)
    for item in allocations:
        line = lines.get(item["line_id"])
        batch = batches.get(item["batch_id"])
        if line is None or batch is None or batch.farm_product.product_id != line.product_id:
            raise StoreOrderError("store_allocation_stale")
        today = timezone.localdate(decision_at)
        if (
            batch.cancelled_at
            or not batch.farm_product.is_active
            or not batch.farm_product.farm.is_active
            or not batch.start_date <= today <= batch.end_date
        ):
            raise StoreOrderError("store_allocation_stale")
        allocated_by_line[line.pk] += item["quantity"]
        allocated_by_batch[batch.pk] += item["quantity"]
    if any(allocated_by_line[line.pk] != line.quantity for line in lines.values()):
        raise StoreOrderError("store_allocation_incomplete")
    for batch_id, quantity in allocated_by_batch.items():
        if quantity > _batch_available(batches[batch_id], net_sold):
            raise StoreOrderError(
                "store_allocation_stale",
                context={
                    "batch_id": str(batch_id),
                    "available_quantity": _batch_available(batches[batch_id], net_sold),
                },
            )
    for item in allocations:
        allocation = StoreStockAllocation.objects.create(
            line=lines[item["line_id"]], batch=batches[item["batch_id"]], quantity=item["quantity"]
        )
        StoreStockMovement.objects.create(
            allocation=allocation,
            batch=allocation.batch,
            line=allocation.line,
            kind=StoreStockMovementKind.SALE,
            quantity=allocation.quantity,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    _consume_reserved_quota(order, actor)
    _release_reservations(order, "approved")
    order.status = StoreOrderStatus.PENDING_DELIVERY
    order.approved_at = decision_at
    order.approved_by = actor
    order.expires_at = None
    if order.total_vnd:
        order.payment_status = StorePaymentStatus.RECEIVED
        order.payment_received_at = order.payment_received_at or decision_at
    order.save(
        update_fields=[
            "status",
            "approved_at",
            "approved_by",
            "expires_at",
            "payment_status",
            "payment_received_at",
            "updated_at",
        ]
    )
    StoreOrderAction.objects.create(
        order=order,
        action="approve",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        actor=actor,
        context={"allocations": _jsonable(allocations)},
    )
    return order, False


def approve_store_order(*, order_id, actor, idempotency_key, allocations):
    order, expired = _approve_store_order_atomic(
        order_id=order_id,
        actor=actor,
        idempotency_key=idempotency_key,
        allocations=allocations,
    )
    if expired:
        raise StoreOrderError("store_order_expired")
    return order


def expire_store_orders(*, limit=200, now=None):
    now = now or timezone.now()
    ids = list(
        StoreOrder.objects.filter(
            status__in=[StoreOrderStatus.PAYMENT_PENDING, StoreOrderStatus.PENDING_REVIEW],
            expires_at__lte=now,
        )
        .order_by("expires_at")
        .values_list("pk", flat=True)[:limit]
    )
    count = 0
    for order_id in ids:
        _order, changed = expire_order_if_needed(order_id, now=now)
        count += int(changed)
    return count


def _record_action(order, action, key, fingerprint, actor, *, context=None):
    StoreOrderAction.objects.create(
        order=order,
        action=action,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        actor=actor,
        context=context or {},
    )


def _reverse_store_quota(order, actor, reason):
    _lock_order_memberships(order)
    usage = MembershipUsageOrder.objects.select_for_update().filter(store_order=order).first()
    if usage is None or usage.status == UsageOrderStatus.REVERSED:
        return
    allocations = list(
        MembershipUsageAllocation.objects.filter(line__order=usage)
        .select_related("cycle_quota__cycle__membership")
        .order_by("cycle_quota_id")
    )
    quotas = {
        quota.pk: quota
        for quota in MembershipCycleQuota.objects.select_for_update().filter(
            pk__in=[item.cycle_quota_id for item in allocations]
        )
    }
    today = timezone.localdate()
    for allocation in allocations:
        quota = quotas[allocation.cycle_quota_id]
        membership = allocation.cycle_quota.cycle.membership
        if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE and quota.cycle.end_date <= today:
            quota.expired_units += allocation.units
            quota.save(update_fields=["expired_units"])
        else:
            quota.remaining_units += allocation.units
            quota.save(update_fields=["remaining_units"])
    MembershipUsageReversal.objects.create(order=usage, actor=actor, reason=reason)
    usage.status = UsageOrderStatus.REVERSED
    usage.reversed_at = timezone.now()
    usage.save(update_fields=["status", "reversed_at"])


@transaction.atomic
def cancel_store_order(*, order_id, actor, idempotency_key, reason, reconciliation_note=""):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint(
        {"reason": reason.strip(), "reconciliation_note": reconciliation_note.strip()}
    )
    if _action_replay(order, "cancel", idempotency_key, fingerprint):
        return order
    if order.status in {StoreOrderStatus.CANCELLED, StoreOrderStatus.DELIVERED}:
        raise StoreOrderError("store_order_state_conflict")
    was_approved = order.status == StoreOrderStatus.PENDING_DELIVERY
    if was_approved:
        _reverse_store_quota(order, actor, reason.strip())
    else:
        _release_reservations(order, "cancelled")
    now = timezone.now()
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = now
    order.cancelled_by = actor
    order.cancellation_reason = reason.strip()
    order.reconciliation_note = reconciliation_note.strip()
    order.expires_at = None
    if order.payment_status == StorePaymentStatus.RECEIVED:
        order.payment_status = StorePaymentStatus.REFUND_REQUIRED
    order.save(
        update_fields=[
            "status",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
            "reconciliation_note",
            "expires_at",
            "payment_status",
            "updated_at",
        ]
    )
    _record_action(
        order,
        "cancel",
        idempotency_key,
        fingerprint,
        actor,
        context={"reason": reason.strip(), "reconciliation_note": reconciliation_note.strip()},
    )
    return order


@transaction.atomic
def record_store_payment(*, order_id, actor, idempotency_key, note=""):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint({"note": note.strip()})
    if _action_replay(order, "record_payment", idempotency_key, fingerprint):
        return order
    if order.total_vnd == 0 or order.payment_status == StorePaymentStatus.REFUNDED:
        raise StoreOrderError("store_payment_state_conflict")
    lock_store_referral_referrer(order)
    order.payment_received_at = order.payment_received_at or timezone.now()
    order.payment_status = (
        StorePaymentStatus.REFUND_REQUIRED
        if order.status == StoreOrderStatus.CANCELLED
        else StorePaymentStatus.RECEIVED
    )
    if note.strip():
        order.reconciliation_note = note.strip()
    order.save(
        update_fields=["payment_received_at", "payment_status", "reconciliation_note", "updated_at"]
    )
    _record_action(
        order,
        "record_payment",
        idempotency_key,
        fingerprint,
        actor,
        context={"note": note.strip(), "amount_vnd": order.total_vnd},
    )
    evaluate_store_referral_reward(order_id=order.pk)
    return order


@transaction.atomic
def refund_store_order(*, order_id, actor, idempotency_key, amount_vnd, note):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint({"amount_vnd": amount_vnd, "note": note.strip()})
    if _action_replay(order, "refund", idempotency_key, fingerprint):
        return order
    if (
        order.status != StoreOrderStatus.CANCELLED
        or order.payment_status != StorePaymentStatus.REFUND_REQUIRED
        or amount_vnd != order.total_vnd
    ):
        raise StoreOrderError("store_refund_state_conflict")
    order.payment_status = StorePaymentStatus.REFUNDED
    order.refunded_at = timezone.now()
    order.refunded_by = actor
    order.refund_amount_vnd = amount_vnd
    order.refund_note = note.strip()
    order.save(
        update_fields=[
            "payment_status",
            "refunded_at",
            "refunded_by",
            "refund_amount_vnd",
            "refund_note",
            "updated_at",
        ]
    )
    _record_action(
        order,
        "refund",
        idempotency_key,
        fingerprint,
        actor,
        context={"amount_vnd": amount_vnd, "note": note.strip()},
    )
    return order


@transaction.atomic
def deliver_store_order(*, order_id, actor, idempotency_key):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint({})
    if _action_replay(order, "deliver", idempotency_key, fingerprint):
        return order
    if order.status != StoreOrderStatus.PENDING_DELIVERY:
        raise StoreOrderError("store_order_state_conflict")
    lock_store_referral_referrer(order)
    order.status = StoreOrderStatus.DELIVERED
    order.delivered_at = timezone.now()
    order.delivered_by = actor
    order.save(update_fields=["status", "delivered_at", "delivered_by", "updated_at"])
    _record_action(order, "deliver", idempotency_key, fingerprint, actor)
    evaluate_store_referral_reward(order_id=order.pk)
    return order


def restock_unavailable_reason(order, batch, remaining, *, as_of=None):
    """One eligibility rule for the Admin projection and the locked restock write."""
    if order.status != StoreOrderStatus.CANCELLED:
        return "order_not_cancelled"
    if order.approved_at is None:
        return "order_not_approved"
    if remaining <= 0:
        return "fully_restocked"
    if batch.cancelled_at:
        return "batch_cancelled"
    if not batch.farm_product.is_active:
        return "offer_inactive"
    if not batch.farm_product.farm.is_active:
        return "farm_inactive"
    today = as_of or timezone.localdate()
    if today < batch.start_date:
        return "batch_not_started"
    if today > batch.end_date:
        return "batch_expired"
    return None


@transaction.atomic
def restock_store_order(*, order_id, actor, idempotency_key, allocations):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint({"allocations": allocations})
    if _action_replay(order, "restock", idempotency_key, fingerprint):
        return order
    allocation_ids = [item["allocation_id"] for item in allocations]
    if len(allocation_ids) != len(set(allocation_ids)):
        raise StoreOrderError("store_restock_duplicate")
    if order.status != StoreOrderStatus.CANCELLED or order.approved_at is None:
        raise StoreOrderError("store_restock_state_conflict")
    rows = {
        item.pk: item
        for item in StoreStockAllocation.objects.select_for_update()
        .select_related("batch__farm_product__farm")
        .filter(pk__in=allocation_ids, line__order=order)
        .order_by("pk")
    }
    if len(rows) != len(set(allocation_ids)):
        raise StoreOrderError("store_restock_invalid")
    for item in allocations:
        allocation = rows[item["allocation_id"]]
        batch = allocation.batch
        today = timezone.localdate()
        already = (
            allocation.movements.filter(kind=StoreStockMovementKind.RESTOCK).aggregate(
                total=Sum("quantity")
            )["total"]
            or 0
        )
        unavailable_reason = restock_unavailable_reason(
            order, batch, allocation.quantity - already, as_of=today
        )
        if item["quantity"] > allocation.quantity - already or unavailable_reason:
            raise StoreOrderError(
                "store_restock_invalid",
                context={"allocation_id": str(allocation.pk), "reason": unavailable_reason},
            )
    for item in allocations:
        allocation = rows[item["allocation_id"]]
        StoreStockMovement.objects.create(
            allocation=allocation,
            batch=allocation.batch,
            line=allocation.line,
            kind=StoreStockMovementKind.RESTOCK,
            quantity=item["quantity"],
            idempotency_key=idempotency_key,
            actor=actor,
        )
    _record_action(
        order,
        "restock",
        idempotency_key,
        fingerprint,
        actor,
        context={"allocations": _jsonable(allocations)},
    )
    return order
