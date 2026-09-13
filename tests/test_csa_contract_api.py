import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Group
from django.db import connection
from django.utils import timezone
from oauth2_provider.models import AccessToken
from rest_framework.test import APIClient

from identity.models import AccountStatus, User
from memberships.models import (
    CSAContract,
    CSAContractStatus,
    CSACustomer,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    Product,
    QuotaPolicy,
)
from memberships.services import (
    assign_membership,
    assign_membership_with_contract,
    revoke_membership,
)

TEST_BANK = {"bin": "970436", "name": "Vietcombank"}


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture(autouse=True)
def contract_api_storage(monkeypatch, settings, tmp_path):
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"


@pytest.fixture
def admin_api(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


@pytest.fixture
def self_api(access_token):
    return bearer_client(access_token)


@pytest.fixture
def other_user_token(oauth_application):
    user = User.objects.create_user(
        email="other-contract-user@example.com",
        name="Other user",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    token = AccessToken.objects.create(
        user=user,
        application=oauth_application,
        token="other-contract-token",
        expires=timezone.now() + timedelta(hours=1),
        scope="profile email",
    )
    return user, token


@pytest.fixture
def contract(active_user):
    payment = MembershipPaymentSettings.objects.create(
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
        name="CSA package",
        description="Seasonal produce",
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
        name=active_user.name,
        phone_e164="0901234567",
        province_code="66",
        ward_code="22015",
        address="12 Orchard Road",
    )
    membership = assign_membership_with_contract(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate(),
        farmbrite_order_reference="ORDER-CONTRACT-API",
        actor=active_user,
    )
    assert payment.pk == "default"
    return CSAContract.objects.get(membership=membership)


def admin_detail_url(contract):
    return f"/api/v1/admin/memberships/{contract.membership_id}/contract"


def admin_pdf_url(contract, locale="vi"):
    return f"{admin_detail_url(contract)}/pdf?locale={locale}"


def self_detail_url(contract):
    return f"/api/v1/memberships/{contract.membership_id}/contract"


def self_pdf_url(contract, locale="vi"):
    return f"{self_detail_url(contract)}/pdf?locale={locale}"


def streamed_content(response):
    return b"".join(response.streaming_content)


def assert_sensitive_contract_fields_absent(data):
    serialized = str(data)
    for field in (
        "storage_key",
        "original_pdf",
        "terms_acceptance_ip",
        "terms_acceptance_user_agent",
        "guest_token",
        "guest_token_hash",
    ):
        assert field not in serialized


@pytest.mark.django_db
def test_admin_reads_contract_and_membership_customer_summaries(admin_api, contract):
    response = admin_api.get(admin_detail_url(contract))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["id"] == str(contract.pk)
    assert data["membership_id"] == str(contract.membership_id)
    assert data["customer_id"] == str(contract.membership.customer_id)
    assert data["status"] == CSAContractStatus.ACTIVE
    assert data["available_locales"] == ["vi", "en"]
    assert data["pdf_version"] == 1
    assert data["current_version"]["version_number"] == 1
    assert data["current_version"]["version_reason"] == "issued"
    assert data["current_version"]["available_locales"] == ["vi", "en"]
    assert [version["version_number"] for version in data["versions"]] == [1]
    assert_sensitive_contract_fields_absent(data)

    membership = admin_api.get(
        f"/api/v1/admin/memberships/{contract.membership_id}?locale=vi"
    ).json()["data"]
    customer = admin_api.get(
        f"/api/v1/admin/csa-customers/{contract.membership.customer_id}"
    ).json()["data"]
    expected_summary = {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "status": contract.status,
    }
    assert membership["contract"] == expected_summary
    assert customer["memberships"][0]["contract"] == expected_summary


@pytest.mark.django_db
def test_admin_reads_revoked_contract_reason(admin_api, contract, active_user, settings):
    revoke_membership(
        membership_id=contract.membership_id,
        reason="Customer requested cancellation",
        actor=active_user,
    )

    data = admin_api.get(admin_detail_url(contract)).json()["data"]

    assert data["status"] == CSAContractStatus.REVOKED
    assert data["revocation_reason"] == "Customer requested cancellation"
    assert data["revoked_by_name_snapshot"] == active_user.name
    assert data["pdf_version"] == 2


@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.django_db
def test_admin_streams_verified_current_pdf_with_secure_headers(admin_api, contract, locale):
    response = admin_api.get(admin_pdf_url(contract, locale))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Cache-Control"] == "private, no-store"
    assert response["Pragma"] == "no-cache"
    assert response["X-Content-Type-Options"] == "nosniff"
    assert f"{contract.reference_code}.{locale}.pdf" in response["Content-Disposition"]
    assert streamed_content(response).startswith(b"%PDF-")
    assert response.status_code not in {301, 302, 307, 308}


@pytest.mark.django_db
def test_pdf_endpoint_never_exposes_or_selects_original_pdf(admin_api, contract, settings):
    original_key = contract.original_pdf_vi_storage_key
    original_hash = contract.original_pdf_vi_sha256
    revoke_membership(
        membership_id=contract.membership_id,
        reason="Cancelled",
        actor=contract.membership.customer.user,
    )
    contract.refresh_from_db()
    assert contract.pdf_vi_sha256 != original_hash

    response = admin_api.get(f"{admin_pdf_url(contract)}&original=true")
    assert response.status_code == 404
    assert response.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    current = admin_api.get(admin_pdf_url(contract))
    content = streamed_content(current)
    assert hashlib.sha256(content).hexdigest() == contract.pdf_vi_sha256
    assert hashlib.sha256(content).hexdigest() != original_hash
    assert original_key not in current.headers.get("Content-Disposition", "")
    assert str(settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT) not in str(current.headers)


@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.django_db
def test_admin_and_owner_download_current_and_historical_versions(
    admin_api, self_api, contract, active_user, locale
):
    original = contract.current_version
    revoke_membership(
        membership_id=contract.membership_id,
        reason="Cancelled",
        actor=active_user,
    )
    contract.refresh_from_db()
    current = contract.current_version
    for client, url in (
        (admin_api, admin_pdf_url(contract, locale)),
        (self_api, self_pdf_url(contract, locale)),
    ):
        old_pdf = client.get(f"{url}&version_id={original.pk}")
        new_pdf = client.get(f"{url}&version_id={current.pk}")
        default_pdf = client.get(url)
        assert old_pdf.status_code == new_pdf.status_code == default_pdf.status_code == 200
        assert f"{contract.reference_code}.v1.{locale}.pdf" in old_pdf["Content-Disposition"]
        assert f"{contract.reference_code}.v2.{locale}.pdf" in new_pdf["Content-Disposition"]
        assert f"{contract.reference_code}.{locale}.pdf" in default_pdf["Content-Disposition"]
        assert hashlib.sha256(streamed_content(old_pdf)).hexdigest() == getattr(
            original, f"pdf_{locale}_sha256"
        )
        assert hashlib.sha256(streamed_content(new_pdf)).hexdigest() == getattr(
            current, f"pdf_{locale}_sha256"
        )
        assert hashlib.sha256(streamed_content(default_pdf)).hexdigest() == getattr(
            current, f"pdf_{locale}_sha256"
        )
        detail = client.get(url.split("/pdf?")[0]).json()["data"]
        assert [item["version_number"] for item in detail["versions"]] == [1, 2]
        assert detail["versions"][0]["contract_status"] == "active"
        assert detail["versions"][1]["contract_status"] == "revoked"
        assert detail["versions"][1]["paid_cycles"] == current.paid_cycles
        assert "paid_cycles" not in detail["versions"][0]
        assert_sensitive_contract_fields_absent(detail)


@pytest.mark.django_db
def test_version_query_is_strict_and_scoped_to_owned_contract(
    admin_api, self_api, contract, other_user_token
):
    url = self_pdf_url(contract)
    for suffix in (
        "&version_id=invalid",
        "&version_id=00000000-0000-0000-0000-000000000000",
        "&original=true",
        "&locale=en",
        "&version_id=",
    ):
        assert self_api.get(url + suffix).status_code == 404
    assert admin_api.get(admin_pdf_url(contract) + "&extra=1").status_code == 404
    _other_user, token = other_user_token
    assert (
        bearer_client(token).get(url + f"&version_id={contract.current_version_id}").status_code
        == 404
    )

    other_customer = CSACustomer.objects.create(
        user=_other_user,
        name=_other_user.name,
        phone_e164="0987654321",
        province_code="66",
        ward_code="22015",
        address="Another street",
    )
    other_membership = assign_membership_with_contract(
        customer_id=other_customer.pk,
        package_id=contract.membership.package_id,
        price_option_id=contract.membership.price_option_id,
        start_date=timezone.localdate(),
        farmbrite_order_reference="ORDER-OTHER-CONTRACT",
        actor=_other_user,
    )
    other_contract = CSAContract.objects.get(membership=other_membership)
    assert (
        admin_api.get(
            admin_pdf_url(contract) + f"&version_id={other_contract.current_version_id}"
        ).status_code
        == 404
    )
    assert (
        self_api.get(
            self_pdf_url(contract) + f"&version_id={other_contract.current_version_id}"
        ).status_code
        == 404
    )


@pytest.mark.django_db
def test_legacy_contract_without_version_keeps_current_pdf(admin_api, contract):
    legacy_version_id = contract.current_version_id
    CSAContract.objects.filter(pk=contract.pk).update(current_version=None)
    with connection.cursor() as cursor:
        cursor.execute(
            "DELETE FROM memberships_csacontractversion WHERE id = %s",
            [legacy_version_id.hex],
        )

    detail = admin_api.get(admin_detail_url(contract))
    current = admin_api.get(admin_pdf_url(contract))
    unavailable = admin_api.get(admin_pdf_url(contract) + f"&version_id={legacy_version_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["versions"] == []
    assert current.status_code == 200
    assert streamed_content(current).startswith(b"%PDF-")
    assert unavailable.status_code == 404


@pytest.mark.django_db
def test_version_pdf_missing_hash_and_key_are_rejected(admin_api, contract, active_user, settings):
    original = contract.current_version
    revoke_membership(
        membership_id=contract.membership_id,
        reason="Cancelled",
        actor=active_user,
    )
    url = admin_pdf_url(contract) + f"&version_id={original.pk}"
    path = settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT / original.pdf_vi_storage_key
    path.unlink()
    assert admin_api.get(url).json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tampered")
    assert admin_api.get(url).json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE memberships_csacontractversion SET pdf_vi_storage_key = %s WHERE id = %s",
            ["../../etc/passwd", original.pk.hex],
        )
    assert admin_api.get(url).json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]


@pytest.mark.django_db
def test_non_admin_cannot_use_admin_contract_endpoints(self_api, contract):
    assert self_api.get(admin_detail_url(contract)).status_code == 403
    assert self_api.get(admin_pdf_url(contract)).status_code == 403


@pytest.mark.django_db
def test_missing_contract_and_legacy_membership_remain_safe(admin_api, contract, active_user):
    missing = admin_api.get(
        "/api/v1/admin/memberships/00000000-0000-0000-0000-000000000000/contract"
    )
    assert missing.status_code == 404

    customer = CSACustomer.objects.create(
        name="Legacy customer",
        phone_e164="0912345678",
        province_code="66",
        ward_code="22015",
        address="Store",
    )
    legacy = assign_membership(
        customer_id=customer.pk,
        package_id=contract.membership.package_id,
        price_option_id=contract.membership.price_option_id,
        start_date=timezone.localdate(),
        farmbrite_order_reference="LEGACY-NO-CONTRACT",
        actor=active_user,
    )
    detail = admin_api.get(f"/api/v1/admin/memberships/{legacy.pk}?locale=vi")
    assert detail.status_code == 200
    assert detail.json()["data"]["contract"] is None
    assert admin_api.get(f"/api/v1/admin/memberships/{legacy.pk}/contract").status_code == 404


@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.django_db
def test_owner_reads_and_downloads_contract(self_api, contract, locale):
    detail = self_api.get(self_detail_url(contract))
    pdf = self_api.get(self_pdf_url(contract, locale))

    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == str(contract.pk)
    assert detail.json()["data"]["current_version"]["version_number"] == 1
    assert detail.json()["data"]["versions"][0]["version_reason"] == "issued"
    assert_sensitive_contract_fields_absent(detail.json()["data"])
    assert "revoked_by_name_snapshot" not in detail.json()["data"]
    assert pdf.status_code == 200
    assert streamed_content(pdf).startswith(b"%PDF-")


@pytest.mark.django_db
def test_owner_sees_revoked_status_and_reason_without_internal_actor(
    self_api, contract, active_user
):
    contract.status = CSAContractStatus.REVOKED
    contract.revoked_at = timezone.now()
    contract.revoked_by = active_user
    contract.revoked_by_name_snapshot = active_user.name
    contract.revocation_reason = "Customer requested cancellation"
    contract.save(
        update_fields=[
            "status",
            "revoked_at",
            "revoked_by",
            "revoked_by_name_snapshot",
            "revocation_reason",
            "updated_at",
        ]
    )

    data = self_api.get(self_detail_url(contract)).json()["data"]

    assert data["status"] == CSAContractStatus.REVOKED
    assert data["revocation_reason"] == "Customer requested cancellation"
    assert "revoked_by" not in str(data)


@pytest.mark.django_db
def test_other_or_unlinked_user_cannot_discover_contract(contract, other_user_token):
    _other_user, token = other_user_token
    other_api = bearer_client(token)

    assert other_api.get(self_detail_url(contract)).status_code == 404
    assert other_api.get(self_pdf_url(contract)).status_code == 404


@pytest.mark.django_db
def test_invalid_locale_is_rejected_without_storage_access(self_api, contract, monkeypatch):
    opened = False

    def fail_if_called():
        nonlocal opened
        opened = True
        raise AssertionError("storage must not be opened")

    monkeypatch.setattr("memberships.contract_access.get_contract_pdf_storage", fail_if_called)
    response = self_api.get(self_pdf_url(contract, "../../etc/passwd"))

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "invalid_contract_locale"}]
    assert opened is False


@pytest.mark.django_db
def test_missing_or_tampered_pdf_returns_stable_error(admin_api, contract, settings):
    path = settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT / contract.pdf_vi_storage_key
    path.unlink()
    missing = admin_api.get(admin_pdf_url(contract))
    assert missing.status_code == 404
    assert missing.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tampered PDF")
    tampered = admin_api.get(admin_pdf_url(contract))
    assert tampered.status_code == 404
    assert tampered.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]


@pytest.mark.django_db
def test_tampered_storage_key_and_path_traversal_are_rejected(admin_api, contract):
    # Simulate out-of-band database corruption; the version ORM intentionally forbids updates.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE memberships_csacontractversion "
            "SET pdf_vi_storage_key = %s, pdf_vi_sha256 = %s WHERE id = %s",
            [
                "../../etc/passwd",
                hashlib.sha256(b"unrelated").hexdigest(),
                contract.current_version_id.hex,
            ],
        )

    response = admin_api.get(admin_pdf_url(contract))

    assert response.status_code == 404
    assert response.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    assert "passwd" not in response.content.decode()


@pytest.mark.django_db
def test_contract_gets_do_not_create_or_regenerate_records(self_api, contract):
    before = (
        CSAContract.objects.count(),
        contract.pdf_version,
        contract.pdf_generated_at,
        contract.pdf_vi_sha256,
        contract.pdf_en_sha256,
    )

    assert self_api.get(self_detail_url(contract)).status_code == 200
    vi_response = self_api.get(self_pdf_url(contract))
    assert vi_response.status_code == 200
    vi_response.close()
    response = self_api.get(self_pdf_url(contract, "en"))
    streamed_content(response)
    response.close()
    contract.refresh_from_db()

    assert (
        CSAContract.objects.count(),
        contract.pdf_version,
        contract.pdf_generated_at,
        contract.pdf_vi_sha256,
        contract.pdf_en_sha256,
    ) == before
