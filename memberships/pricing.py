"""Exact CSA price and VND parsing helpers."""

from decimal import Decimal, InvalidOperation

MAX_POSITIVE_BIG_INTEGER = 9_223_372_036_854_775_807


def parse_whole_vnd(value, *, allow_zero=True) -> Decimal:
    """Parse a finite whole-VND amount within the database-supported range."""

    if isinstance(value, (float, bool)):
        raise ValueError("VND amount must be a whole number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("VND amount must be a whole number") from error
    if (
        not amount.is_finite()
        or amount != amount.to_integral_value()
        or (amount < 0 if allow_zero else amount <= 0)
        or amount > MAX_POSITIVE_BIG_INTEGER
    ):
        raise ValueError("VND amount must be a whole number")
    return amount


def calculate_total_price_vnd(*, duration_months: int, monthly_price_vnd: int) -> int:
    duration = int(duration_months)
    monthly_price = int(monthly_price_vnd)
    if duration <= 0 or monthly_price <= 0:
        raise ValueError("Duration and monthly price must be positive.")
    if monthly_price > MAX_POSITIVE_BIG_INTEGER // duration:
        raise ValueError("Total price exceeds the supported VND limit.")
    return duration * monthly_price
