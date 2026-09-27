"""Domain validation for a complete CSA price-option payment plan.

Call this after assembling all installment rows, before making a plan available
to a purchase flow. Cross-row sums cannot be enforced by a SQL check constraint.
"""

from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.db.models.deletion import ProtectedError

from memberships.models import (
    CSAPurchaseRequest,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    MembershipPaymentType,
)
from memberships.pricing import MAX_POSITIVE_BIG_INTEGER


class PaymentPlanValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _positive_whole_vnd(value, *, code: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise PaymentPlanValidationError(code)
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise PaymentPlanValidationError(code) from error
    if (
        not amount.is_finite()
        or amount != amount.to_integral_value()
        or amount <= 0
        or amount > MAX_POSITIVE_BIG_INTEGER
    ):
        raise PaymentPlanValidationError(code)
    return amount


def validate_payment_plan_configuration(plan, installments=None) -> None:
    """Validate a complete plan, including exact VND and product-cycle totals.

    ``installments`` may contain unsaved rows, allowing validation before a
    transaction writes either parent or children. Otherwise persisted rows are
    loaded through the plan's related manager.
    """

    if installments is None:
        if plan._state.adding:
            raise PaymentPlanValidationError("payment_plan_installments_required")
        installments = plan.installments.all()
    rows = list(installments)

    if not str(plan.name or "").strip():
        raise PaymentPlanValidationError("payment_plan_name_invalid")
    if plan.payment_type not in MembershipPaymentType.values:
        raise PaymentPlanValidationError("payment_plan_type_invalid")
    duration_months = plan.price_option.duration_months
    if duration_months < 1:
        raise PaymentPlanValidationError("payment_plan_duration_invalid")
    if duration_months == 1 and plan.payment_type == MembershipPaymentType.INSTALLMENT:
        raise PaymentPlanValidationError("payment_plan_installment_not_allowed")
    count = plan.installment_count
    if not isinstance(count, int) or isinstance(count, bool):
        raise PaymentPlanValidationError("payment_plan_installment_count_invalid")
    if (plan.payment_type == MembershipPaymentType.FULL and count != 1) or (
        plan.payment_type == MembershipPaymentType.INSTALLMENT and count < 2
    ):
        raise PaymentPlanValidationError("payment_plan_installment_count_invalid")
    total_amount = _positive_whole_vnd(plan.total_amount, code="payment_plan_total_invalid")
    if len(rows) != count:
        raise PaymentPlanValidationError("payment_plan_installment_count_mismatch")

    sequences = [row.sequence for row in rows]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in sequences
    ):
        raise PaymentPlanValidationError("payment_plan_sequence_invalid")
    if len(set(sequences)) != len(sequences):
        raise PaymentPlanValidationError("payment_plan_sequence_duplicate")
    if sorted(sequences) != list(range(1, count + 1)):
        raise PaymentPlanValidationError("payment_plan_sequence_gap")

    amount_sum = Decimal(0)
    cycle_sum = 0
    for row in rows:
        amount_sum += _positive_whole_vnd(row.amount, code="payment_plan_amount_invalid")
        cycles = row.cycle_count
        if not isinstance(cycles, int) or isinstance(cycles, bool) or cycles < 1:
            raise PaymentPlanValidationError("payment_plan_cycle_count_invalid")
        cycle_sum += cycles
    if amount_sum != total_amount:
        raise PaymentPlanValidationError("payment_plan_amount_total_mismatch")
    if cycle_sum != duration_months:
        raise PaymentPlanValidationError("payment_plan_cycle_total_mismatch")


def admin_payment_plan_payload(plan) -> dict:
    rows = list(plan.installments.all())
    return {
        "id": str(plan.pk),
        "price_option_id": str(plan.price_option_id),
        "name": plan.name,
        "payment_type": plan.payment_type,
        "total_amount": format(plan.total_amount, "f"),
        "installment_count": plan.installment_count,
        "sort_order": plan.sort_order,
        "is_active": plan.is_active,
        "in_use": getattr(plan, "has_requests", None)
        if hasattr(plan, "has_requests")
        else plan.purchase_requests.exists(),
        "initial_payment_amount": format(rows[0].amount, "f") if rows else None,
        "installments": [
            {
                "sequence": row.sequence,
                "amount": format(row.amount, "f"),
                "cycle_count": row.cycle_count,
            }
            for row in rows
        ],
    }


def admin_payment_plan_queryset():
    return MembershipPaymentPlan.objects.annotate(
        has_requests=Exists(CSAPurchaseRequest.objects.filter(payment_plan_id=OuterRef("pk")))
    ).prefetch_related("installments")


@transaction.atomic
def save_admin_payment_plan(*, option_id, data, plan_id=None):
    option = MembershipPackagePriceOption.objects.select_for_update().get(pk=option_id)
    if plan_id is None:
        plan = MembershipPaymentPlan(price_option=option)
    else:
        plan = MembershipPaymentPlan.objects.select_for_update().get(
            pk=plan_id, price_option=option
        )
        if plan.is_active:
            raise PaymentPlanValidationError("payment_plan_deactivate_before_edit")
        if plan.purchase_requests.exists():
            raise PaymentPlanValidationError("payment_plan_in_use")
    for field in ("name", "payment_type", "total_amount", "installment_count", "sort_order"):
        if field in data:
            setattr(plan, field, data[field])
    rows = (
        [
            MembershipPaymentPlanInstallment(payment_plan=plan, **row)
            for row in data.get("installments", [])
        ]
        if "installments" in data
        else list(plan.installments.all())
    )
    validate_payment_plan_configuration(plan, rows)
    try:
        plan.save()
        if "installments" in data:
            plan.installments.all().delete()
            MembershipPaymentPlanInstallment.objects.bulk_create(rows)
    except IntegrityError as error:
        raise PaymentPlanValidationError("payment_plan_conflict") from error
    return plan


@transaction.atomic
def set_admin_payment_plan_active(*, option_id, plan_id, active: bool):
    option = MembershipPackagePriceOption.objects.select_for_update().get(pk=option_id)
    plan = MembershipPaymentPlan.objects.select_for_update().get(pk=plan_id, price_option=option)
    if active:
        validate_payment_plan_configuration(plan)
        if (
            MembershipPaymentPlan.objects.filter(
                price_option=option,
                payment_type=plan.payment_type,
                installment_count=plan.installment_count,
                is_active=True,
            )
            .exclude(pk=plan.pk)
            .exists()
        ):
            raise PaymentPlanValidationError("payment_plan_active_duplicate")
    if plan.is_active != active:
        plan.is_active = active
        plan.save(update_fields=["is_active", "updated_at"])
    return plan


@transaction.atomic
def delete_admin_payment_plan(*, option_id, plan_id):
    option = MembershipPackagePriceOption.objects.select_for_update().get(pk=option_id)
    plan = MembershipPaymentPlan.objects.select_for_update().get(pk=plan_id, price_option=option)
    if plan.is_active:
        raise PaymentPlanValidationError("payment_plan_deactivate_before_delete")
    if plan.purchase_requests.exists():
        raise PaymentPlanValidationError("payment_plan_in_use")
    try:
        plan.delete()
    except ProtectedError as error:
        raise PaymentPlanValidationError("payment_plan_in_use") from error


def payment_plan_snapshot_payload(purchase_request) -> dict | None:
    """Read only immutable purchase-request columns, never the live catalog plan."""

    if purchase_request.payment_plan_id is None:
        return None
    payload = {
        "id": str(purchase_request.payment_plan_id),
        "name": purchase_request.payment_plan_name_snapshot,
        "payment_type": purchase_request.payment_type_snapshot,
        "total_amount": format(purchase_request.total_payment_amount_snapshot, "f"),
        "installment_count": purchase_request.installment_count_snapshot,
        "initial_payment_amount": initial_payment_amount_snapshot(purchase_request),
        "installments": purchase_request.payment_installments_snapshot,
    }
    if purchase_request.coupon_snapshot:
        payload["customer_payable_total"] = purchase_request.coupon_snapshot[
            "customer_payable_total"
        ]
        payload["discount_amount"] = purchase_request.coupon_snapshot["discount_amount"]
    return payload


def initial_payment_amount_snapshot(purchase_request) -> str | None:
    """Amount due now, derived only from the immutable request snapshot."""

    if purchase_request.payment_plan_id is None:
        if purchase_request.coupon_snapshot:
            return purchase_request.coupon_snapshot["initial_payment_after_discount"]
        return str(purchase_request.amount_snapshot)
    from memberships.payments import CSAPaymentError, _snapshot_rows

    try:
        return format(_snapshot_rows(purchase_request)[0][1], "f")
    except CSAPaymentError:
        return None
