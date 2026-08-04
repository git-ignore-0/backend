MAX_POSITIVE_BIG_INTEGER = 9_223_372_036_854_775_807


def calculate_total_price_vnd(*, duration_months: int, monthly_price_vnd: int) -> int:
    duration = int(duration_months)
    monthly_price = int(monthly_price_vnd)
    if duration <= 0 or monthly_price <= 0:
        raise ValueError("Duration and monthly price must be positive.")
    if monthly_price > MAX_POSITIVE_BIG_INTEGER // duration:
        raise ValueError("Total price exceeds the supported VND limit.")
    return duration * monthly_price
