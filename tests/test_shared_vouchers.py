from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from memberships.coupons import CouponError, coupon_quote_snapshot
from memberships.models import CSACoupon


@pytest.fixture
def voucher(db):
    now = timezone.now()
    return CSACoupon.objects.create(
        code="SHARED",
        discount_type="fixed",
        discount_value=30_000,
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=1),
        usage_limit=2,
    )


def test_existing_coupon_defaults_to_csa(voucher):
    assert voucher.application_scope == "csa"


def test_store_only_voucher_cannot_discount_csa(voucher):
    voucher.application_scope = "store"
    with pytest.raises(CouponError, match="coupon_scope_mismatch"):
        coupon_quote_snapshot(
            coupon=voucher,
            package_id=None,
            price_option_id=None,
            plan={
                "payment_type": "full",
                "installment_count": 1,
                "total_amount": "100000",
                "initial_payment_amount": "100000",
                "contract_total": "100000",
            },
            now=timezone.now(),
        )


def test_store_discount_uses_cash_subtotal_and_shared_uuid_limit(voucher, active_user):
    from memberships.coupons import check_coupon_customer, store_coupon_snapshot

    voucher.application_scope = "both"
    snapshot = store_coupon_snapshot(coupon=voucher, subtotal_vnd=320_000, now=timezone.now())
    assert snapshot["discount_amount_vnd"] == 30_000
    assert snapshot["subtotal_vnd"] == 320_000
    assert snapshot["discount_value"] == "30000"
    check_coupon_customer(coupon=voucher, user=active_user)
    with pytest.raises(CouponError, match="coupon_authentication_required"):
        check_coupon_customer(coupon=voucher, user=None)


@pytest.mark.parametrize(
    "value,cap,expected",
    [("12.50", None, 40_000), ("12.50", 25_000, 25_000), ("33.33", None, 106_656)],
)
def test_store_percent_rounding(voucher, value, cap, expected):
    from memberships.coupons import store_coupon_snapshot

    voucher.application_scope = "store"
    voucher.discount_type = "percent"
    voucher.discount_value = Decimal(value)
    voucher.maximum_discount_amount = cap
    result = store_coupon_snapshot(coupon=voucher, subtotal_vnd=320_000, now=timezone.now())
    assert result["discount_amount_vnd"] == expected


def test_store_only_rejects_csa_specific_conditions_in_model(voucher):
    from django.core.exceptions import ValidationError

    voucher.application_scope = "store"
    voucher.payment_types = ["full"]
    with pytest.raises(ValidationError, match="coupon_csa_conditions_not_allowed"):
        voucher.full_clean()


def test_csa_guest_cannot_request_coupon(db):
    from memberships.purchases import create_csa_payment_quote

    with pytest.raises(CouponError, match="coupon_authentication_required"):
        create_csa_payment_quote(package_id=None, price_option_id=None, coupon_code="ANY")


@pytest.mark.parametrize("scope", ["csa", "store", "both"])
def test_scope_time_boundaries_and_half_vnd_rounding(voucher, scope):
    from memberships.coupons import check_coupon_availability, store_coupon_snapshot

    now = timezone.now()
    voucher.application_scope = scope
    voucher.starts_at = now
    voucher.expires_at = now + timedelta(seconds=1)
    branch = "store" if scope != "csa" else "csa"
    check_coupon_availability(coupon=voucher, scope=branch, now=now)
    with pytest.raises(CouponError, match="coupon_not_started"):
        check_coupon_availability(coupon=voucher, scope=branch, now=now - timedelta(microseconds=1))
    with pytest.raises(CouponError, match="coupon_expired"):
        check_coupon_availability(coupon=voucher, scope=branch, now=voucher.expires_at)
    if scope != "csa":
        voucher.discount_type = "percent"
        voucher.discount_value = Decimal("50")
        assert (
            store_coupon_snapshot(coupon=voucher, subtotal_vnd=101, now=now)["discount_amount_vnd"]
            == 51
        )


def test_model_and_admin_validate_csa_array_conditions_identically(voucher):
    from django.core.exceptions import ValidationError

    voucher.installment_counts = [0]
    with pytest.raises(ValidationError, match="coupon_not_applicable"):
        voucher.full_clean()


def test_usage_release_requires_exactly_one_source(db):
    from memberships.voucher_usage import release_coupon

    with pytest.raises(ValueError, match="Exactly one voucher source"):
        release_coupon(reason="cancelled")
