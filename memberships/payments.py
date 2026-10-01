"""Immutable CSA payment schedule and manual bank-confirmation services."""

from datetime import datetime, time
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from identity.models import AuditLog
from memberships.coupons import CouponError, calculate_coupon_discount, normalize_coupon_code
from memberships.models import (
    CSAContract,
    CSAContractVersionReason,
    CSAPurchasePayment,
    CSAPurchasePaymentStatus,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    MembershipStatus,
)
from memberships.pricing import parse_whole_vnd


class CSAPaymentError(ValueError):
    code = "csa_payment_invalid"


class CSAPaymentUnavailable(CSAPaymentError):
    code = "csa_payment_unavailable"


class CSAPaymentStateConflict(CSAPaymentError):
    code = "csa_payment_state_conflict"


class CSAPaymentAmountMismatch(CSAPaymentError):
    code = "csa_payment_amount_mismatch"


class CSAPaymentScheduleInvalid(CSAPaymentError):
    code = "csa_payment_schedule_invalid"


def _whole_vnd(value, *, allow_zero=False) -> Decimal:
    try:
        return parse_whole_vnd(value, allow_zero=allow_zero)
    except ValueError as error:
        raise CSAPaymentAmountMismatch("Payment amount must be whole VND") from error


def _coupon_pricing_snapshot(item: CSAPurchaseRequest) -> dict | None:
    snapshot = item.coupon_snapshot
    if snapshot is None:
        if item.customer_payable_total_snapshot is not None:
            raise CSAPaymentScheduleInvalid("Coupon pricing snapshot is inconsistent")
        return None
    required = {
        "coupon_code",
        "discount_type",
        "discount_value",
        "maximum_discount_amount",
        "discount_amount",
        "contract_total_before_discount",
        "payment_plan_total_before_discount",
        "customer_payable_total",
        "initial_payment_before_discount",
        "initial_payment_after_discount",
    }
    coupon_code = snapshot.get("coupon_code") if isinstance(snapshot, dict) else None
    if (
        not isinstance(snapshot, dict)
        or not required.issubset(snapshot)
        or not isinstance(coupon_code, str)
        or item.customer_payable_total_snapshot is None
    ):
        raise CSAPaymentScheduleInvalid("Coupon pricing snapshot is incomplete")
    try:
        canonical_coupon_code = normalize_coupon_code(coupon_code)
    except CouponError as error:
        raise CSAPaymentScheduleInvalid("Coupon pricing snapshot is invalid") from error
    if coupon_code != canonical_coupon_code:
        raise CSAPaymentScheduleInvalid("Coupon pricing snapshot is invalid")
    try:
        original_total = _whole_vnd(item.amount_snapshot)
        contract_total = _whole_vnd(snapshot["contract_total_before_discount"])
        plan_total = _whole_vnd(snapshot["payment_plan_total_before_discount"])
        expected_contract_total = _whole_vnd(
            Decimal(item.price_option_monthly_amount_snapshot) * item.duration_months_snapshot
        )
        payable_total = _whole_vnd(snapshot["customer_payable_total"], allow_zero=True)
        persisted_payable = _whole_vnd(item.customer_payable_total_snapshot, allow_zero=True)
        discount = _whole_vnd(snapshot["discount_amount"], allow_zero=True)
        initial_before = _whole_vnd(snapshot["initial_payment_before_discount"])
        initial_after = _whole_vnd(snapshot["initial_payment_after_discount"], allow_zero=True)
    except (CSAPaymentAmountMismatch, TypeError, ValueError) as error:
        raise CSAPaymentScheduleInvalid("Coupon pricing snapshot is invalid") from error
    try:
        _, discount_value, maximum_discount, expected_discount = calculate_coupon_discount(
            discount_type=snapshot["discount_type"],
            discount_value=snapshot["discount_value"],
            maximum_discount_amount=snapshot["maximum_discount_amount"],
            initial_payment_amount=initial_before,
        )
    except CouponError as error:
        raise CSAPaymentScheduleInvalid("Coupon pricing snapshot is invalid") from error
    discount_type = snapshot["discount_type"]
    if (
        contract_total != expected_contract_total
        or original_total != plan_total
        or payable_total != persisted_payable
        or payable_total != plan_total - discount
        or initial_after != initial_before - discount
        or discount != expected_discount
        or discount > initial_before
        or (
            item.payment_plan_id is None
            and (
                initial_before != original_total
                or initial_before != contract_total
                or initial_after != payable_total
            )
        )
    ):
        raise CSAPaymentScheduleInvalid("Coupon pricing snapshot totals do not match")
    return {
        "original_total": format(original_total, "f"),
        "discount_amount": format(discount, "f"),
        "customer_payable_total": format(payable_total, "f"),
        "initial_payment_before_discount": format(initial_before, "f"),
        "initial_payment_after_discount": format(initial_after, "f"),
        "coupon": {
            "code": canonical_coupon_code,
            "discount_type": discount_type,
            "discount_value": format(discount_value, "f"),
            "maximum_discount_amount": (
                format(maximum_discount, "f") if maximum_discount is not None else None
            ),
        },
    }


def _snapshot_rows(item: CSAPurchaseRequest) -> list[tuple[int, Decimal, int]]:
    pricing = _coupon_pricing_snapshot(item)
    if item.payment_plan_id is None:
        amount = pricing["customer_payable_total"] if pricing else item.amount_snapshot
        return [
            (
                1,
                _whole_vnd(amount, allow_zero=pricing is not None),
                item.duration_months_snapshot,
            )
        ]
    snapshot = item.payment_installments_snapshot
    if not isinstance(snapshot, list) or len(snapshot) != item.installment_count_snapshot:
        raise CSAPaymentScheduleInvalid("Purchase payment snapshot is incomplete")
    rows = []
    for expected_sequence, entry in enumerate(snapshot, start=1):
        expected_keys = {"sequence", "amount", "cycle_count"}
        coupon_keys = {
            "amount_before_discount",
            "discount_amount",
            "amount_after_discount",
            "months_covered",
        }
        required_keys = expected_keys | coupon_keys if pricing is not None else expected_keys
        if not isinstance(entry, dict) or set(entry) != required_keys:
            raise CSAPaymentScheduleInvalid("Purchase payment snapshot is invalid")
        sequence, cycles = entry["sequence"], entry["cycle_count"]
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence != expected_sequence
            or not isinstance(cycles, int)
            or isinstance(cycles, bool)
            or cycles < 1
            or not isinstance(entry["amount"], str)
        ):
            raise CSAPaymentScheduleInvalid("Purchase payment snapshot is invalid")
        try:
            amount = _whole_vnd(entry["amount"], allow_zero=pricing is not None)
        except CSAPaymentAmountMismatch as error:
            raise CSAPaymentScheduleInvalid("Purchase payment snapshot is invalid") from error
        if pricing is not None:
            if (
                not all(
                    isinstance(entry[field], str)
                    for field in (
                        "amount_before_discount",
                        "discount_amount",
                        "amount_after_discount",
                    )
                )
                or not isinstance(entry["months_covered"], int)
                or isinstance(entry["months_covered"], bool)
            ):
                raise CSAPaymentScheduleInvalid("Purchase payment coupon snapshot is invalid")
            try:
                before = _whole_vnd(entry["amount_before_discount"])
                row_discount = _whole_vnd(entry["discount_amount"], allow_zero=True)
                after = _whole_vnd(entry["amount_after_discount"], allow_zero=True)
            except CSAPaymentAmountMismatch as error:
                raise CSAPaymentScheduleInvalid(
                    "Purchase payment coupon snapshot is invalid"
                ) from error
            expected_discount = (
                Decimal(pricing["discount_amount"]) if expected_sequence == 1 else Decimal(0)
            )
            if (
                entry["months_covered"] != cycles
                or amount != after
                or after != before - row_discount
                or row_discount != expected_discount
            ):
                raise CSAPaymentScheduleInvalid("Purchase payment coupon snapshot is invalid")
        rows.append((sequence, amount, cycles))
    if pricing is not None:
        before_total = sum(
            (_whole_vnd(entry["amount_before_discount"]) for entry in snapshot),
            Decimal(0),
        )
        if (
            before_total != Decimal(pricing["original_total"])
            or _whole_vnd(snapshot[0]["amount_before_discount"])
            != Decimal(pricing["initial_payment_before_discount"])
            or rows[0][1] != Decimal(pricing["initial_payment_after_discount"])
        ):
            raise CSAPaymentScheduleInvalid("Purchase payment coupon totals do not match")
    if (
        sum((row[1] for row in rows), Decimal(0))
        != Decimal(
            item.customer_payable_total_snapshot
            if item.customer_payable_total_snapshot is not None
            else item.amount_snapshot
        )
        or sum(row[2] for row in rows) != item.duration_months_snapshot
    ):
        raise CSAPaymentScheduleInvalid("Purchase payment snapshot totals do not match")
    return rows


def purchase_pricing_snapshot(item: CSAPurchaseRequest) -> dict:
    """Build validated pricing facts using only immutable request snapshots."""

    rows = _snapshot_rows(item)
    coupon_pricing = _coupon_pricing_snapshot(item)
    if coupon_pricing is not None:
        return coupon_pricing
    original_total = _whole_vnd(item.amount_snapshot)
    return {
        "original_total": format(original_total, "f"),
        "discount_amount": "0",
        "customer_payable_total": format(original_total, "f"),
        "initial_payment_before_discount": format(rows[0][1], "f"),
        "initial_payment_after_discount": format(rows[0][1], "f"),
        "coupon": None,
    }


@transaction.atomic
def ensure_csa_payment_schedule(*, request_id) -> list[CSAPurchasePayment]:
    """Materialize the immutable request snapshot exactly once under its row lock."""

    item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
    expected = _snapshot_rows(item)
    existing = list(
        CSAPurchasePayment.objects.select_for_update()
        .filter(purchase_request=item)
        .order_by("installment_sequence", "id")
    )
    if existing:
        actual = [
            (payment.installment_sequence, payment.amount_due, payment.cycle_count)
            for payment in existing
        ]
        if actual != expected:
            raise CSAPaymentScheduleInvalid("Stored payment schedule differs from request snapshot")
        return existing
    CSAPurchasePayment.objects.bulk_create(
        [
            CSAPurchasePayment(
                purchase_request=item,
                installment_sequence=sequence,
                amount_due=amount,
                coupon_zero_amount=bool(item.coupon_snapshot) and amount == 0,
                cycle_count=cycles,
            )
            for sequence, amount, cycles in expected
        ]
    )
    return list(item.payments.order_by("installment_sequence", "id"))


def payment_due_at(start_date, preceding_cycles):
    from memberships.services import add_months

    first_day = add_months(start_date, preceding_cycles)
    return timezone.make_aware(
        datetime.combine(first_day, time.min), timezone.get_default_timezone()
    )


@transaction.atomic
def set_csa_payment_due_dates(*, request_id, start_date) -> None:
    CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
    payments = list(
        CSAPurchasePayment.objects.select_for_update()
        .filter(purchase_request_id=request_id)
        .order_by("installment_sequence")
    )
    preceding = 0
    for payment in payments:
        due = payment_due_at(start_date, preceding)
        if payment.due_at is not None and payment.due_at != due:
            raise CSAPaymentScheduleInvalid("Payment due date cannot change")
        if payment.due_at is None:
            payment.due_at = due
            payment.save(update_fields=["due_at", "updated_at"])
        preceding += payment.cycle_count


def confirm_locked_csa_payment(*, payment, amount_paid, actor, note="") -> CSAPurchasePayment:
    if actor is None:
        raise CSAPaymentStateConflict("A confirming admin is required")
    amount = _whole_vnd(amount_paid, allow_zero=payment.amount_due == 0)
    if amount != payment.amount_due:
        raise CSAPaymentAmountMismatch("Payment amount must equal the installment amount due")
    if payment.status == CSAPurchasePaymentStatus.CONFIRMED:
        return payment
    if payment.status == CSAPurchasePaymentStatus.REJECTED:
        raise CSAPaymentStateConflict("Rejected payment cannot be confirmed")
    payment.status = CSAPurchasePaymentStatus.CONFIRMED
    payment.amount_paid = amount
    payment.confirmed_at = timezone.now()
    payment.confirmed_by = actor
    payment.note = str(note or "").strip()
    payment.save()
    AuditLog.objects.create(
        user=actor,
        action="csa_payment.confirmed",
        context={
            "payment_id": str(payment.pk),
            "request_id": str(payment.purchase_request_id) if payment.purchase_request_id else None,
            "membership_id": str(payment.membership_id) if payment.membership_id else None,
            "installment_sequence": payment.installment_sequence,
            "amount_paid": format(amount, "f"),
        },
    )
    return payment


def confirm_csa_payment(*, request_id, installment_sequence, amount_paid, actor, note=""):
    from memberships.contract_pdfs import delete_contract_documents, get_contract_pdf_storage
    from memberships.contract_versions import issue_contract_version
    from memberships.models import Membership
    from memberships.services import ensure_due_cycles

    storage = get_contract_pdf_storage()
    documents = None
    try:
        with transaction.atomic():
            try:
                item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
            except CSAPurchaseRequest.DoesNotExist as error:
                raise CSAPaymentUnavailable("Payment request is unavailable") from error
            if item.status != CSAPurchaseRequestStatus.APPROVED or not item.membership_id:
                raise CSAPaymentStateConflict("Only approved requests can confirm later payments")
            membership = Membership.objects.select_for_update().get(pk=item.membership_id)
            if membership.status == MembershipStatus.REVOKED:
                raise CSAPaymentStateConflict("Revoked membership cannot receive payments")
            try:
                payment = CSAPurchasePayment.objects.select_for_update().get(
                    purchase_request=item, installment_sequence=installment_sequence
                )
            except CSAPurchasePayment.DoesNotExist as error:
                raise CSAPaymentUnavailable("Payment installment is unavailable") from error
            if (
                payment.status != CSAPurchasePaymentStatus.CONFIRMED
                and CSAPurchasePayment.objects.filter(
                    purchase_request=item, installment_sequence__lt=installment_sequence
                )
                .exclude(status=CSAPurchasePaymentStatus.CONFIRMED)
                .exists()
            ):
                raise CSAPaymentStateConflict("Earlier installments must be confirmed first")
            if payment.status == CSAPurchasePaymentStatus.CONFIRMED:
                if (
                    _whole_vnd(amount_paid, allow_zero=payment.amount_due == 0)
                    != payment.amount_due
                ):
                    raise CSAPaymentAmountMismatch(
                        "Payment amount must equal the installment amount due"
                    )
                return payment
            confirmed = confirm_locked_csa_payment(
                payment=payment, amount_paid=amount_paid, actor=actor, note=note
            )
            ensure_due_cycles(membership)
            contract = CSAContract.objects.select_for_update().get(membership=membership)
            _version, documents = issue_contract_version(
                contract_id=contract.pk,
                reason=CSAContractVersionReason.PAYMENT_CONFIRMED,
                actor=actor,
                storage=storage,
            )
        return confirmed
    except Exception:
        if documents is not None:
            delete_contract_documents(documents, storage=storage)
        raise


@transaction.atomic
def reject_csa_payment(*, request_id, installment_sequence, reason, actor):
    reason = str(reason or "").strip()
    if not reason:
        raise CSAPaymentStateConflict("Payment rejection reason is required")
    try:
        item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
    except CSAPurchaseRequest.DoesNotExist as error:
        raise CSAPaymentUnavailable("Payment request is unavailable") from error
    if item.status != CSAPurchaseRequestStatus.APPROVED:
        raise CSAPaymentStateConflict("Only approved requests can reject an installment")
    try:
        payment = CSAPurchasePayment.objects.select_for_update().get(
            purchase_request=item, installment_sequence=installment_sequence
        )
    except CSAPurchasePayment.DoesNotExist as error:
        raise CSAPaymentUnavailable("Payment installment is unavailable") from error
    if payment.status == CSAPurchasePaymentStatus.REJECTED:
        return payment
    if payment.status == CSAPurchasePaymentStatus.CONFIRMED:
        raise CSAPaymentStateConflict("Confirmed payment cannot be rejected")
    payment.status = CSAPurchasePaymentStatus.REJECTED
    payment.rejected_at = timezone.now()
    payment.rejected_by = actor
    payment.rejection_reason = reason
    payment.save()
    AuditLog.objects.create(
        user=actor,
        action="csa_payment.rejected",
        context={
            "payment_id": str(payment.pk),
            "request_id": str(item.pk),
            "installment_sequence": payment.installment_sequence,
            "reason": reason,
        },
    )
    return payment


def paid_cycle_count(*, request_id) -> int:
    return (
        CSAPurchasePayment.objects.filter(
            purchase_request_id=request_id, status=CSAPurchasePaymentStatus.CONFIRMED
        ).aggregate(total=Sum("cycle_count"))["total"]
        or 0
    )


def csa_payment_schedule_payload(item: CSAPurchaseRequest) -> dict:
    payments = list(item.payments.all())
    if not payments:
        return {"payments": [], "payment_summary": None}
    paid = sum(
        (
            payment.amount_paid
            for payment in payments
            if payment.status == CSAPurchasePaymentStatus.CONFIRMED
        ),
        Decimal(0),
    )
    confirmed = [
        payment for payment in payments if payment.status == CSAPurchasePaymentStatus.CONFIRMED
    ]
    total = sum((payment.amount_due for payment in payments), Decimal(0))
    return {
        "payments": [
            {
                "id": str(payment.pk),
                "installment_sequence": payment.installment_sequence,
                "amount_due": format(payment.amount_due, "f"),
                "amount_paid": (
                    format(payment.amount_paid, "f") if payment.amount_paid is not None else None
                ),
                "cycle_count": payment.cycle_count,
                "status": payment.status,
                "due_at": payment.due_at.isoformat() if payment.due_at else None,
                "confirmed_at": (
                    payment.confirmed_at.isoformat() if payment.confirmed_at else None
                ),
                "confirmed_by": (
                    {"id": str(payment.confirmed_by_id), "name": payment.confirmed_by.name}
                    if payment.confirmed_by_id
                    else None
                ),
                "rejected_at": payment.rejected_at.isoformat() if payment.rejected_at else None,
                "rejection_reason": payment.rejection_reason or None,
                "note": payment.note,
            }
            for payment in payments
        ],
        "payment_summary": {
            "total_amount": format(total, "f"),
            "paid_amount": format(paid, "f"),
            "remaining_amount": format(total - paid, "f"),
            "confirmed_installments": len(confirmed),
            "installment_count": len(payments),
            "paid_cycles": sum(payment.cycle_count for payment in confirmed),
            "total_cycles": item.duration_months_snapshot,
        },
    }


def public_csa_payment_schedule_payload(item: CSAPurchaseRequest) -> dict:
    """Expose only the purchased schedule, not internal review actors or notes."""
    schedule = csa_payment_schedule_payload(item)
    return {
        "payment_summary": schedule["payment_summary"],
        "payments": [
            {
                key: payment[key]
                for key in (
                    "installment_sequence",
                    "amount_due",
                    "amount_paid",
                    "cycle_count",
                    "status",
                    "due_at",
                    "confirmed_at",
                    "rejected_at",
                )
            }
            for payment in schedule["payments"]
        ],
    }


@transaction.atomic
def mark_overdue_csa_payments(*, now=None, batch_size=100) -> int:
    now = now or timezone.now()
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    candidates = list(
        CSAPurchasePayment.objects.filter(
            status=CSAPurchasePaymentStatus.PENDING,
            due_at__lte=now,
            purchase_request__status=CSAPurchaseRequestStatus.APPROVED,
        )
        .order_by("due_at", "id")
        .values_list("purchase_request_id", "id")[:batch_size]
    )
    changed = 0
    for request_id, payment_id in candidates:
        item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
        if item.membership_id and item.membership.status == MembershipStatus.REVOKED:
            continue
        payment = CSAPurchasePayment.objects.select_for_update().get(pk=payment_id)
        if payment.status == CSAPurchasePaymentStatus.PENDING and payment.due_at <= now:
            payment.status = CSAPurchasePaymentStatus.OVERDUE
            payment.save(update_fields=["status", "updated_at"])
            AuditLog.objects.create(
                action="csa_payment.overdue",
                context={
                    "payment_id": str(payment.pk),
                    "request_id": str(request_id),
                    "installment_sequence": payment.installment_sequence,
                    "due_at": payment.due_at.isoformat(),
                },
            )
            changed += 1
    return changed
