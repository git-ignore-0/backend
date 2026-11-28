"""Admin quote, creation and receipt handling for Store orders."""

from functools import partial
from uuid import uuid4

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from commerce.inventory import inventory_wire_quantity
from commerce.models import (
    MAX_STORE_ORDER_AMOUNT_VND,
    CatalogTreeLock,
    ProductStatus,
    StoreOrder,
    StoreOrderLine,
    StoreOrderStatus,
    StorePaymentStatus,
)
from commerce.store_delivery import VIETNAM
from commerce.store_orders import (
    StoreOrderError,
    _address_for_checkout,
    _create_store_checkout,
    _lock_checkout_identity,
    _reference_code,
    _store_address_version,
    _store_line_snapshots,
    _store_products,
    calculate_store_quote,
    request_fingerprint,
)
from content.creation_receipts import (
    CreationIdempotencyConflict,
    claim_creation_receipt,
    find_creation_receipt,
)
from content.models import AdminCreationReceipt
from identity.models import User

CREATION_ACTION = AdminCreationReceipt.Action.STORE_ORDER


def _buyer(payload):
    if "buyer_user_id" not in payload:
        return None
    user = User.objects.filter(pk=payload["buyer_user_id"]).first()
    if user is None:
        raise StoreOrderError("store_buyer_not_found")
    if not user.can_access_core():
        raise StoreOrderError("store_buyer_ineligible")
    return user


def _retail_payload(payload):
    return {
        **payload,
        "lines": [{**line, "use_quota": False} for line in payload["lines"]],
    }


def _historical_products(payload, *, lock=False):
    return _store_products(
        payload["lines"],
        eligible=Q(status__in=[ProductStatus.ACTIVE, ProductStatus.DISCONTINUED]),
        lock=lock,
    )


def _historical_quote(*, user, payload, products, lock=False):
    address, _, saved_address = _address_for_checkout(
        user, payload.get("guest"), lock=lock, admin=True, historical=True
    )
    delivery_date = payload["delivery_date"]
    if delivery_date >= timezone.now().astimezone(VIETNAM).date():
        raise StoreOrderError("store_delivery_date_invalid")
    line_values = [
        (item, products[item["product_id"]], item["unit_price_vnd"] * item["quantity"])
        for item in payload["lines"]
    ]
    subtotal = sum(amount for _, _, amount in line_values)
    shipping_fee = payload["shipping_fee_vnd"]
    return {
        "address": address,
        "address_version": _store_address_version(saved_address, address),
        "line_values": line_values,
        "subtotal": subtotal,
        "shipping_fee": shipping_fee,
        "total": subtotal + shipping_fee,
        "delivery_date": delivery_date,
    }


def _preview(quote, lines):
    if quote["total"] > MAX_STORE_ORDER_AMOUNT_VND:
        raise StoreOrderError("store_order_amount_out_of_range")
    address = quote["address"]
    return {
        "delivery_date": quote["delivery_date"].isoformat(),
        "shipping_address": {
            "recipient_name": address["recipient_name_snapshot"],
            "phone_number": address["phone_e164_snapshot"],
            "province_code": address["province_code_snapshot"],
            "ward_code": address["ward_code_snapshot"],
            "address": address["address_snapshot"],
        },
        "shipping_address_version": quote["address_version"],
        "shipping_discount_reason": (
            "csa_companion_delivery" if quote.get("free_shipping") else None
        ),
        "subtotal_vnd": str(quote["subtotal"]),
        "shipping_fee_vnd": str(quote["shipping_fee"]),
        "total_vnd": str(quote["total"]),
        "lines": lines,
    }


def _preview_line(item, unit_price, subtotal, available=None):
    return {
        "product_id": str(item["product_id"]),
        "quantity": item["quantity"],
        "unit_price_vnd": str(unit_price),
        "subtotal_vnd": str(subtotal),
        "available_quantity": available,
    }


def _retail_preview(quote):
    return _preview(
        quote,
        [
            _preview_line(
                item,
                product.retail_price_vnd,
                subtotal,
                inventory_wire_quantity(quote["inventory"].maximum(product.pk)),
            )
            for _, item, product, _, _, subtotal, _ in quote["line_values"]
        ],
    )


def _historical_preview(quote):
    return _preview(
        quote,
        [
            _preview_line(item, item["unit_price_vnd"], subtotal)
            for item, _, subtotal in quote["line_values"]
        ],
    )


def _confirmation(actor, intent, preview):
    material = request_fingerprint({"actor_id": actor.pk, "intent": intent, "quote": preview})
    return salted_hmac(
        "commerce.admin-store-confirmation", material, algorithm="sha256"
    ).hexdigest()


def quote_admin_store_order(*, actor, payload):
    user = _buyer(payload)
    if payload["kind"] == "historical":
        quote = _historical_quote(
            user=user, payload=payload, products=_historical_products(payload)
        )
        preview = _historical_preview(quote)
    else:
        quote = calculate_store_quote(
            user=user, payload=_retail_payload(payload), admin_actor=actor
        )
        preview = _retail_preview(quote)
    return {**preview, "confirmation": _confirmation(actor, payload, preview)}


def _confirm_quote(preview, *, actor, intent, confirmation):
    if not constant_time_compare(confirmation, _confirmation(actor, intent, preview)):
        raise StoreOrderError("store_requote_required", context={"reason": "confirmation_changed"})


def _record_historical_order(*, user, payload, receipt, quote):
    order = StoreOrder.objects.create(
        reference_code=_reference_code(),
        buyer_user=user,
        admin_creation_receipt=receipt,
        is_historical=True,
        create_idempotency_key=uuid4(),
        request_fingerprint=receipt.request_fingerprint,
        locale=payload["locale"],
        **quote["address"],
        workflow_version=2,
        status=StoreOrderStatus.DELIVERED,
        payment_status=StorePaymentStatus.RECEIVED
        if quote["total"]
        else StorePaymentStatus.NOT_REQUIRED,
        subtotal_vnd=quote["subtotal"],
        shipping_fee_vnd=quote["shipping_fee"],
        total_vnd=quote["total"],
        delivery_date=quote["delivery_date"],
    )
    StoreOrderLine.objects.bulk_create(
        [
            StoreOrderLine(
                order=order,
                product=product,
                position=position,
                **_store_line_snapshots(product, payload["locale"]),
                unit_price_vnd_snapshot=item["unit_price_vnd"],
                quantity=item["quantity"],
                cash_quantity=item["quantity"],
                cash_subtotal_vnd=subtotal,
            )
            for position, (item, product, subtotal) in enumerate(quote["line_values"])
        ]
    )
    return order


def _existing_receipt(*, actor, key, fingerprint):
    receipt = find_creation_receipt(actor=actor, action=CREATION_ACTION, key=key)
    if receipt is not None and receipt.request_fingerprint != fingerprint:
        raise StoreOrderError("store_idempotency_conflict")
    return receipt


@transaction.atomic
def create_admin_store_order(*, actor, payload):
    key = payload["idempotency_key"]
    fingerprint = request_fingerprint(payload)
    find_receipt = partial(_existing_receipt, actor=actor, key=key, fingerprint=fingerprint)
    if receipt := find_receipt():
        return receipt, False

    intent = {
        field: value
        for field, value in payload.items()
        if field not in {"confirmation", "idempotency_key"}
    }
    confirm = partial(
        _confirm_quote, actor=actor, intent=intent, confirmation=payload["confirmation"]
    )

    def claim_receipt():
        return claim_creation_receipt(
            actor=actor, action=CREATION_ACTION, key=key, fingerprint=fingerprint
        )

    def accept_new_quote(quote):
        confirm(_retail_preview(quote))
        return claim_receipt()

    try:
        if intent["kind"] == "historical":
            products = _historical_products(intent, lock=True)
            if receipt := find_receipt():
                return receipt, False
            user = _buyer(intent)
            _lock_checkout_identity(user, actor=actor)
            quote = _historical_quote(user=user, payload=intent, products=products, lock=True)
            confirm(_historical_preview(quote))
            # Both kinds finish taking address and other domain locks before
            # claiming the same actor/key slot.
            receipt, created = claim_receipt()
            if not created:
                return receipt, False
            order = _record_historical_order(
                user=user, payload=intent, receipt=receipt, quote=quote
            )
        else:
            CatalogTreeLock.objects.select_for_update().get(pk=1)
            if receipt := find_receipt():
                return receipt, False
            order, _, created = _create_store_checkout(
                user=_buyer(intent),
                payload=_retail_payload(intent),
                key=uuid4(),
                fingerprint=fingerprint,
                admin_actor=actor,
                confirm_admin_quote=accept_new_quote,
            )
            receipt = order.admin_creation_receipt
            if not created:
                return receipt, False
    except CreationIdempotencyConflict as error:
        raise StoreOrderError("store_idempotency_conflict") from error
    except User.DoesNotExist as error:
        raise StoreOrderError("store_buyer_not_found") from error
    except StoreOrderError:
        # A conflicting request can commit while this one waits for domain locks.
        if receipt := find_receipt():
            return receipt, False
        raise

    receipt.resource_id = order.pk
    receipt.save(update_fields=["resource_id"])
    return receipt, True
