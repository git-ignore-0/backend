import hashlib
import json
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import transaction
from django.db.models import F, Sum
from django.db.models.functions import Round
from django.utils import timezone
from django.utils.crypto import salted_hmac

from commerce.inventory import (
    WAITING_STATES,
    active_stock_batches,
    effective_store_stock_reservations,
    inventory_capacity,
)
from commerce.models import (
    STORE_CHECKOUT_MAX_QUANTITY,
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
from commerce.public_catalog import _public_product_filter
from commerce.shipping import (
    active_shipping_area,
    shipping_address_version,
    shipping_recipient_details,
)
from commerce.store_delivery import (
    companion_benefits,
    lock_companion_sources,
    validate_delivery_date,
)
from config.phones import InvalidPhoneNumber, normalize_vietnam_phone
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from farms.stock import (
    batch_net_sold,
    batch_remaining_units,
    is_current_stock_batch,
    product_stock_totals_query,
    stock_movement_totals,
)
from identity.locks import lock_user_for_shared_read
from members.administrative_units import province_exists, ward_choices
from memberships.coupons import (
    CouponError,
    check_coupon_customer,
    coupon_for_code,
    store_coupon_snapshot,
)
from memberships.models import (
    CouponUsage,
    Membership,
    MembershipCycle,
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
from memberships.services import (
    add_months,
    due_cycle_count,
    ensure_due_cycles,
    quota_availability_rows,
    sync_membership_status,
)
from memberships.voucher_usage import consume_coupon, lock_coupons, release_coupon
from referrals.store_rewards import (
    evaluate_store_referral_reward,
    lock_store_referral_referrer,
    store_referral_rewards_active,
)

REVIEW_WINDOW = timedelta(hours=8)
NEW_PAYMENT_WINDOW = timedelta(hours=12)
VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")


def delivery_end(order):
    return datetime.combine(order.delivery_date + timedelta(days=1), time.min, VIETNAM)


def _workflow_deadline(order, started_at):
    return min(started_at + NEW_PAYMENT_WINDOW, delivery_end(order))


def _expiry_reason(order):
    if order.workflow_version == 1:
        return "expired"
    end = delivery_end(order)
    # The stored deadline is the decision, even when reconciliation runs late.
    if end <= order.expires_at:
        return "delivery_date_expired"
    return (
        "payment_review_expired"
        if order.status == StoreOrderStatus.PAYMENT_REVIEW
        else "payment_expired"
    )


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
        # Dates also cover an expired active row before the status worker runs.
        frozen = (
            rows.filter(
                customer__user=user,
                end_date__lte=today,
                extension_end_date__gte=today,
            )
            .exclude(status=MembershipStatus.REVOKED)
            .exists()
        )
        if frozen:
            raise StoreOrderError("store_membership_frozen")
        return None
    if lock:
        membership = sync_membership_status(membership, as_of=today)
    eligible_statuses = (
        {MembershipStatus.ACTIVE} if lock else {MembershipStatus.ACTIVE, MembershipStatus.SCHEDULED}
    )
    return membership if membership.status in eligible_statuses else None


def _translation(product, locale):
    translations = {item.locale: item for item in product.translations.all()}
    return translations.get(locale) or translations.get("vi") or translations.get("en")


def _net_sold_by_batch(batch_ids):
    rows = stock_movement_totals(StoreStockMovement.objects.filter(batch_id__in=batch_ids))
    return defaultdict(int, rows.values_list("batch_id", "net_sold"))


def active_product_stock_totals(
    product_ids, *, now=None, exclude_source_id=None, exclude_farm_id=None
):
    """Read inventory totals without loading batch or supplier models."""
    batches = active_stock_batches(now=now).filter(farm_product__product_id__in=product_ids)
    if exclude_source_id is not None:
        batches = batches.exclude(farm_product_id=exclude_source_id)
    if exclude_farm_id is not None:
        batches = batches.exclude(farm_product__farm_id=exclude_farm_id)
    stock = product_stock_totals_query(batches)
    return dict(stock.values_list("farm_product__product_id", "total"))


def ensure_batch_change_preserves_reservations(
    batch, *, quantity_units=None, start_date=None, end_date=None, cancelling=False, now=None
):
    from copy import copy

    # All source writers and demand writers serialize on the Product row.
    now = now or timezone.now()
    today = timezone.localdate(now)
    source = batch.farm_product
    net_sold = _net_sold_by_batch([batch.pk]).get(batch.pk, 0)
    proposed = copy(batch)
    proposed.quantity_units = batch.quantity_units if quantity_units is None else quantity_units
    if proposed.quantity_units < net_sold:
        raise StoreOrderError("stock_sold")
    proposed.start_date = start_date if start_date is not None else batch.start_date
    proposed.end_date = end_date if end_date is not None else batch.end_date
    if cancelling:
        proposed.cancelled_at = now

    current_remaining = (
        max(0, batch.quantity_units - net_sold) if is_current_stock_batch(batch, today=today) else 0
    )
    next_remaining = (
        max(0, proposed.quantity_units - net_sold)
        if is_current_stock_batch(proposed, today=today)
        else 0
    )
    delta = next_remaining - current_remaining
    if delta >= 0:
        return

    product_id = source.product_id
    key = (product_id, source.farm_id)
    capacity = inventory_capacity([product_id], now=now, stock_adjustments={key: delta})
    if (
        capacity.physical.get(key, 0) < capacity.assigned.get(key, 0)
        or product_id in capacity.legacy_shortfalls
    ):
        raise StoreOrderError("stock_reserved")


def ensure_farm_deactivation_preserves_reservations(farm):
    sources = dict(farm.products.order_by().values_list("product_id", "removed_at"))
    if not sources:
        return
    now = timezone.now()
    capacity = inventory_capacity(sources, now=now, exclude_farm_id=farm.pk)
    if any(sources[product_id] is None for product_id in capacity.legacy_shortfalls) or any(
        capacity.assigned.get((product_id, farm.pk), 0) for product_id in sources
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


def _address_for_checkout(user, guest, *, lock=True):
    if user and user.is_authenticated:
        addresses = ShippingAddress.objects
        # The locking callers already hold a shared User lock. Fetch the
        # related User separately so FOR UPDATE applies to the address only.
        addresses = addresses.select_for_update() if lock else addresses.select_related("user")
        try:
            address = addresses.get(user=user)
        except ShippingAddress.DoesNotExist as error:
            raise StoreOrderError("store_shipping_address_required") from error
        contact = shipping_recipient_details(address)
        if not all(contact.values()):
            raise StoreOrderError("store_shipping_address_required")
        address_values, area = _validate_address(
            {
                **contact,
                "province_code": address.province_code,
                "ward_code": address.ward_code,
                "address": address.address,
            }
        )
        return address_values, area, address
    if not guest:
        raise StoreOrderError("store_guest_address_required")
    address_values, area = _validate_address(guest)
    return address_values, area, None


def _available_quota_rows(membership, group_ids, *, lock=True):
    rows = [
        (row, available)
        for row, _reserved, available, _current, _available_for_order in quota_availability_rows(
            membership,
            product_ids=group_ids,
            lock=lock,
        )
        if row.remaining_units > 0
    ]
    if not lock:
        # Project not-yet-materialized paid cycles without creating records.
        today = timezone.localdate()
        existing = set(membership.cycles.values_list("sequence", flat=True))
        for sequence in range(1, due_cycle_count(membership, as_of=today) + 1):
            start = add_months(membership.start_date, sequence - 1)
            end = min(add_months(membership.start_date, sequence), membership.end_date)
            if sequence in existing or (
                membership.quota_policy_snapshot == QuotaPolicy.EXPIRE and end <= today
            ):
                continue
            cycle = MembershipCycle(
                membership=membership, sequence=sequence, start_date=start, end_date=end
            )
            for snapshot in membership.product_snapshots.filter(product_id__in=group_ids):
                quota = MembershipCycleQuota(
                    cycle=cycle,
                    product_snapshot=snapshot,
                    granted_units=snapshot.quota_units_per_cycle,
                    remaining_units=snapshot.quota_units_per_cycle,
                )
                rows.append((quota, quota.remaining_units))
    return rows


def _reference_code():
    return f"SO{timezone.localdate():%y%m%d}{secrets.token_hex(4).upper()}"


def _lock_checkout_identity(user):
    """Serialize with identity updates without blocking actor FK shared locks.

    Quota mutations may already hold Membership when writing an actor FK. An
    exclusive User lock here would invert those locks. Store does not update User.
    """
    lock_user_for_shared_read(user.pk)


def _check_checkout_stock(requested, products, inventory, check_expected):
    assignments = {}
    for item in requested:
        quantity = item["quantity"]
        if type(quantity) is not int or not 1 <= quantity <= STORE_CHECKOUT_MAX_QUANTITY:
            raise StoreOrderError("store_order_invalid")
        product_id = item["product_id"]
        available = inventory.maximum(product_id)
        product = products[product_id]
        if check_expected and item["expected_unit_price_vnd"] != product.retail_price_vnd:
            raise StoreOrderError(
                "store_requote_required",
                context={
                    "reason": "price_changed",
                    "product_id": str(product_id),
                    "unit_price_vnd": product.retail_price_vnd,
                },
            )
        if check_expected and item["expected_available_quantity"] != available:
            raise StoreOrderError(
                "store_requote_required",
                context={
                    "reason": "stock_changed",
                    "product_id": str(product_id),
                    "available_quantity": available,
                },
            )
        farm_id = inventory.choose_farm(product_id, quantity)
        if farm_id is None:
            raise StoreOrderError(
                "store_stock_changed",
                context={"product_id": str(product_id), "available_quantity": available},
            )
        assignments[product_id] = farm_id
    return assignments


def calculate_store_quote(*, user, payload, lock=False, check_expected=False, draft_coupon=None):
    """Single authoritative cash/quota/shipping calculator; read-only when lock=False."""
    if payload.get("coupon_code") is not None and (user is None or not user.is_authenticated):
        raise CouponError("coupon_authentication_required")
    delivery_date = payload.get("delivery_date")
    validate_delivery_date(delivery_date)
    if lock:
        lock_companion_sources(user=user, delivery_date=delivery_date)
    requested = payload["lines"]
    product_ids = [item["product_id"] for item in requested]
    if len(product_ids) != len(set(product_ids)):
        raise StoreOrderError("store_duplicate_product")
    products = list(
        (Product.objects.select_for_update() if lock else Product.objects)
        .filter(_public_product_filter(), pk__in=product_ids)
        .prefetch_related("translations")
        .order_by("pk")
    )
    by_id = {item.pk: item for item in products}
    if len(products) != len(product_ids):
        raise StoreOrderError("store_product_changed")
    if lock and user and user.is_authenticated:
        # Match Product -> identity -> address/quota, but keep actor FK writes
        # compatible while cancellation/approval already holds Membership.
        _lock_checkout_identity(user)
    address, area, saved_address = _address_for_checkout(user, payload.get("guest"), lock=lock)
    address_updated_at = saved_address.updated_at if saved_address else None
    address_version = (
        shipping_address_version(
            saved_address,
            recipient_name=address["recipient_name_snapshot"],
            phone_number=address["phone_e164_snapshot"],
        )
        if saved_address
        else None
    )
    if check_expected:
        expected_version = payload.get("shipping_address_version")
        if saved_address is None and expected_version is not None:
            raise StoreOrderError("store_shipping_address_invalid")
        if saved_address is not None:
            if expected_version is None:
                # Timestamp-only attempts reviewed the recipient stored on the address.
                expected_version = shipping_address_version(
                    saved_address,
                    recipient_name=saved_address.recipient_name,
                    phone_number=saved_address.phone_number,
                )
            if (
                payload.get("shipping_address_updated_at") != address_updated_at
                or expected_version != address_version
            ):
                raise StoreOrderError(
                    "store_requote_required", context={"reason": "shipping_address_changed"}
                )
    membership = _active_membership_for_user(user, lock=lock)

    if lock:
        offers = list(
            FarmProduct.objects.select_for_update()
            .filter(product_id__in=product_ids)
            .order_by("pk")
        )
        list(
            Farm.objects.select_for_update()
            .filter(pk__in=[offer.farm_id for offer in offers])
            .order_by("pk")
        )
    # Product mutexes also serialize Farm status, lot writes and stock demand.
    stock_at = timezone.now()
    inventory = inventory_capacity(product_ids, now=stock_at)
    _check_checkout_stock(requested, by_id, inventory, check_expected)

    group_ids = {
        by_id[item["product_id"]].csa_quota_group_id
        for item in requested
        if item["use_quota"] and by_id[item["product_id"]].csa_quota_group_id
    }
    if lock and membership and group_ids:
        ensure_due_cycles(membership)
    quota_rows = _available_quota_rows(membership, group_ids, lock=lock) if membership else []
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
        if check_expected and item["expected_quota_quantity"] != quota_quantity:
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

    coupon = draft_coupon
    if coupon is None and payload.get("coupon_code") is not None:
        coupon = coupon_for_code(payload["coupon_code"], lock=lock)
    snapshot = None
    discount = 0
    if coupon is not None:
        if draft_coupon is None:
            check_coupon_customer(coupon=coupon, user=user)
        snapshot = store_coupon_snapshot(
            coupon=coupon,
            subtotal_vnd=subtotal,
            now=timezone.now(),
            check_availability=draft_coupon is None,
        )
        discount = snapshot["discount_amount_vnd"]
    # All catalog, identity, membership, inventory and coupon waits have finished.
    decision_time = timezone.now()
    validate_delivery_date(delivery_date, now=decision_time)
    benefit = companion_benefits(
        user=user, first=delivery_date, last=delivery_date, now=decision_time
    ).get(delivery_date)
    free_shipping = benefit is not None
    shipping_fee = 0 if free_shipping else area.shipping_fee_vnd
    total = subtotal - discount + shipping_fee
    if snapshot is not None:
        snapshot["total_vnd"] = total
    quote_material = {
        "coupon": snapshot,
        "configuration": {
            key: str(getattr(coupon, key))
            for key in (
                "application_scope",
                "is_active",
                "starts_at",
                "expires_at",
                "minimum_amount",
                "usage_limit",
            )
        }
        if coupon
        else None,
        "shipping_fee_vnd": shipping_fee,
        "delivery_date": delivery_date.isoformat(),
    }
    voucher_quote = (
        salted_hmac(
            "commerce.voucher-quote", json.dumps(quote_material, sort_keys=True)
        ).hexdigest()
        if coupon
        else None
    )
    if check_expected:
        if coupon is not None and payload.get("expected_voucher_quote") != voucher_quote:
            raise StoreOrderError("store_requote_required", context={"reason": "voucher_changed"})
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
    return {
        "address": address,
        "address_updated_at": address_updated_at,
        "address_version": address_version,
        "line_values": line_values,
        "subtotal": subtotal,
        "shipping_fee": shipping_fee,
        "total": total,
        "free_shipping": free_shipping,
        "delivery_date": delivery_date,
        "normal_shipping_fee": area.shipping_fee_vnd,
        "coupon": coupon,
        "snapshot": snapshot,
        "discount": discount,
        "voucher_quote": voucher_quote,
        "inventory": inventory,
    }


def preview_store_voucher(*, user, payload, draft_coupon=None):
    quote = calculate_store_quote(user=user, payload=payload, draft_coupon=draft_coupon)
    return {
        "delivery_date": quote["delivery_date"].isoformat(),
        "shipping_discount_reason": "csa_companion_delivery" if quote["free_shipping"] else None,
        "subtotal_vnd": str(quote["subtotal"]),
        "eligible_subtotal_vnd": str(quote["subtotal"]),
        "coupon": quote["snapshot"],
        "discount_amount_vnd": str(quote["discount"]),
        "shipping_fee_vnd": str(quote["shipping_fee"]),
        "total_vnd": str(quote["total"]),
        "voucher_quote": quote["voucher_quote"],
        "shipping_address_updated_at": quote["address_updated_at"].isoformat()
        if quote["address_updated_at"]
        else None,
        "shipping_address_version": quote["address_version"],
        "lines": [
            {
                "product_id": str(product.pk),
                "quantity": item["quantity"],
                "use_quota": item["use_quota"],
                "expected_unit_price_vnd": product.retail_price_vnd,
                "expected_available_quantity": quote["inventory"].maximum(product.pk),
                "expected_quota_quantity": quota_quantity,
            }
            for _, item, product, quota_quantity, *_ in quote["line_values"]
        ],
    }


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

    quote = calculate_store_quote(user=user, payload=payload, lock=True, check_expected=True)
    address = quote["address"]
    subtotal, shipping_fee, total = quote["subtotal"], quote["shipping_fee"], quote["total"]
    line_values = quote["line_values"]
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
    validate_delivery_date(quote["delivery_date"], now=now)
    # A batch date or effective hold deadline can change while waiting for payment config.
    products = {item[2].pk: item[2] for item in line_values}
    assignments = _check_checkout_stock(
        payload["lines"], products, inventory_capacity(products, now=now), True
    )
    # Payment configuration may have blocked after the quote was calculated.
    benefit = companion_benefits(
        user=user, first=quote["delivery_date"], last=quote["delivery_date"], now=now
    ).get(quote["delivery_date"])
    final_shipping = 0 if benefit else quote["normal_shipping_fee"]
    if final_shipping != shipping_fee:
        raise StoreOrderError(
            "store_requote_required",
            context={
                "reason": "total_changed",
                "shipping_fee_vnd": final_shipping,
                "total_vnd": total - shipping_fee + final_shipping,
            },
        )
    if benefit:
        benefit["decided_at"] = now.isoformat()
    order = StoreOrder.objects.create(
        id=order_id,
        reference_code=_reference_code(),
        buyer_user=user if user and user.is_authenticated else None,
        referral_rewards_eligible=store_referral_rewards_active(),
        create_idempotency_key=key,
        request_fingerprint=fingerprint,
        locale=payload["locale"],
        **address,
        workflow_version=2,
        status=StoreOrderStatus.PENDING_REVIEW,
        payment_status=StorePaymentStatus.UNCONFIRMED if total else StorePaymentStatus.NOT_REQUIRED,
        coupon=quote["coupon"],
        coupon_snapshot=quote["snapshot"],
        discount_amount_vnd=quote["discount"],
        subtotal_vnd=subtotal,
        shipping_fee_vnd=shipping_fee,
        total_vnd=total,
        delivery_date=quote["delivery_date"],
        shipping_benefit_snapshot=benefit,
        shipping_discount_reason="csa_companion_delivery" if benefit else "",
        bank_bin_snapshot=payment.bank_bin if payment else "",
        bank_name_snapshot=payment.bank_name if payment else "",
        account_number_snapshot=payment.account_number if payment else "",
        account_name_snapshot=payment.account_name if payment else "",
        payment_config_version=payment.version if payment else None,
        transfer_content=_reference_code() if payment else None,
        guest_access_token_hash=_hash(guest_token) if guest_token else "",
        expires_at=datetime.combine(quote["delivery_date"] + timedelta(days=1), time.min, VIETNAM),
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
        StoreStockReservation.objects.create(
            line=line, product=product, farm_id=assignments[product.pk], quantity=item["quantity"]
        )
        StoreQuotaReservation.objects.bulk_create(
            [
                StoreQuotaReservation(line=line, cycle_quota=quota, units=units)
                for quota, units in reservations
            ]
        )
    if quote["coupon"] is not None:
        consume_coupon(coupon=quote["coupon"], user=user, store_order=order)
    return order, guest_token, True


def _release_reservations(order, reason):
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
                            StoreOrderStatus.PAYMENT_REVIEW,
                        ],
                        line__order__expires_at__gt=now,
                    )
                    .exclude(line__order=order)
                    .aggregate(total=Round(Sum("units"), precision=2))["total"]
                    or 0
                )
                from memberships.csa_rounds import preserve_expiring_quota, reserved_quota

                if quota.cycle.membership.end_date <= today and preserve_expiring_quota(
                    quota.cycle.membership
                ):
                    continue
                held_elsewhere += reserved_quota([quota_id]).get(quota_id, 0)
                kept = min(quota.remaining_units, held_elsewhere)
                quota.expired_units += quota.remaining_units - kept
                quota.remaining_units = kept
                quota.save(update_fields=["expired_units", "remaining_units"])
        StoreQuotaReservation.objects.filter(
            pk__in=[item.pk for item in quota_reservations]
        ).update(released_at=now)


def _expire_locked_order(order, *, now):
    """Expire once after taking resource locks; do not reinterpret legacy deadlines."""
    if order.status not in WAITING_STATES:
        return False
    if order.expires_at is None or now < order.expires_at:
        return False
    reason = _expiry_reason(order)
    _cancel_resources(order, None, reason)
    _open_cancellation_reconciliation(order, None)
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = now
    order.cancellation_reason = reason
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
    release_coupon(store_order=order, reason=reason)
    return True


def expire_order_if_needed(order_id, *, now=None):
    # Reconcile before taking the StoreOrder lock: CSA locks round before order.
    from memberships.csa_rounds import process_round
    from memberships.models import CSASelection

    selection = CSASelection.objects.filter(order_id=order_id).values_list("round_id", "pk").first()
    if selection:
        round_id, selection_id = selection
        process_round(round_id, now=now, selection_ids=[selection_id])
    return _expire_store_order_atomic(order_id, now=now)


@transaction.atomic
def _expire_store_order_atomic(order_id, *, now=None):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    if order.status not in WAITING_STATES:
        return order, False
    _lock_cancellation_resources(order)
    return order, _expire_locked_order(
        order, now=max(now, timezone.now()) if now is not None else timezone.now()
    )


def _lock_order_products(order):
    product_ids = list(order.lines.order_by("product_id").values_list("product_id", flat=True))
    list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))


def _lock_order_memberships(order):
    from memberships.models import CSASelection

    # Native CSA orders need not have Store quota reservations. Resolve both
    # ownership paths under the Order lock before taking any source lock.
    store_memberships = (
        StoreQuotaReservation.objects.filter(line__order=order)
        .order_by()
        .values_list("cycle_quota__cycle__membership_id", flat=True)
    )
    csa_memberships = (
        CSASelection.objects.filter(order=order).order_by().values_list("membership_id", flat=True)
    )
    membership_ids = list(store_memberships.union(csa_memberships))
    list(Membership.objects.select_for_update().filter(pk__in=membership_ids).order_by("pk"))


def _lock_order_quotas(order):
    quota_ids = list(
        # Committed quota also needs its lock before cancellation's final clock
        # check, because reversing a consumed usage writes these same rows.
        StoreQuotaReservation.objects.filter(line__order=order)
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
        _lock_cancellation_resources(order, actor=user)
        now = timezone.now()
        if _expire_locked_order(order, now=now):
            expired_id = order.pk
        else:
            report_state = (
                StoreOrderStatus.PAYMENT_REVIEW
                if order.workflow_version == 2
                else StoreOrderStatus.PENDING_REVIEW
            )
            if order.status == report_state and order.transfer_reported_at:
                return order
            if order.status != StoreOrderStatus.PAYMENT_PENDING:
                raise StoreOrderError("store_order_state_conflict")
            order.status = report_state
            order.transfer_reported_at = order.transfer_reported_at or now
            order.transfer_reported_by = user if user and user.is_authenticated else None
            order.expires_at = (
                _workflow_deadline(order, order.transfer_reported_at)
                if order.workflow_version == 2
                else now + REVIEW_WINDOW
            )
            order.save(
                update_fields=[
                    "status",
                    "transfer_reported_at",
                    "transfer_reported_by",
                    "expires_at",
                    "updated_at",
                ]
            )
            return order
    raise StoreOrderError("store_order_expired", context={"order_id": str(expired_id)})


def cancel_pending_store_order(
    *, order_id, user, guest_token, idempotency_key, request_refund=False
):
    order, expired = _cancel_pending_store_order_atomic(
        order_id=order_id,
        user=user,
        guest_token=guest_token,
        idempotency_key=idempotency_key,
        request_refund=request_refund,
    )
    if expired:
        raise StoreOrderError("store_order_expired")
    return order


@transaction.atomic
def _cancel_pending_store_order_atomic(
    *, order_id, user, guest_token, idempotency_key, request_refund=False
):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    verify_order_access(order, user=user, guest_token=guest_token)
    fingerprint = request_fingerprint({"request_refund": True} if request_refund else {})
    if _action_replay(order, "buyer_cancel", idempotency_key, fingerprint):
        return order, False
    _lock_cancellation_resources(order, actor=user)
    if _expire_locked_order(order, now=timezone.now()):
        return order, True
    allowed = {StoreOrderStatus.PAYMENT_PENDING}
    if order.workflow_version == 2:
        allowed.add(StoreOrderStatus.PENDING_REVIEW)
        allowed.add(StoreOrderStatus.PAYMENT_REVIEW)
    elif request_refund:
        allowed.add(StoreOrderStatus.PENDING_REVIEW)
    if order.status not in allowed or (
        request_refund
        and (
            order.total_vnd == 0
            or (order.workflow_version == 2 and order.status == StoreOrderStatus.PENDING_REVIEW)
        )
    ):
        raise StoreOrderError("store_order_state_conflict")
    actor = user if user and user.is_authenticated else None
    previous_status = order.status
    _cancel_resources(order, actor, "buyer_cancelled")
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancelled_by = actor
    order.cancellation_reason = "buyer_cancelled"
    order.expires_at = None
    if order.payment_status == StorePaymentStatus.RECEIVED:
        order.payment_status = StorePaymentStatus.REFUND_REQUIRED
    if order.workflow_version == 2:
        _open_cancellation_reconciliation(order, actor, previous_status=previous_status)
    elif request_refund:
        _open_refund_request(order, actor)
    order.save(
        update_fields=[
            "status",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
            "expires_at",
            "payment_status",
            "updated_at",
        ]
    )
    _record_action(
        order,
        "buyer_cancel",
        idempotency_key,
        fingerprint,
        actor,
        context={
            "request_refund": request_refund
            or (
                order.workflow_version == 2
                and previous_status
                in {StoreOrderStatus.PAYMENT_PENDING, StoreOrderStatus.PAYMENT_REVIEW}
            )
        },
    )
    release_coupon(store_order=order, reason="buyer_cancelled")
    return order, False


def _open_cancellation_reconciliation(order, actor, *, previous_status=None):
    """Only cancellation after payment became possible opens a v2 money review."""
    status = previous_status if previous_status is not None else order.status
    if (
        order.workflow_version == 2
        and order.total_vnd > 0
        and status in {StoreOrderStatus.PAYMENT_PENDING, StoreOrderStatus.PAYMENT_REVIEW}
    ):
        _open_refund_request(order, actor)


def _open_refund_request(order, actor):
    if order.refund_requested_at is not None and order.refund_request_closed_at is None:
        return False
    order.refund_requested_at = timezone.now()
    order.refund_requested_by = actor
    order.refund_request_closed_at = None
    order.refund_request_closed_by = None
    order.refund_request_closed_reason = ""
    order.save(
        update_fields=[
            "refund_requested_at",
            "refund_requested_by",
            "refund_request_closed_at",
            "refund_request_closed_by",
            "refund_request_closed_reason",
            "updated_at",
        ]
    )
    return True


@transaction.atomic
def request_store_refund(*, order_id, user, guest_token, idempotency_key):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    verify_order_access(order, user=user, guest_token=guest_token)
    fingerprint = request_fingerprint({})
    if _action_replay(order, "request_refund", idempotency_key, fingerprint):
        return order
    if (
        order.status != StoreOrderStatus.CANCELLED
        or not order.total_vnd
        or order.refund_status == "refunded"
        or order.workflow_version == 2
    ):
        raise StoreOrderError("store_refund_state_conflict")
    actor = user if user and user.is_authenticated else None
    opened = _open_refund_request(order, actor)
    # Persist accepted no-op keys too: retrying one after admin resolution must
    # not create a second request or change the original request timestamp.
    _record_action(
        order, "request_refund", idempotency_key, fingerprint, actor, context={"opened": opened}
    )
    return order


@transaction.atomic
def close_store_refund_request(*, order_id, actor, idempotency_key, reason):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    reason = reason.strip()
    fingerprint = request_fingerprint({"reason": reason})
    if _action_replay(order, "close_refund_request", idempotency_key, fingerprint):
        return order
    if (
        not reason
        or order.status != StoreOrderStatus.CANCELLED
        or order.refund_status != "pending"
        or order.payment_received_at is not None
        or order.payment_status in {StorePaymentStatus.RECEIVED, StorePaymentStatus.REFUND_REQUIRED}
    ):
        raise StoreOrderError("store_refund_state_conflict")
    order.refund_request_closed_at = timezone.now()
    order.refund_request_closed_by = actor
    order.refund_request_closed_reason = reason
    order.save(
        update_fields=[
            "refund_request_closed_at",
            "refund_request_closed_by",
            "refund_request_closed_reason",
            "updated_at",
        ]
    )
    _record_action(
        order,
        "close_refund_request",
        idempotency_key,
        fingerprint,
        actor,
        context={"reason": reason},
    )
    return order


def _batch_available(batch, net_sold):
    return max(0, batch.quantity_units - net_sold.get(batch.pk, 0))


def _batch_allocation_plan(line, batch_quantities):
    needed = line.quantity
    allocations = []
    for batch_id, available in batch_quantities:
        quantity = min(needed, available)
        if quantity:
            allocations.append({"line_id": line.pk, "batch_id": batch_id, "quantity": quantity})
            needed -= quantity
        if not needed:
            return allocations
    return []


def allocation_suggestion(order):
    return source_inventory(order)["suggestion"]


def source_inventory(order, *, lines=None):
    from memberships.csa_rounds import effective_stock_hold_rows

    now = timezone.now()
    if lines is None:
        lines = list(order.lines.order_by("position", "id"))
    product_ids = {line.product_id for line in lines}
    capacity = inventory_capacity(product_ids, now=now, exclude_order=order)
    batches = (
        active_stock_batches(now=now)
        .filter(farm_product__product_id__in=product_ids)
        .select_related("farm_product__farm")
        .annotate(remaining_units=batch_remaining_units(batch_net_sold()))
        .order_by("farm_product__product_id", "end_date", "start_date", "id")
    )
    farm_name = "farm__name_vi" if order.locale == "vi" else "farm__name_en"
    own_stock = effective_store_stock_reservations(now=now).filter(line__order=order)
    own_csa = effective_stock_hold_rows(now=now).filter(selection__order=order)
    own_rows = list(own_stock.values_list("product_id", "quantity", "farm_id", farm_name))
    own_rows.extend(own_csa.values_list("product_id", "quantity", "farm_id", farm_name))
    current_order_reserved = defaultdict(int)
    assigned_farms = {}
    farm_names = defaultdict(dict)
    for product_id, quantity, farm_id, name in own_rows:
        current_order_reserved[product_id] += quantity
        if farm_id is not None:
            assigned_farms[product_id] = farm_id
            farm_names[product_id][farm_id] = name
    active_offers = FarmProduct.objects.filter(
        product_id__in=product_ids, removed_at__isnull=True, farm__is_active=True
    )
    for product_id, farm_id, name in active_offers.values_list("product_id", "farm_id", farm_name):
        farm_names[product_id][farm_id] = name
    other_demand = capacity.total_demand
    by_product = defaultdict(list)
    for batch in batches:
        offer = batch.farm_product
        farm = offer.farm
        by_product[offer.product_id].append(batch)
        farm_names[offer.product_id].setdefault(
            farm.pk, farm.name_vi if order.locale == "vi" else farm.name_en
        )
    csa = getattr(order, "csa_selection", None)
    can_approve = (
        order.status == StoreOrderStatus.PENDING_REVIEW
        and (order.expires_at is None or now < order.expires_at)
        and (csa is None or csa.state in ("locked", "confirmed"))
    )
    suggestion = []
    candidates = []
    source_lines = []
    for line in lines:
        product_id = line.product_id
        assigned_farm_id = assigned_farms.get(product_id)
        farm_quantities = {
            farm_id: capacity.remaining(product_id, farm_id)
            for farm_id in sorted(farm_names[product_id], key=str)
        }
        suggested_farm_id = (
            capacity.choose_farm(product_id, line.quantity, preferred_farm_id=assigned_farm_id)
            if can_approve
            else None
        )
        source_lines.append(
            {
                "line_id": str(line.pk),
                "product_id": str(product_id),
                "required_quantity": line.quantity,
                "assigned_farm_id": str(assigned_farm_id) if assigned_farm_id else None,
                "suggested_farm_id": str(suggested_farm_id) if suggested_farm_id else None,
                "farms": [
                    {
                        "farm_id": str(farm_id),
                        "farm_name": farm_names[product_id][farm_id],
                        "available_quantity": quantity,
                        "eligible": can_approve and quantity >= line.quantity,
                    }
                    for farm_id, quantity in farm_quantities.items()
                ],
            }
        )
        suggested_batches = []
        for batch in by_product[line.product_id]:
            farm_id = batch.farm_product.farm_id
            available = farm_quantities.get(farm_id, 0)
            if farm_id == suggested_farm_id:
                suggested_batches.append((batch.pk, batch.remaining_units))
            candidates.append(
                {
                    "line_id": str(line.pk),
                    "product_id": str(product_id),
                    "farm_id": str(farm_id),
                    "farm_name": farm_names[product_id][farm_id],
                    "batch_id": str(batch.pk),
                    "start_date": batch.start_date.isoformat(),
                    "end_date": batch.end_date.isoformat(),
                    "available_quantity": min(available, batch.remaining_units)
                    if available >= line.quantity
                    else 0,
                }
            )
        suggestion.extend(_batch_allocation_plan(line, suggested_batches))
    return {
        "refresh_required_before_approve": True,
        "lines": source_lines,
        "product_reservations": [
            {
                "product_id": str(product_id),
                "reserved_quantity": current_order_reserved.get(product_id, 0)
                + other_demand.get(product_id, 0),
                "this_order_quantity": current_order_reserved.get(product_id, 0),
                "other_orders_quantity": other_demand.get(product_id, 0),
            }
            for product_id in sorted(product_ids, key=str)
        ],
        "candidates": candidates,
        "suggestion": _jsonable(suggestion),
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


def _approval_batch_rows(batches):
    return batches.order_by().values(
        "pk", "farm_product_id", "farm_product__farm_id", "farm_product__product_id"
    )


def _legacy_approval_sources(lines, allocations):
    pairs = [(item["line_id"], item["batch_id"]) for item in allocations]
    if len(pairs) != len(set(pairs)):
        raise StoreOrderError("store_allocation_duplicate")
    batch_rows = {
        row["pk"]: row
        for row in _approval_batch_rows(
            FarmProductStockBatch.objects.filter(
                pk__in={batch_id for _, batch_id in pairs},
                farm_product__product_id__in={line.product_id for line in lines.values()},
            )
        )
    }
    farms_by_line = {}
    for line_id, batch_id in pairs:
        line = lines.get(line_id)
        row = batch_rows.get(batch_id)
        if line is None or row is None or row["farm_product__product_id"] != line.product_id:
            raise StoreOrderError("store_allocation_stale")
        farm_id = row["farm_product__farm_id"]
        if farms_by_line.setdefault(line_id, farm_id) != farm_id:
            raise StoreOrderError(
                "store_allocation_mixed_farm",
                context={"line_id": str(line_id), "product_id": str(line.product_id)},
            )
    if not farms_by_line or farms_by_line.keys() != lines.keys():
        raise StoreOrderError("store_allocation_incomplete")
    return farms_by_line, batch_rows.values()


def _selected_approval_sources(lines, sources):
    farms_by_line = {item["line_id"]: item["farm_id"] for item in sources}
    if len(farms_by_line) != len(sources):
        raise StoreOrderError("store_allocation_duplicate")
    if farms_by_line.keys() - lines.keys():
        raise StoreOrderError("store_allocation_stale")
    if not farms_by_line or farms_by_line.keys() != lines.keys():
        raise StoreOrderError("store_allocation_incomplete")
    selected_pairs = {
        (lines[line_id].product_id, farm_id) for line_id, farm_id in farms_by_line.items()
    }
    potential_offers = FarmProduct.objects.filter(
        product_id__in={product_id for product_id, _ in selected_pairs},
        farm_id__in=set(farms_by_line.values()),
        removed_at__isnull=True,
    ).values_list("pk", "product_id", "farm_id")
    offer_ids = [
        pk
        for pk, product_id, farm_id in potential_offers
        if (product_id, farm_id) in selected_pairs
    ]
    # Include upcoming rows that may become current while waiting. Eligibility is
    # decided from the locked rows and the final clock reading.
    batches = FarmProductStockBatch.objects.filter(
        farm_product_id__in=offer_ids,
        cancelled_at__isnull=True,
        supply_generation=F("farm_product__supply_generation"),
        end_date__gte=timezone.localdate(),
    )
    return farms_by_line, list(_approval_batch_rows(batches))


def _approval_farm_error(line, farm_id, available_quantity, *, legacy=False):
    context = {"product_id": str(line.product_id), "available_quantity": available_quantity}
    if not legacy:
        context.update(line_id=str(line.pk), farm_id=str(farm_id))
    return StoreOrderError("store_allocation_stale", context=context)


def _plan_selected_approval(lines, farms_by_line, batches, available_by_batch, capacity):
    by_farm = defaultdict(list)
    for batch in sorted(batches.values(), key=lambda row: (row.end_date, row.start_date, row.pk)):
        if batch.pk in available_by_batch:
            offer = batch.farm_product
            by_farm[offer.product_id, offer.farm_id].append(
                (batch.pk, available_by_batch[batch.pk])
            )
    allocations = []
    for line_id, line in lines.items():
        farm_id = farms_by_line[line_id]
        plan = _batch_allocation_plan(line, by_farm[line.product_id, farm_id])
        if not plan:
            raise _approval_farm_error(line, farm_id, capacity.remaining(line.product_id, farm_id))
        allocations.extend(plan)
    return allocations


def _validate_approval_plan(
    lines, farms_by_line, allocations, available_by_batch, capacity, *, legacy
):
    allocated_by_line = defaultdict(int)
    allocated_by_batch = defaultdict(int)
    for item in allocations:
        quantity = item["quantity"]
        if type(quantity) is not int or quantity <= 0:
            raise StoreOrderError("store_allocation_incomplete")
        if item["batch_id"] not in available_by_batch:
            raise StoreOrderError("store_allocation_stale")
        allocated_by_line[item["line_id"]] += quantity
        allocated_by_batch[item["batch_id"]] += quantity
    if any(allocated_by_line[line.pk] != line.quantity for line in lines.values()):
        raise StoreOrderError("store_allocation_incomplete")
    for batch_id, quantity in allocated_by_batch.items():
        available = available_by_batch[batch_id]
        if quantity > available:
            raise StoreOrderError(
                "store_allocation_stale",
                context={"batch_id": str(batch_id), "available_quantity": available},
            )
    for line_id, line in lines.items():
        farm_id = farms_by_line[line_id]
        available = capacity.remaining(line.product_id, farm_id)
        if line.quantity > available:
            raise _approval_farm_error(line, farm_id, available, legacy=legacy)


@transaction.atomic
def _approve_store_order_atomic(*, order_id, actor, idempotency_key, allocations, sources):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    if sources is None:
        # Old receipts hash the ordered allocation array, including mixed-Farm sales.
        fingerprint = request_fingerprint({"allocations": allocations})
    else:
        fingerprint = request_fingerprint(
            {"sources": sorted(sources, key=lambda item: str(item["line_id"]))}
        )
    if _action_replay(order, "approve", idempotency_key, fingerprint):
        return order, False
    _lock_order_products(order)
    _lock_checkout_identity(actor)
    _lock_order_memberships(order)
    if _expire_locked_order(order, now=timezone.now()):
        return order, True
    if order.status != StoreOrderStatus.PENDING_REVIEW:
        raise StoreOrderError("store_order_state_conflict")
    csa = getattr(order, "csa_selection", None)
    if csa and csa.state not in ("locked", "confirmed"):
        raise StoreOrderError("store_order_state_conflict")
    lines = {line.pk: line for line in order.lines.select_for_update().all()}
    product_ids = {line.product_id for line in lines.values()}
    # Lock Product -> Membership -> FarmProduct -> Farm -> stock batch -> quota rows.
    if sources is None:
        farms_by_line, batch_sources = _legacy_approval_sources(lines, allocations)
    else:
        farms_by_line, batch_sources = _selected_approval_sources(lines, sources)
    batches = _lock_stock_sources(batch_sources)
    _lock_order_quotas(order)
    _lock_cancellation_coupon(order)
    # Waiting for a source, quota or coupon lock may have crossed the deadline. This is
    # the approval decision time; keep it for the recorded approval timestamp.
    decision_at = timezone.now()
    if _expire_locked_order(order, now=decision_at):
        return order, True
    if len(batches) != len(batch_sources):
        raise StoreOrderError("store_allocation_stale")
    today = timezone.localdate(decision_at)
    net_sold = _net_sold_by_batch(batches)
    available_by_batch = {
        batch.pk: _batch_available(batch, net_sold)
        for batch in batches.values()
        if is_current_stock_batch(batch, today=today)
    }
    capacity = inventory_capacity(product_ids, now=decision_at, exclude_order=order)
    if sources is not None:
        allocations = _plan_selected_approval(
            lines, farms_by_line, batches, available_by_batch, capacity
        )
    _validate_approval_plan(
        lines, farms_by_line, allocations, available_by_batch, capacity, legacy=sources is None
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
    if csa:
        csa.stock_holds.all().delete()
    _consume_reserved_quota(order, actor)
    _release_reservations(order, "approved")
    order.status = (
        StoreOrderStatus.PAYMENT_PENDING
        if order.workflow_version == 2 and order.total_vnd
        else StoreOrderStatus.PENDING_DELIVERY
    )
    order.approved_at = order.approved_at or decision_at
    order.approved_by = actor
    order.expires_at = (
        _workflow_deadline(order, order.approved_at)
        if order.status == StoreOrderStatus.PAYMENT_PENDING
        else None
    )
    if order.total_vnd and order.workflow_version == 1:
        order.payment_status = StorePaymentStatus.RECEIVED
        if order.payment_received_at is None:
            order.payment_received_at = decision_at
            order.payment_received_by = actor
    order.save(
        update_fields=[
            "status",
            "approved_at",
            "approved_by",
            "expires_at",
            "payment_status",
            "payment_received_at",
            "payment_received_by",
            "updated_at",
        ]
    )
    context = {"allocations": _jsonable(allocations)}
    if sources is not None:
        context["sources"] = _jsonable(sources)
    _record_action(order, "approve", idempotency_key, fingerprint, actor, context=context)
    return order, False


def approve_store_order(*, order_id, actor, idempotency_key, allocations=None, sources=None):
    if (sources is None) == (allocations is None):
        raise StoreOrderError("store_order_invalid")
    order, expired = _approve_store_order_atomic(
        order_id=order_id,
        actor=actor,
        idempotency_key=idempotency_key,
        allocations=allocations,
        sources=sources,
    )
    if expired:
        raise StoreOrderError("store_order_expired")
    return order


def expire_store_orders(*, limit=200, now=None):
    now = now or timezone.now()
    ids = list(
        StoreOrder.objects.filter(
            status__in=WAITING_STATES,
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
        for quota in MembershipCycleQuota.objects.select_for_update()
        .filter(pk__in=[item.cycle_quota_id for item in allocations])
        .order_by("pk")
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


def _lock_stock_sources(batch_sources):
    """Lock only validated sources under their Product mutexes, in PK order."""
    offers = {
        offer.pk: offer
        for offer in FarmProduct.objects.select_for_update()
        .filter(pk__in={source["farm_product_id"] for source in batch_sources})
        .order_by("pk")
    }
    farms = {
        farm.pk: farm
        for farm in Farm.objects.select_for_update()
        .filter(pk__in={source["farm_product__farm_id"] for source in batch_sources})
        .order_by("pk")
    }
    batches = {
        batch.pk: batch
        for batch in FarmProductStockBatch.objects.select_for_update()
        .filter(pk__in={source["pk"] for source in batch_sources})
        .order_by("pk")
    }
    # Attach the locked versions for eligibility reads without a joined FOR UPDATE.
    for offer in offers.values():
        offer.farm = farms[offer.farm_id]
    for batch in batches.values():
        batch.farm_product = offers[batch.farm_product_id]
    return batches


def _order_stock_allocations(order):
    return (
        StoreStockAllocation.objects.filter(line__order=order)
        .select_related("batch__farm_product")
        .order_by("pk")
    )


def _lock_order_stock_sources(allocations):
    return _lock_stock_sources(
        [
            {
                "pk": item.batch_id,
                "farm_product_id": item.batch.farm_product_id,
                "farm_product__farm_id": item.batch.farm_product.farm_id,
            }
            for item in allocations
        ]
    )


def _lock_cancellation_coupon(order):
    if order.coupon_id is not None and order.status in WAITING_STATES:
        lock_coupons([order.coupon_id])
        list(CouponUsage.objects.select_for_update().filter(store_order=order).order_by("pk"))


def _lock_cancellation_resources(order, *, actor=None, lock_coupon=True):
    _lock_order_products(order)
    # Match checkout's Product -> shared User -> Membership order. Actor FK
    # writes after the coupon lock can then reuse this shared lock without
    # waiting behind a CSA purchase's exclusive identity lock.
    if actor is not None and actor.is_authenticated:
        _lock_checkout_identity(actor)
    _lock_order_memberships(order)
    _lock_order_stock_sources(list(_order_stock_allocations(order)))
    _lock_order_quotas(order)
    if lock_coupon:
        _lock_cancellation_coupon(order)


def _restocked_by_allocation(allocation_ids):
    return {
        item["allocation_id"]: item["total"]
        for item in StoreStockMovement.objects.filter(
            allocation_id__in=allocation_ids, kind=StoreStockMovementKind.RESTOCK
        )
        .values("allocation_id")
        .annotate(total=Sum("quantity"))
    }


def _restore_committed_stock(order, actor):
    """Append reversal movements, including expired/disabled sources, exactly once."""
    allocations = list(_order_stock_allocations(order))
    _lock_order_stock_sources(allocations)
    restored = _restocked_by_allocation([item.pk for item in allocations])
    for allocation in allocations:
        remaining = allocation.quantity - restored.get(allocation.pk, 0)
        if remaining:
            StoreStockMovement.objects.create(
                allocation=allocation,
                batch_id=allocation.batch_id,
                line_id=allocation.line_id,
                kind=StoreStockMovementKind.RESTOCK,
                quantity=remaining,
                idempotency_key=uuid.uuid5(order.pk, f"cancel:{allocation.pk}"),
                actor=actor,
            )


def _cancel_resources(order, actor, reason):
    from memberships.models import CSASelection

    # Future retail cancellations release committed batches in either workflow.
    # CSA-native fulfillment keeps its existing resource policy; historical
    # cancelled orders never enter this transition again.
    if order.workflow_version == 2 or not CSASelection.objects.filter(order=order).exists():
        _restore_committed_stock(order, actor)
    if order.approved_at is not None:
        _reverse_store_quota(order, actor, reason)
    _release_reservations(order, reason)


def cancel_store_order(*, order_id, actor, idempotency_key, reason, reconciliation_note=None):
    order, expired = _cancel_store_order_atomic(
        order_id=order_id,
        actor=actor,
        idempotency_key=idempotency_key,
        reason=reason,
        reconciliation_note=reconciliation_note,
    )
    if expired:
        raise StoreOrderError("store_order_expired")
    return order


@transaction.atomic
def _cancel_store_order_atomic(
    *, order_id, actor, idempotency_key, reason, reconciliation_note=None
):
    if not reason.strip():
        raise StoreOrderError("store_cancellation_reason_required")
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    note = (reconciliation_note or "").strip()
    # Keep the accepted historical fingerprint: omitted and explicit blank notes
    # have always shared the same request identity.
    fingerprint = request_fingerprint({"reason": reason.strip(), "reconciliation_note": note})
    if _action_replay(order, "cancel", idempotency_key, fingerprint):
        return order, False
    _lock_cancellation_resources(order, actor=actor)
    if _expire_locked_order(order, now=timezone.now()):
        return order, True
    if order.status in {StoreOrderStatus.CANCELLED, StoreOrderStatus.DELIVERED}:
        raise StoreOrderError("store_order_state_conflict")
    from memberships.csa_rounds import _cancel_locked
    from memberships.models import CSASelection

    csa = CSASelection.objects.filter(order=order).first()
    if csa:
        _cancel_locked(csa, actor, reason.strip())
    _cancel_resources(order, actor, reason.strip())
    _open_cancellation_reconciliation(order, actor)
    order.status = StoreOrderStatus.CANCELLED
    order.cancelled_at = timezone.now()
    order.cancelled_by = actor
    order.cancellation_reason = reason.strip()
    if reconciliation_note is not None or order.workflow_version == 1:
        order.reconciliation_note = note
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
        context={"reason": reason.strip(), "reconciliation_note": note},
    )
    release_coupon(store_order=order, reason="cancelled")
    return order, False


@transaction.atomic
def record_store_payment(*, order_id, actor, idempotency_key, note=""):
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    fingerprint = request_fingerprint({"note": note.strip()})
    if _action_replay(order, "record_payment", idempotency_key, fingerprint):
        return order
    if order.total_vnd == 0 or order.payment_status == StorePaymentStatus.REFUNDED:
        raise StoreOrderError("store_payment_state_conflict")
    _lock_cancellation_resources(order, actor=actor, lock_coupon=False)
    lock_store_referral_referrer(order)
    _lock_cancellation_coupon(order)
    _expire_locked_order(order, now=timezone.now())
    if order.workflow_version == 2:
        if order.status == StoreOrderStatus.CANCELLED and (
            order.refund_request_closed_at is not None or order.refund_status != "pending"
        ):
            raise StoreOrderError("store_payment_state_conflict")
        if order.status not in {
            StoreOrderStatus.PAYMENT_PENDING,
            StoreOrderStatus.PAYMENT_REVIEW,
            StoreOrderStatus.CANCELLED,
            StoreOrderStatus.PENDING_DELIVERY,
        }:
            raise StoreOrderError("store_payment_state_conflict")
        if order.status == StoreOrderStatus.PENDING_DELIVERY and order.payment_received_at is None:
            raise StoreOrderError("store_payment_state_conflict")
    if order.payment_status in {StorePaymentStatus.RECEIVED, StorePaymentStatus.REFUND_REQUIRED}:
        _record_action(
            order,
            "record_payment",
            idempotency_key,
            fingerprint,
            actor,
            context={"note": note.strip(), "amount_vnd": order.total_vnd, "already_received": True},
        )
        return order
    order.payment_received_at = order.payment_received_at or timezone.now()
    order.payment_received_by = actor
    order.payment_status = (
        StorePaymentStatus.REFUND_REQUIRED
        if order.status == StoreOrderStatus.CANCELLED
        else StorePaymentStatus.RECEIVED
    )
    if order.workflow_version == 2 and order.status in {
        StoreOrderStatus.PAYMENT_PENDING,
        StoreOrderStatus.PAYMENT_REVIEW,
    }:
        order.status = StoreOrderStatus.PENDING_DELIVERY
        order.expires_at = None
    if note.strip():
        order.reconciliation_note = note.strip()
    order.save(
        update_fields=[
            "payment_received_at",
            "payment_received_by",
            "payment_status",
            "status",
            "expires_at",
            "reconciliation_note",
            "updated_at",
        ]
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
    release_coupon(store_order=order, reason="refunded")
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
    from memberships.models import CSASelection

    CSASelection.objects.filter(order=order).update(state="delivered")
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
    if batch.farm_product.removed_at is not None:
        return "offer_inactive"
    if batch.supply_generation != batch.farm_product.supply_generation:
        return "batch_historical"
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
        for item in _order_stock_allocations(order).filter(
            pk__in=allocation_ids, line__product_id=F("batch__farm_product__product_id")
        )
    }
    if len(rows) != len(allocation_ids):
        raise StoreOrderError("store_restock_invalid")
    list(
        Product.objects.select_for_update()
        .filter(pk__in={item.batch.farm_product.product_id for item in rows.values()})
        .order_by("pk")
    )
    batches = _lock_order_stock_sources(rows.values())
    restored = _restocked_by_allocation(allocation_ids)
    today = timezone.localdate()
    for item in allocations:
        allocation = rows[item["allocation_id"]]
        remaining = allocation.quantity - restored.get(allocation.pk, 0)
        unavailable_reason = restock_unavailable_reason(
            order, batches[allocation.batch_id], remaining, as_of=today
        )
        if item["quantity"] > remaining or unavailable_reason:
            raise StoreOrderError(
                "store_restock_invalid",
                context={"allocation_id": str(allocation.pk), "reason": unavailable_reason},
            )
    for item in allocations:
        allocation = rows[item["allocation_id"]]
        StoreStockMovement.objects.create(
            allocation=allocation,
            batch_id=allocation.batch_id,
            line_id=allocation.line_id,
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
