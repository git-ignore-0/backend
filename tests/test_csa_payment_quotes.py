from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import ShippingAddress, ShippingArea
from identity.models import AccountStatus, User
from memberships.coupons import CouponError, normalize_coupon_code
from memberships.models import (
    CSAContract,
    CSACoupon,
    CSACustomer,
    CSAPurchasePayment,
    CSAPurchasePaymentStatus,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    Membership,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    MembershipPaymentType,
    QuotaPolicy,
)
from memberships.purchases import (
    PaymentQuoteExpired,
    PaymentQuoteIdentityMismatch,
    PaymentQuoteUnavailable,
    _load_payment_quote,
    _quote_token,
    confirm_csa_payment_quote,
    create_csa_payment_quote,
    csa_payment_quote_payload,
    update_membership_payment_settings,
)
from memberships.tracker import tracker_request_payload
from memberships.views import admin_csa_purchase_payload

QUOTE_URL = "/api/v1/public/csa-payment-quotes"
CONFIRM_URL = "/api/v1/public/csa-purchase-requests/confirm-transfer"
BANK = {"bin": "970436", "name": "Vietcombank"}


@pytest.fixture(autouse=True)
def quote_test_isolation(monkeypatch, db):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [BANK])
    ShippingArea.objects.update_or_create(
        ward_code="22015",
        defaults={
            "province_code": "66",
            "shipping_fee_vnd": 30_000,
            "is_active": True,
        },
    )
    yield
    cache.clear()


@pytest.fixture
def catalog():
    package = MembershipPackage.objects.create(
        name="CSA quote package", quota_policy=QuotaPolicy.ROLLOVER
    )
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=400_000
    )
    return package, option


@pytest.fixture
def payment_settings():
    return update_membership_payment_settings(
        bank_bin=BANK["bin"],
        bank_name=BANK["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
        direct_transfer_enabled=True,
    )


@pytest.fixture
def guest_identity():
    return {
        "name": "Nguyễn Văn An",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "Số 12, ngõ 5",
    }


def make_plan(option, *, payment_type):
    if payment_type == MembershipPaymentType.FULL:
        total = Decimal("1100000")
        rows = [(1, total, 3)]
    else:
        total = Decimal("1300000")
        rows = [(1, Decimal("500000"), 1), (2, Decimal("800000"), 2)]
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name=f"{payment_type} plan",
        payment_type=payment_type,
        total_amount=total,
        installment_count=len(rows),
        is_active=True,
    )
    for sequence, amount, cycles in rows:
        MembershipPaymentPlanInstallment.objects.create(
            payment_plan=plan,
            sequence=sequence,
            amount=amount,
            cycle_count=cycles,
        )
    return plan


def quote_payload(package, option, plan, identity):
    return {
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "payment_plan_id": str(plan.pk),
        "terms_accepted": True,
        "terms_locale": "vi",
        "guest_identity": identity,
    }


@pytest.mark.django_db
def test_quote_is_stateless_and_uses_full_payment_total_for_qr(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)

    response = APIClient().post(
        QUOTE_URL, quote_payload(package, option, plan, guest_identity), format="json"
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert not CSAPurchaseRequest.objects.exists()
    assert data["payment"]["amount"] == "1100000"
    assert data["payment"]["bank_code"] == BANK["bin"]
    assert data["payment"]["bank_name"] == BANK["name"]
    assert data["qr_payload"]["amount"] == "1100000"
    assert data["payment_summary"]["initial_payment_amount"] == "1100000"
    assert data["payment_summary"]["payment_type"] == "full"
    assert "shipping_fee_vnd" not in data
    assert "Nguyễn Văn An" not in data["quote_token"]
    assert "+84912345678" not in data["quote_token"]
    assert "guest_token" not in data
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.django_db
def test_guest_quote_rejects_a_real_ward_that_is_not_enabled_for_shipping(
    catalog, payment_settings, guest_identity
):
    ShippingArea.objects.all().delete()
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)

    response = APIClient().post(
        QUOTE_URL, quote_payload(package, option, plan, guest_identity), format="json"
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_shipping_area_unsupported"}]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "quote",
    [
        {"v": 1, "nonce": "a" * 32},
        {
            "v": 1,
            "nonce": "a" * 32,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "request_code": "CSA-TEST",
            "subject": "guest",
            "selection": {},
            "payment": {},
            "terms": {},
        },
    ],
)
def test_malformed_signed_quote_is_unavailable_not_server_error(quote):
    with pytest.raises(PaymentQuoteUnavailable):
        _load_payment_quote(quote_token=_quote_token(quote), now=timezone.now())


@pytest.mark.django_db
@pytest.mark.parametrize("section", ["subject", "selection", "payment", "terms"])
def test_malformed_signed_quote_sections_are_unavailable_not_server_error(
    section, catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)
    _token, quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        guest_identity=guest_identity,
    )
    quote[section] = {}

    with pytest.raises(PaymentQuoteUnavailable):
        _load_payment_quote(quote_token=_quote_token(quote), now=timezone.now())


@pytest.mark.django_db
def test_installment_quote_uses_first_installment_for_qr_and_preserves_snapshot(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.INSTALLMENT)

    response = APIClient().post(
        QUOTE_URL,
        {
            **quote_payload(package, option, plan, guest_identity),
            "amount": "1",
            "bank": "attacker",
        },
        format="json",
    )
    assert response.status_code == 400
    assert not CSAPurchaseRequest.objects.exists()

    response = APIClient().post(
        QUOTE_URL, quote_payload(package, option, plan, guest_identity), format="json"
    )
    data = response.json()["data"]
    assert data["payment"]["amount"] == "500000"
    assert data["qr_payload"]["amount"] == "500000"
    assert data["payment_summary"] == {
        "payment_type": "installment",
        "total_amount": "1300000",
        "initial_payment_amount": "500000",
        "installment_count": 2,
        "installments": [
            {"sequence": 1, "amount": "500000", "cycle_count": 1},
            {"sequence": 2, "amount": "800000", "cycle_count": 2},
        ],
    }


@pytest.mark.django_db
def test_confirm_quote_creates_one_payment_confirmed_request_and_is_idempotent(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.INSTALLMENT)
    quoted = (
        APIClient()
        .post(QUOTE_URL, quote_payload(package, option, plan, guest_identity), format="json")
        .json()["data"]
    )

    first = APIClient().post(
        CONFIRM_URL,
        {"quote_token": quoted["quote_token"], "guest_identity": guest_identity},
        format="json",
    )
    assert first.status_code == 201
    data = first.json()["data"]
    item = CSAPurchaseRequest.objects.get(pk=data["id"])
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert data["guest_confirmation_token"]
    assert item.amount_snapshot == 1_300_000
    assert "shipping_fee_vnd" not in data
    assert item.payment_installments_snapshot == quoted["payment_summary"]["installments"]
    assert list(
        CSAPurchasePayment.objects.filter(purchase_request=item).values_list(
            "installment_sequence", "status"
        )
    ) == [(1, CSAPurchasePaymentStatus.REPORTED), (2, CSAPurchasePaymentStatus.PENDING)]

    retried = APIClient().post(
        CONFIRM_URL,
        {"quote_token": quoted["quote_token"], "guest_identity": guest_identity},
        format="json",
    )
    assert retried.status_code == 200
    assert retried.json()["data"]["id"] == data["id"]
    assert "guest_confirmation_token" not in retried.json()["data"]
    assert CSAPurchaseRequest.objects.count() == 1


@pytest.mark.django_db
def test_retry_returns_existing_request_after_shipping_area_is_disabled(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)
    quoted = (
        APIClient()
        .post(QUOTE_URL, quote_payload(package, option, plan, guest_identity), format="json")
        .json()["data"]
    )
    first = APIClient().post(
        CONFIRM_URL,
        {"quote_token": quoted["quote_token"], "guest_identity": guest_identity},
        format="json",
    )
    assert first.status_code == 201
    ShippingArea.objects.filter(ward_code=guest_identity["ward_code"]).update(is_active=False)

    retried = APIClient().post(
        CONFIRM_URL,
        {"quote_token": quoted["quote_token"], "guest_identity": guest_identity},
        format="json",
    )

    assert retried.status_code == 200
    assert retried.json()["data"]["id"] == first.json()["data"]["id"]
    assert CSAPurchaseRequest.objects.count() == 1


@pytest.mark.django_db
def test_confirm_rejects_expired_or_identity_mismatched_quote_without_creating_request(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)
    now = timezone.now()
    token, _quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        guest_identity=guest_identity,
        now=now,
    )
    assert _quote["expires_at"] == (now + timedelta(minutes=5)).isoformat()
    with pytest.raises(PaymentQuoteExpired):
        confirm_csa_payment_quote(
            quote_token=token,
            guest_identity=guest_identity,
            now=now + timedelta(minutes=5),
        )
    with pytest.raises(PaymentQuoteIdentityMismatch):
        confirm_csa_payment_quote(
            quote_token=token,
            guest_identity={**guest_identity, "address": "Khác"},
            now=now,
        )
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_authenticated_quote_is_bound_to_the_current_user(catalog, payment_settings, active_user):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)
    ShippingAddress.objects.create(
        user=active_user,
        recipient_name="Nguyễn Văn An",
        phone_number="+84901234567",
        province_code="66",
        ward_code="22015",
        address="Delivery address",
    )
    token, _quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        user=active_user,
    )
    another_user = User.objects.create_user(
        email="other@example.com",
        name="Other user",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )

    with pytest.raises(PaymentQuoteIdentityMismatch):
        confirm_csa_payment_quote(quote_token=token, user=another_user)

    address = ShippingAddress.objects.get(user=active_user)
    address.address = "Changed after quote"
    address.save(update_fields=["address", "updated_at"])
    with pytest.raises(PaymentQuoteIdentityMismatch):
        confirm_csa_payment_quote(quote_token=token, user=active_user)

    address.address = "Delivery address"
    address.save(update_fields=["address", "updated_at"])
    item, guest_token = confirm_csa_payment_quote(quote_token=token, user=active_user)
    assert item.user_id == active_user.pk
    assert item.name_snapshot == "Nguyễn Văn An"
    assert item.address_snapshot == "Delivery address"
    assert guest_token is None


@pytest.mark.django_db
def test_tampered_quote_token_is_unavailable(catalog, payment_settings, guest_identity):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.FULL)
    token, _quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        guest_identity=guest_identity,
    )
    tampered = f"{token[:-1]}{'x' if token[-1] != 'x' else 'y'}"

    response = APIClient().post(
        CONFIRM_URL,
        {"quote_token": tampered, "guest_identity": guest_identity},
        format="json",
    )
    assert response.status_code == 404
    assert response.json()["errors"] == [{"code": "csa_payment_quote_unavailable"}]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_quote_snapshot_survives_catalog_and_payment_setting_changes_before_confirmation(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.INSTALLMENT)
    token, quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        guest_identity=guest_identity,
    )
    MembershipPaymentPlan.objects.filter(pk=plan.pk).update(is_active=False, total_amount=999999)
    MembershipPackagePriceOption.objects.filter(pk=option.pk).update(
        is_active=False, active_duration_key=None
    )
    MembershipPackage.objects.filter(pk=package.pk).update(is_active=False)
    update_membership_payment_settings(
        bank_bin=BANK["bin"],
        bank_name=BANK["name"],
        account_number="987654321",
        account_name="CHANGED",
        direct_transfer_enabled=True,
    )

    item, _guest_token = confirm_csa_payment_quote(quote_token=token, guest_identity=guest_identity)
    assert item.amount_snapshot == 1_300_000
    assert item.account_number_snapshot == quote["payment"]["account_number"]
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED


def make_coupon(*, code="TETTRUNGTHU2026", discount_type="fixed", value="600000", **kwargs):
    now = timezone.now()
    return CSACoupon.objects.create(
        code=code,
        discount_type=discount_type,
        discount_value=Decimal(value),
        starts_at=kwargs.pop("starts_at", now - timedelta(days=1)),
        expires_at=kwargs.pop("expires_at", now + timedelta(days=1)),
        **kwargs,
    )


@pytest.mark.django_db
def test_coupon_code_normalizes_and_rejects_case_insensitive_duplicate():
    assert normalize_coupon_code(" tettrungthu2026 ") == "TETTRUNGTHU2026"
    coupon = make_coupon(code=" TetTrungThu2026 ")
    coupon.refresh_from_db()
    assert coupon.code == "TETTRUNGTHU2026"
    with pytest.raises(ValidationError):
        make_coupon(code="tettrungthu2026")
    with pytest.raises(CouponError) as error:
        normalize_coupon_code("  ")
    assert error.value.code == "coupon_invalid"


@pytest.mark.django_db
@pytest.mark.parametrize(
    (
        "payment_type",
        "discount_type",
        "value",
        "expected_discount",
        "expected_first",
        "expected_total",
    ),
    [
        ("full", "percent", "10", "110000", "990000", "990000"),
        ("full", "percent", "100", "1100000", "0", "0"),
        ("full", "fixed", "1100000", "1100000", "0", "0"),
        ("installment", "percent", "10", "50000", "450000", "1250000"),
        ("installment", "fixed", "500000", "500000", "0", "800000"),
    ],
)
def test_coupon_quote_confirm_snapshot_and_replay(
    catalog,
    payment_settings,
    guest_identity,
    payment_type,
    discount_type,
    value,
    expected_discount,
    expected_first,
    expected_total,
):
    package, option = catalog
    plan = make_plan(option, payment_type=payment_type)
    coupon = make_coupon(discount_type=discount_type, value=value)
    payload = {
        **quote_payload(package, option, plan, guest_identity),
        "coupon_code": " tettrungthu2026 ",
    }
    quoted = APIClient().post(QUOTE_URL, payload, format="json")
    assert quoted.status_code == 200, quoted.json()
    data = quoted.json()["data"]
    assert not CSAPurchaseRequest.objects.exists()
    assert data["coupon"]["code"] == coupon.code
    assert data["coupon"]["discount_amount"] == expected_discount
    assert data["payment_summary"]["customer_payable_total"] == expected_total
    assert data["qr_payload"]["amount"] == expected_first
    assert data["payment_summary"]["installments"][0]["amount_after_discount"] == expected_first
    if payment_type == "installment":
        assert data["payment_summary"]["installments"][1]["amount"] == "800000"
    confirmed = APIClient().post(
        CONFIRM_URL,
        {"quote_token": data["quote_token"], "guest_identity": guest_identity},
        format="json",
    )
    assert confirmed.status_code == 201, confirmed.json()
    item = CSAPurchaseRequest.objects.get(pk=confirmed.json()["data"]["id"])
    assert item.coupon_snapshot["customer_payable_total"] == expected_total
    assert str(item.customer_payable_total_snapshot) == expected_total
    assert item.amount_snapshot == int(plan.total_amount)
    assert item.payments.get(installment_sequence=1).amount_due == Decimal(expected_first)
    assert confirmed.json()["data"]["qr_payload"]["amount"] == expected_first
    assert admin_csa_purchase_payload(item)["coupon"]["code"] == coupon.code
    assert tracker_request_payload(item)["customer_payable_total"] == expected_total
    coupon.refresh_from_db()
    assert coupon.used_count == 1
    replay = APIClient().post(
        CONFIRM_URL,
        {"quote_token": data["quote_token"], "guest_identity": guest_identity},
        format="json",
    )
    assert replay.status_code == 200
    coupon.refresh_from_db()
    assert coupon.used_count == 1
    assert CSAPurchaseRequest.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payment_type", "maximum_discount", "expected_discount", "expected_initial"),
    [
        ("full", "100000", "100000", "1000000"),
        ("full", "600000", "550000", "550000"),
        ("installment", "40000", "40000", "460000"),
    ],
)
def test_percent_coupon_cap_is_applied_and_snapshotted(
    catalog,
    payment_settings,
    guest_identity,
    payment_type,
    maximum_discount,
    expected_discount,
    expected_initial,
):
    package, option = catalog
    plan = make_plan(option, payment_type=payment_type)
    coupon = make_coupon(
        discount_type="percent",
        value="50",
        maximum_discount_amount=Decimal(maximum_discount),
    )
    token, quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        coupon_code=coupon.code,
        guest_identity=guest_identity,
    )

    assert quote["coupon"]["maximum_discount_amount"] == maximum_discount
    assert quote["coupon"]["discount_amount"] == expected_discount
    assert quote["coupon"]["initial_payment_after_discount"] == expected_initial
    response_payload = csa_payment_quote_payload(token, quote)
    assert response_payload["coupon"]["maximum_discount_amount"] == maximum_discount
    assert response_payload["qr_payload"]["amount"] == expected_initial

    CSACoupon.objects.filter(pk=coupon.pk).update(
        maximum_discount_amount=Decimal("1"), is_active=False
    )
    request, _ = confirm_csa_payment_quote(quote_token=token, guest_identity=guest_identity)
    assert request.coupon_snapshot == quote["coupon"]
    assert request.payments.get(installment_sequence=1).amount_due == Decimal(expected_initial)


@pytest.mark.django_db
def test_coupon_fixed_above_first_payment_is_rejected(catalog, payment_settings, guest_identity):
    package, option = catalog
    plan = make_plan(option, payment_type="installment")
    make_coupon(value="500001")
    response = APIClient().post(
        QUOTE_URL,
        {**quote_payload(package, option, plan, guest_identity), "coupon_code": "tettrungthu2026"},
        format="json",
    )
    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "coupon_invalid_value"}]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("changes", "error_code"),
    [
        ({"is_active": False}, "coupon_inactive"),
        ({"starts_at": timezone.now() + timedelta(hours=1)}, "coupon_not_started"),
        ({"expires_at": timezone.now() - timedelta(seconds=1)}, "coupon_expired"),
        ({"used_count": 1}, "coupon_usage_exhausted"),
        ({"package_id": "other"}, "coupon_not_applicable"),
        ({"price_option_id": "other"}, "coupon_not_applicable"),
        ({"payment_type": "installment"}, "coupon_not_applicable"),
        ({"installment_count": 2}, "coupon_not_applicable"),
        ({"minimum_amount": Decimal("1200000")}, "coupon_minimum_amount_not_met"),
    ],
)
def test_coupon_eligibility_errors(catalog, payment_settings, guest_identity, changes, error_code):
    package, option = catalog
    plan = make_plan(option, payment_type="full")
    coupon = make_coupon()
    if "package_id" in changes or "price_option_id" in changes:
        other_package = MembershipPackage.objects.create(
            name="Other package", quota_policy=QuotaPolicy.ROLLOVER
        )
        other_option = MembershipPackagePriceOption.objects.create(
            package=other_package, duration_months=3, monthly_price_vnd=400_000
        )
        changes = {
            key: (other_package.pk if key == "package_id" else other_option.pk)
            if value == "other"
            else value
            for key, value in changes.items()
        }
    CSACoupon.objects.filter(pk=coupon.pk).update(**changes)
    response = APIClient().post(
        QUOTE_URL,
        {**quote_payload(package, option, plan, guest_identity), "coupon_code": coupon.code},
        format="json",
    )
    assert response.status_code in (400, 409)
    assert response.json()["errors"] == [{"code": error_code}]


@pytest.mark.django_db
def test_coupon_quote_retains_discount_after_live_coupon_change(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type="installment")
    coupon = make_coupon(value="100000")
    token, quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        coupon_code=coupon.code,
        guest_identity=guest_identity,
    )
    CSACoupon.objects.filter(pk=coupon.pk).update(discount_value=Decimal("200000"), is_active=False)
    item, _ = confirm_csa_payment_quote(quote_token=token, guest_identity=guest_identity)
    assert item.coupon_snapshot == quote["coupon"]
    assert item.payments.get(installment_sequence=1).amount_due == Decimal("400000")


@pytest.mark.django_db
def test_malformed_signed_coupon_id_returns_stable_quote_error(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type="full")
    coupon = make_coupon(value="100000")
    _token, quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        coupon_code=coupon.code,
        guest_identity=guest_identity,
    )
    quote["coupon_id"] = "not-a-uuid"
    with pytest.raises(PaymentQuoteUnavailable):
        _load_payment_quote(quote_token=_quote_token(quote), now=timezone.now())


@pytest.mark.django_db
def test_coupon_usage_checked_again_at_confirm_and_expired_quote_does_not_consume(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type="full")
    coupon = make_coupon(value="100000")
    now = timezone.now()
    token, _ = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        coupon_code=coupon.code,
        guest_identity=guest_identity,
        now=now,
    )
    with pytest.raises(PaymentQuoteExpired):
        confirm_csa_payment_quote(
            quote_token=token, guest_identity=guest_identity, now=now + timedelta(minutes=5)
        )
    assert not CSAPurchaseRequest.objects.exists()
    coupon.refresh_from_db()
    assert coupon.used_count == 0
    CSACoupon.objects.filter(pk=coupon.pk).update(used_count=1)
    with pytest.raises(CouponError) as error:
        confirm_csa_payment_quote(quote_token=token, guest_identity=guest_identity, now=now)
    assert error.value.code == "coupon_usage_exhausted"
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_coupon_cannot_be_used_twice_by_same_phone(catalog, payment_settings, guest_identity):
    package, option = catalog
    plan = make_plan(option, payment_type="full")
    coupon = make_coupon(value="100000", usage_limit=2)
    token, _ = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        coupon_code=coupon.code,
        guest_identity=guest_identity,
    )
    confirm_csa_payment_quote(quote_token=token, guest_identity=guest_identity)
    with pytest.raises(CouponError) as error:
        create_csa_payment_quote(
            package_id=package.pk,
            price_option_id=option.pk,
            payment_plan_id=plan.pk,
            coupon_code=coupon.code,
            guest_identity=guest_identity,
        )
    assert error.value.code == "coupon_already_used"


@pytest.mark.django_db
def test_coupon_without_catalog_payment_plan_uses_discounted_qr(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    coupon = make_coupon(value="100000")
    token, quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        coupon_code=coupon.code,
        guest_identity=guest_identity,
    )
    assert quote["selection"]["plan"]["total_amount"] == "1200000"
    assert csa_payment_quote_payload(token, quote)["qr_payload"]["amount"] == "1100000"
    item, _ = confirm_csa_payment_quote(quote_token=token, guest_identity=guest_identity)
    assert item.amount_snapshot == 1200000
    assert item.payments.get().amount_due == Decimal("1100000")


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="Row-lock concurrency requires PostgreSQL"
)
def test_concurrent_coupon_confirms_do_not_exceed_usage_limit(
    catalog, payment_settings, guest_identity
):
    package, option = catalog
    plan = make_plan(option, payment_type="full")
    coupon = make_coupon(value="100000", usage_limit=1)
    second_identity = {**guest_identity, "phone": "0987654321"}
    tokens = [
        create_csa_payment_quote(
            package_id=package.pk,
            price_option_id=option.pk,
            payment_plan_id=plan.pk,
            coupon_code=coupon.code,
            guest_identity=identity,
        )[0]
        for identity in (guest_identity, second_identity)
    ]

    def confirm(args):
        close_old_connections()
        try:
            confirm_csa_payment_quote(quote_token=args[0], guest_identity=args[1])
            return "ok"
        except CouponError as error:
            return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(confirm, zip(tokens, (guest_identity, second_identity), strict=True))
        )
    assert sorted(outcomes) == ["coupon_usage_exhausted", "ok"]
    coupon.refresh_from_db()
    assert coupon.used_count == 1
    assert CSAPurchaseRequest.objects.count() == 1
