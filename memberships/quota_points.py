"""Allowed new point inputs; balances and historical ledger values may differ."""

from decimal import Decimal

POINT_INPUT_ERROR = "Use 0.25, 0.5, 0.75 or a whole number of at least 1 point."
FRACTIONAL_POINT_INPUTS = frozenset({Decimal("0.25"), Decimal("0.5"), Decimal("0.75")})


def is_allowed_point_input(value: Decimal) -> bool:
    return value.is_finite() and (
        value in FRACTIONAL_POINT_INPUTS or (value >= 1 and value == value.to_integral_value())
    )
