"""CSA coupon eligibility and exact VND quote arithmetic."""

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from memberships.models import CSACoupon


class CouponError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def normalize_coupon_code(value):
    if not isinstance(value, str):
        raise CouponError("coupon_invalid")
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{0,63}", code):
        raise CouponError("coupon_invalid")
    return code


def coupon_for_code(value, *, lock=False):
    code = normalize_coupon_code(value)
    queryset = CSACoupon.objects.select_for_update() if lock else CSACoupon.objects
    try:
        return queryset.get(code__iexact=code)
    except CSACoupon.DoesNotExist as error:
        raise CouponError("coupon_invalid") from error


def _whole_vnd(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CouponError("coupon_invalid_value") from error
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise CouponError("coupon_invalid_value")
    return amount


def coupon_quote_snapshot(
    *, coupon, package_id, price_option_id, plan, now, check_availability=True
):
    if check_availability and not coupon.is_active:
        raise CouponError("coupon_inactive")
    if check_availability and now < coupon.starts_at:
        raise CouponError("coupon_not_started")
    if check_availability and now >= coupon.expires_at:
        raise CouponError("coupon_expired")
    if check_availability and coupon.used_count >= coupon.usage_limit:
        raise CouponError("coupon_usage_exhausted")
    if (
        (coupon.package_ids and str(package_id) not in coupon.package_ids)
        or (
            not coupon.package_ids
            and coupon.package_id
            and str(coupon.package_id) != str(package_id)
        )
        or (coupon.price_option_ids and str(price_option_id) not in coupon.price_option_ids)
        or (
            not coupon.price_option_ids
            and coupon.price_option_id
            and str(coupon.price_option_id) != str(price_option_id)
        )
        or (coupon.payment_types and plan["payment_type"] not in coupon.payment_types)
        or (
            not coupon.payment_types
            and coupon.payment_type
            and coupon.payment_type != plan["payment_type"]
        )
        or (
            coupon.installment_counts and plan["installment_count"] not in coupon.installment_counts
        )
        or (
            not coupon.installment_counts
            and coupon.installment_count
            and coupon.installment_count != plan["installment_count"]
        )
    ):
        raise CouponError("coupon_not_applicable")
    total = _whole_vnd(plan["total_amount"])
    if coupon.minimum_amount is not None and total < coupon.minimum_amount:
        raise CouponError("coupon_minimum_amount_not_met")
    first = _whole_vnd(plan["initial_payment_amount"])
    try:
        value = Decimal(str(coupon.discount_value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise CouponError("coupon_invalid_value") from error
    if not value.is_finite() or value <= 0:
        raise CouponError("coupon_invalid_value")
    if coupon.discount_type == CSACoupon.DiscountType.PERCENT:
        if value > 100:
            raise CouponError("coupon_invalid_value")
        discount = (first * value / Decimal(100)).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        if coupon.maximum_discount_amount is not None:
            maximum_discount = _whole_vnd(coupon.maximum_discount_amount)
            if maximum_discount <= 0:
                raise CouponError("coupon_invalid_value")
            discount = min(discount, maximum_discount)
    elif coupon.discount_type == CSACoupon.DiscountType.FIXED:
        if value != value.to_integral_value() or coupon.maximum_discount_amount is not None:
            raise CouponError("coupon_invalid_value")
        discount = value
    else:
        raise CouponError("coupon_invalid_value")
    if discount > first:
        raise CouponError("coupon_invalid_value")
    discount = discount.quantize(Decimal(1))
    return {
        "coupon_code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": format(value, "f"),
        "maximum_discount_amount": (
            format(coupon.maximum_discount_amount, "f")
            if coupon.maximum_discount_amount is not None
            else None
        ),
        "discount_amount": format(discount, "f"),
        "contract_total_before_discount": str(_whole_vnd(plan["contract_total"])),
        "payment_plan_total_before_discount": format(total, "f"),
        "customer_payable_total": format(total - discount, "f"),
        "initial_payment_before_discount": format(first, "f"),
        "initial_payment_after_discount": format(first - discount, "f"),
    }


def discounted_plan(plan, snapshot):
    rows = []
    for row in plan["installments"]:
        before = _whole_vnd(row["amount"])
        discount = Decimal(snapshot["discount_amount"]) if row["sequence"] == 1 else Decimal(0)
        rows.append(
            {
                "sequence": row["sequence"],
                "amount": format(before - discount, "f"),
                "cycle_count": row["cycle_count"],
                "amount_before_discount": format(before, "f"),
                "discount_amount": format(discount, "f"),
                "amount_after_discount": format(before - discount, "f"),
                "months_covered": row["cycle_count"],
            }
        )
    return rows
