from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AccountStatus, User
from members.models import MemberProfile
from memberships.models import (
    CSAContract,
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
    update_membership_payment_settings,
)

QUOTE_URL = "/api/v1/public/csa-payment-quotes"
CONFIRM_URL = "/api/v1/public/csa-purchase-requests/confirm-transfer"
BANK = {"bin": "970436", "name": "Vietcombank"}


@pytest.fixture(autouse=True)
def quote_test_isolation(monkeypatch):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [BANK])
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
    assert "Nguyễn Văn An" not in data["quote_token"]
    assert "+84912345678" not in data["quote_token"]
    assert "guest_token" not in data
    assert response.headers["Cache-Control"] == "private, no-store"


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
    MemberProfile.objects.create(
        user=active_user,
        phone_number="0901234567",
        province_code="66",
        ward_code="22015",
        address="Auth account address",
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
    item, guest_token = confirm_csa_payment_quote(quote_token=token, user=active_user)
    assert item.user_id == active_user.pk
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
