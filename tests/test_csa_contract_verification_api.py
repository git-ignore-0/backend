from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AuditLog
from memberships.expiry import expire_csa_purchase_requests
from memberships.models import (
    CSAContract,
    CSAContractStatus,
    CSACustomer,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    CSATrackerSession,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    Product,
    QuotaPolicy,
)
from memberships.purchases import create_public_csa_purchase_request
from memberships.services import assign_membership_with_contract

VERIFY_URL = "/api/v1/public/csa-contracts/verify"
TEST_BANK = {"bin": "970436", "name": "Vietcombank"}


@pytest.fixture(autouse=True)
def verification_environment(monkeypatch, settings, tmp_path, default_supported_shipping_area):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"
    yield
    cache.clear()


@pytest.fixture
def contract_setup(active_user):
    MembershipPaymentSettings.objects.create(
        direct_transfer_enabled=True,
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
    )
    product = Product.objects.create(
        name="Seasonal basket",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    package = MembershipPackage.objects.create(
        name="Verification package",
        description="Seasonal vegetables",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=2)
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=500_000,
    )
    customer = CSACustomer.objects.create(
        user=active_user,
        name="Private customer name",
        phone_e164="0901234567",
        province_code="66",
        ward_code="22015",
        address="Private customer address",
    )
    membership = assign_membership_with_contract(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate(),
        farmbrite_order_reference="VERIFY-CONTRACT-ORDER",
        actor=active_user,
    )
    return CSAContract.objects.get(membership=membership), package, option


def verify(client, reference_code, **extra):
    return client.get(
        VERIFY_URL,
        {"reference_code": reference_code, **extra},
        REMOTE_ADDR="203.0.113.50",
    )


def assert_verification_unavailable(response):
    assert response.status_code == 404
    assert response.json() == {"errors": [{"code": "csa_contract_verification_unavailable"}]}


@pytest.mark.django_db
def test_active_contract_verification_is_public_minimal_and_read_only(contract_setup):
    contract, _package, _option = contract_setup
    counts_before = {
        "contracts": CSAContract.objects.count(),
        "sessions": CSATrackerSession.objects.count(),
        "audits": AuditLog.objects.count(),
    }

    with CaptureQueriesContext(connection) as queries:
        response = verify(APIClient(), f"  {contract.reference_code.lower()}  ")

    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response["Pragma"] == "no-cache"
    assert response.json()["data"] == {
        "reference_code": contract.reference_code,
        "status": CSAContractStatus.ACTIVE,
        "issued_at": contract.issued_at.isoformat(),
        "start_date": contract.start_date.isoformat(),
        "end_date": contract.end_date.isoformat(),
        "revoked_at": None,
        "revocation_reason": None,
    }
    serialized = str(response.json())
    for sensitive in (
        contract.member_name_snapshot,
        contract.phone_e164_snapshot,
        contract.address_snapshot,
        contract.package_name_snapshot,
        "customer_id",
        "membership_id",
        "purchase_request_id",
        "storage_key",
        "pdf",
        "guest_token",
        "bank",
        "terms",
        "amount",
    ):
        assert sensitive not in serialized
    assert counts_before == {
        "contracts": CSAContract.objects.count(),
        "sessions": CSATrackerSession.objects.count(),
        "audits": AuditLog.objects.count(),
    }
    assert all(
        not query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        for query in queries.captured_queries
    )


@pytest.mark.django_db
def test_revoked_contract_verification_returns_only_public_revocation(contract_setup, active_user):
    contract, _package, _option = contract_setup
    contract.status = CSAContractStatus.REVOKED
    contract.revoked_at = timezone.now()
    contract.revoked_by = active_user
    contract.revocation_reason = "Contract holder requested revocation"
    contract.save(
        update_fields=[
            "status",
            "revoked_at",
            "revoked_by",
            "revocation_reason",
            "updated_at",
        ]
    )

    response = verify(APIClient(), contract.reference_code)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == CSAContractStatus.REVOKED
    assert data["revoked_at"] == contract.revoked_at.isoformat()
    assert data["revocation_reason"] == "Contract holder requested revocation"
    assert active_user.name not in str(data)
    assert "revoked_by" not in data


@pytest.mark.parametrize(
    "request_status",
    [
        CSAPurchaseRequestStatus.PENDING,
        CSAPurchaseRequestStatus.REJECTED,
        CSAPurchaseRequestStatus.EXPIRED,
    ],
)
@pytest.mark.django_db
def test_purchase_request_codes_are_never_verified_as_contracts(
    request_status, contract_setup, active_user
):
    _contract, package, option = contract_setup
    item, _token = create_public_csa_purchase_request(
        package_id=package.pk,
        price_option_id=option.pk,
        name="Request only",
        phone="0912345678",
        province_code="66",
        ward_code="22015",
        address="Request-only address",
    )
    if request_status == CSAPurchaseRequestStatus.REJECTED:
        item.status = request_status
        item.rejected_at = timezone.now()
        item.rejected_by = active_user
        item.rejection_reason = "Rejected request"
        item.save(
            update_fields=[
                "status",
                "rejected_at",
                "rejected_by",
                "rejection_reason",
                "updated_at",
            ]
        )
    elif request_status == CSAPurchaseRequestStatus.EXPIRED:
        CSAPurchaseRequest.objects.filter(pk=item.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert expire_csa_purchase_requests() == 1

    assert_verification_unavailable(verify(APIClient(), item.request_code))
    assert not CSAContract.objects.filter(reference_code=item.request_code).exists()


@pytest.mark.parametrize(
    ("reference_code", "extra"),
    [
        ("CSA-AAAAAAAAAAAA", {}),
        ("../../private/contracts/file.pdf", {}),
        ("CSA-202609-ABCDEF", {"phone": "0901234567"}),
    ],
)
@pytest.mark.django_db
def test_invalid_unknown_and_extra_query_use_one_safe_error(reference_code, extra):
    response = verify(APIClient(), reference_code, **extra)

    assert_verification_unavailable(response)
    assert response["Cache-Control"] == "private, no-store"
    assert "file.pdf" not in str(response.json())


@pytest.mark.django_db
def test_invalid_contract_snapshot_is_not_verifiable(contract_setup):
    contract, _package, _option = contract_setup
    CSAContract.objects.filter(pk=contract.pk).update(terms_snapshot={})

    assert_verification_unavailable(verify(APIClient(), contract.reference_code))


@pytest.mark.django_db
def test_verification_endpoint_cannot_be_used_to_download_pdf(contract_setup):
    contract, _package, _option = contract_setup

    response = verify(APIClient(), contract.reference_code, locale="vi")

    assert_verification_unavailable(response)
    assert response.get("Content-Type") == "application/json"
    assert "Content-Disposition" not in response


@pytest.mark.django_db
def test_verification_is_throttled_per_ip(contract_setup):
    contract, _package, _option = contract_setup
    client = APIClient()

    responses = [verify(client, contract.reference_code) for _attempt in range(21)]

    assert [response.status_code for response in responses[:20]] == [200] * 20
    assert responses[20].status_code == 429
    assert responses[20]["Cache-Control"] == "private, no-store"
