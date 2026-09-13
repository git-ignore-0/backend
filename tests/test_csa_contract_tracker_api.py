import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AuditLog
from memberships.expiry import expire_csa_purchase_request_if_due
from memberships.models import (
    CSAContract,
    CSAContractVersion,
    CSAPurchaseRefundStatus,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    CSATrackerSession,
    Membership,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    Product,
    QuotaPolicy,
)
from memberships.purchases import (
    approve_csa_purchase_request,
    create_public_csa_purchase_request,
    reject_csa_purchase_request,
)
from memberships.services import revoke_membership
from memberships.tracker import TRACKER_COOKIE_NAME

LOOKUP_URL = "/api/v1/public/csa-contract-tracker/lookup"
CONTRACT_URL = "/api/v1/public/csa-contract-tracker/contract"
PDF_URL = f"{CONTRACT_URL}/pdf"
TEST_BANK = {"bin": "970436", "name": "Vietcombank"}


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture(autouse=True)
def tracker_environment(monkeypatch, settings, tmp_path):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"
    yield
    cache.clear()


@pytest.fixture
def admin_api(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


@pytest.fixture
def package_and_option():
    product = Product.objects.create(
        name="Seasonal basket",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    package = MembershipPackage.objects.create(
        name="Guest CSA package",
        description="Seasonal vegetables",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=2)
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=500_000,
    )
    return package, option


@pytest.fixture
def payment_settings():
    return MembershipPaymentSettings.objects.create(
        direct_transfer_enabled=True,
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
    )


@pytest.fixture
def request_factory(package_and_option, payment_settings):
    package, option = package_and_option

    def create(*, phone="0912345678", status=CSAPurchaseRequestStatus.PENDING):
        item, _guest_token = create_public_csa_purchase_request(
            package_id=package.pk,
            price_option_id=option.pk,
            name="Guest customer",
            phone=phone,
            province_code="66",
            ward_code="22015",
            address="12 Orchard Road",
        )
        if status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED:
            item.status = status
            item.payment_confirmed_at = timezone.now()
            item.save(update_fields=["status", "payment_confirmed_at", "updated_at"])
        return item

    return create


def approve_request(item, actor):
    if item.status == CSAPurchaseRequestStatus.PENDING:
        item.status = CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
        item.payment_confirmed_at = timezone.now()
        item.save(update_fields=["status", "payment_confirmed_at", "updated_at"])
    approve_csa_purchase_request(
        request_id=item.pk,
        start_date=timezone.localdate(),
        actor=actor,
    )
    item.refresh_from_db()
    return item


def lookup(client, item, phone="0912345678"):
    return client.post(
        LOOKUP_URL,
        {"reference_code": f"  {item.request_code.lower()}  ", "phone": phone},
        format="json",
    )


def content(response):
    return b"".join(response.streaming_content)


@pytest.mark.parametrize("phone", ["0912345678", "84912345678", "+84912345678"])
@pytest.mark.django_db
def test_lookup_normalizes_phone_and_creates_private_short_lived_cookie(
    request_factory, phone, settings
):
    settings.SESSION_COOKIE_SECURE = True
    item = request_factory()
    client = APIClient()

    response = lookup(client, item, phone)

    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    data = response.json()["data"]
    assert data["reference_code"] == item.request_code
    assert data["status"] == CSAPurchaseRequestStatus.PENDING
    assert data["contract"] is None
    assert data["pdf_available"] is False
    assert "versions" not in data
    assert "membership" not in str(data)
    cookie = response.cookies[TRACKER_COOKIE_NAME]
    assert cookie["httponly"] is True
    assert cookie["secure"] is True
    assert cookie["samesite"] == "Lax"
    assert 0 < int(cookie["max-age"]) <= 600
    assert item.request_code not in cookie.value
    assert item.phone_e164 not in cookie.value
    assert str(item.pk) not in cookie.value
    assert item.guest_token_hash not in cookie.value
    session = CSATrackerSession.objects.get()
    assert session.token_hash != cookie.value
    assert item.phone_e164 not in session.token_hash


@pytest.mark.parametrize(
    "payload",
    [
        {"reference_code": "CSA-DOES-NOT-EXIST", "phone": "0912345678"},
        {"reference_code": "VALID_CODE", "phone": "0987654321"},
        {"reference_code": "00000000-0000-0000-0000-000000000000", "phone": "0912345678"},
        {"reference_code": "name@example.com", "phone": "0912345678", "email": "x@y.z"},
    ],
)
@pytest.mark.django_db
def test_lookup_failures_use_one_generic_error(request_factory, payload):
    item = request_factory()
    if payload["reference_code"] == "VALID_CODE":
        payload["reference_code"] = item.request_code

    response = APIClient().post(LOOKUP_URL, payload, format="json")

    assert response.status_code == 404
    assert response.json()["errors"] == [{"code": "csa_tracker_lookup_unavailable"}]
    assert TRACKER_COOKIE_NAME not in response.cookies


@pytest.mark.django_db
def test_payment_confirmed_lookup_has_no_contract_or_pdf(request_factory):
    item = request_factory(status=CSAPurchaseRequestStatus.PAYMENT_CONFIRMED)

    data = lookup(APIClient(), item).json()["data"]

    assert data["status"] == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert data["payment_confirmed_at"] is not None
    assert data["contract"] is None
    assert data["pdf_available"] is False
    assert "versions" not in data


@pytest.mark.django_db
def test_approved_lookup_returns_contract_membership_snapshot(request_factory, active_user):
    item = approve_request(request_factory(), active_user)

    response = lookup(APIClient(), item)

    assert response.status_code == 200
    data = response.json()["data"]
    contract = CSAContract.objects.get(purchase_request=item)
    assert data["reference_type"] == "contract"
    assert data["status"] == CSAPurchaseRequestStatus.APPROVED
    assert data["membership_start_date"] == item.membership.start_date.isoformat()
    assert data["membership_end_date"] == item.membership.end_date.isoformat()
    assert data["contract"] == {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "status": contract.status,
    }
    assert data["available_pdf_locales"] == ["vi", "en"]
    assert [version["version_number"] for version in data["versions"]] == [1]
    assert data["pdf_available"] is True
    serialized = str(data)
    assert "storage_key" not in serialized
    assert "guest_token" not in serialized
    assert "customer_id" not in serialized


@pytest.mark.django_db
def test_tracker_session_is_bound_expires_and_is_replaced(request_factory, active_user):
    first = approve_request(request_factory(), active_user)
    client = APIClient()
    assert lookup(client, first).status_code == 200
    first_token = client.cookies[TRACKER_COOKIE_NAME].value
    bound = client.get(CONTRACT_URL, {"request_id": "00000000-0000-0000-0000-000000000000"})
    assert bound.json()["data"]["reference_code"] == first.request_code

    copied_url_client = APIClient()
    assert copied_url_client.get(PDF_URL, {"locale": "vi"}).status_code == 401

    CSATrackerSession.objects.filter(token_hash__isnull=False).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    expired = client.get(CONTRACT_URL)
    assert expired.status_code == 401
    assert expired.json()["errors"] == [{"code": "csa_tracker_session_expired"}]

    second = request_factory(phone="0987654321")
    approve_request(second, active_user)
    assert lookup(client, first).status_code == 200
    old_token = client.cookies[TRACKER_COOKIE_NAME].value
    assert lookup(client, second, "0987654321").status_code == 200
    old_client = APIClient()
    old_client.cookies[TRACKER_COOKIE_NAME] = old_token
    assert old_client.get(CONTRACT_URL).status_code == 401
    assert client.get(CONTRACT_URL).json()["data"]["reference_code"] == second.request_code
    assert first_token != client.cookies[TRACKER_COOKIE_NAME].value


@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.django_db
def test_guest_session_reads_contract_and_streams_current_pdf(request_factory, active_user, locale):
    item = approve_request(request_factory(), active_user)
    client = APIClient()
    lookup(client, item)

    detail = client.get(CONTRACT_URL)
    pdf = client.get(PDF_URL, {"locale": locale})

    assert detail.status_code == 200
    assert detail.json()["data"]["reference_code"] == item.request_code
    assert "customer_id" not in str(detail.json())
    assert "storage_key" not in str(detail.json())
    assert [version["version_number"] for version in detail.json()["data"]["versions"]] == [1]
    assert detail.json()["data"]["current_version"]["paid_cycles"] >= 0
    assert pdf.status_code == 200
    assert pdf["Content-Type"] == "application/pdf"
    assert pdf["Cache-Control"] == "private, no-store"
    assert pdf["Pragma"] == "no-cache"
    assert pdf["X-Content-Type-Options"] == "nosniff"
    assert f"{item.request_code}.{locale}.pdf" in pdf["Content-Disposition"]
    assert content(pdf).startswith(b"%PDF-")


@pytest.mark.parametrize(
    "status", [CSAPurchaseRequestStatus.PENDING, CSAPurchaseRequestStatus.PAYMENT_CONFIRMED]
)
@pytest.mark.django_db
def test_open_request_cannot_read_contract_or_pdf(request_factory, status):
    item = request_factory(status=status)
    client = APIClient()
    lookup(client, item)

    detail = client.get(CONTRACT_URL)
    pdf = client.get(PDF_URL, {"locale": "vi"})

    assert detail.status_code == 404
    assert detail.json()["errors"] == [{"code": "csa_tracker_contract_unavailable"}]
    assert pdf.status_code == 404
    assert pdf.json()["errors"] == [{"code": "csa_tracker_contract_unavailable"}]


@pytest.mark.parametrize("terminal", ["rejected", "expired"])
@pytest.mark.django_db
def test_terminal_request_never_exposes_contract_versions(request_factory, terminal):
    item = request_factory()
    if terminal == "rejected":
        reject_csa_purchase_request(request_id=item.pk, reason="Cancelled", actor=None)
    else:
        CSAPurchaseRequest.objects.filter(pk=item.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert expire_csa_purchase_request_if_due(request_id=item.pk)
    item.refresh_from_db()
    client = APIClient()
    lookup_response = lookup(client, item)
    assert lookup_response.status_code == 200
    data = lookup_response.json()["data"]
    assert data["contract"] is None
    assert "versions" not in data
    assert client.get(CONTRACT_URL).status_code == 404
    assert (
        client.get(PDF_URL, {"version_id": "00000000-0000-0000-0000-000000000000"}).status_code
        == 404
    )


@pytest.mark.django_db
def test_guest_pdf_rejects_invalid_locale_hash_and_storage_key(
    request_factory, active_user, settings
):
    item = approve_request(request_factory(), active_user)
    client = APIClient()
    lookup(client, item)
    contract = CSAContract.objects.get(purchase_request=item)

    invalid_locale = client.get(PDF_URL, {"locale": "../../original"})
    assert invalid_locale.status_code == 400
    assert invalid_locale.json()["errors"] == [{"code": "invalid_contract_locale"}]

    path = settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT / contract.pdf_vi_storage_key
    path.write_bytes(b"tampered")
    bad_hash = client.get(PDF_URL, {"locale": "vi"})
    assert bad_hash.status_code == 404
    assert bad_hash.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]

    CSAContract.objects.filter(pk=contract.pk).update(
        pdf_vi_storage_key="../../etc/passwd",
        pdf_vi_sha256=hashlib.sha256(b"unrelated").hexdigest(),
    )
    bad_key = client.get(f"{PDF_URL}?locale=vi&original=true")
    assert bad_key.status_code == 404
    assert bad_key.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    assert "passwd" not in bad_key.content.decode()


@pytest.mark.django_db
def test_guest_cannot_select_original_pdf(request_factory, active_user, settings):
    item = approve_request(request_factory(), active_user)
    contract = CSAContract.objects.get(purchase_request=item)
    original_hash = contract.original_pdf_vi_sha256
    revoke_membership(membership_id=contract.membership_id, reason="Cancelled", actor=active_user)
    contract.refresh_from_db()
    client = APIClient()
    lookup(client, item)

    response = client.get(f"{PDF_URL}?locale=vi&original=true")
    assert response.status_code == 404
    assert response.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    downloaded = content(client.get(f"{PDF_URL}?locale=vi"))
    assert hashlib.sha256(downloaded).hexdigest() == contract.pdf_vi_sha256
    assert hashlib.sha256(downloaded).hexdigest() != original_hash


@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.django_db
def test_guest_can_download_historical_version_but_not_other_request(
    request_factory, active_user, locale
):
    first = approve_request(request_factory(), active_user)
    first_contract = CSAContract.objects.get(purchase_request=first)
    first_version = first_contract.current_version
    revoke_membership(
        membership_id=first_contract.membership_id,
        reason="Cancelled",
        actor=active_user,
    )
    first_contract.refresh_from_db()
    second = approve_request(request_factory(phone="0987654321"), active_user)
    second_version_id = CSAContract.objects.get(purchase_request=second).current_version_id
    client = APIClient()
    assert lookup(client, first).status_code == 200

    detail = client.get(CONTRACT_URL).json()["data"]
    assert [version["version_number"] for version in detail["versions"]] == [1, 2]
    assert detail["versions"][1]["contract_status"] == "revoked"
    assert "storage_key" not in str(detail)
    assert "sha256" not in str(detail)
    old_pdf = client.get(PDF_URL, {"locale": locale, "version_id": str(first_version.pk)})
    assert old_pdf.status_code == 200
    assert hashlib.sha256(content(old_pdf)).hexdigest() == getattr(
        first_version, f"pdf_{locale}_sha256"
    )
    before = (
        CSAContract.objects.count(),
        CSAContractVersion.objects.count(),
        CSATrackerSession.objects.count(),
    )
    denied = client.get(PDF_URL, {"locale": locale, "version_id": str(second_version_id)})
    assert denied.status_code == 404
    assert denied.json()["errors"] == [{"code": "csa_contract_pdf_unavailable"}]
    for params in (
        {"locale": "vi", "version_id": "bad"},
        {"locale": "vi", "original": "true"},
        {"locale": "vi", "version_id": "00000000-0000-0000-0000-000000000000"},
    ):
        assert client.get(PDF_URL, params).status_code == 404
    assert (
        CSAContract.objects.count(),
        CSAContractVersion.objects.count(),
        CSATrackerSession.objects.count(),
    ) == before


@pytest.mark.django_db
def test_lookup_is_throttled_by_normalized_reference_and_phone(request_factory):
    item = request_factory()
    client = APIClient()
    phones = (
        "0912345678",
        "84912345678",
        "+84912345678",
        "0912345678",
        "84912345678",
        "+84912345678",
    )
    responses = [lookup(client, item, phone) for phone in phones]

    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]


@pytest.mark.django_db
def test_reject_confirmed_request_tracks_refund_and_mark_refunded_is_idempotent(
    request_factory, active_user, admin_api
):
    item = request_factory(status=CSAPurchaseRequestStatus.PAYMENT_CONFIRMED)
    reject = admin_api.post(
        f"/api/v1/admin/csa-purchase-requests/{item.pk}/reject",
        {"rejection_reason": "Payment must be returned"},
        format="json",
    )

    assert reject.status_code == 200
    item.refresh_from_db()
    assert item.refund_status == CSAPurchaseRefundStatus.PENDING
    assert item.refund_marked_at is None
    tracker_client = APIClient()
    tracked = lookup(tracker_client, item).json()["data"]
    assert tracked["status"] == CSAPurchaseRequestStatus.REJECTED
    assert tracked["refund_status"] == CSAPurchaseRefundStatus.PENDING
    assert tracked["contract"] is None and tracked["pdf_available"] is False
    assert "versions" not in tracked
    assert tracker_client.get(PDF_URL, {"locale": "vi"}).status_code == 404

    url = f"/api/v1/admin/csa-purchase-requests/{item.pk}/mark-refunded"
    first = admin_api.post(url, {"refund_note": "Bank transfer returned"}, format="json")
    second = admin_api.post(url, {"refund_note": "Must not overwrite"}, format="json")

    assert first.status_code == second.status_code == 200
    item.refresh_from_db()
    assert item.refund_status == CSAPurchaseRefundStatus.COMPLETED
    assert item.refund_marked_by == active_user
    assert item.refund_marked_at is not None
    assert item.refund_note == "Bank transfer returned"
    assert AuditLog.objects.filter(action="csa_purchase_request.refund_completed").count() == 1
    log = AuditLog.objects.get(action="csa_purchase_request.refund_completed")
    assert log.user == active_user
    assert log.context == {
        "request_id": str(item.pk),
        "refund_status": CSAPurchaseRefundStatus.COMPLETED,
    }
    completed = lookup(APIClient(), item).json()["data"]
    assert completed["refund_status"] == CSAPurchaseRefundStatus.COMPLETED


@pytest.mark.django_db
def test_refund_action_rejects_non_refundable_states(request_factory, admin_api):
    pending = request_factory()
    pending_response = admin_api.post(
        f"/api/v1/admin/csa-purchase-requests/{pending.pk}/mark-refunded",
        {},
        format="json",
    )
    assert pending_response.status_code == 409
    assert pending_response.json()["errors"] == [{"code": "csa_purchase_refund_state_conflict"}]

    reject_csa_purchase_request(request_id=pending.pk, reason="Cancelled", actor=None)
    pending.refresh_from_db()
    assert pending.refund_status == CSAPurchaseRefundStatus.NOT_APPLICABLE
    rejected_response = admin_api.post(
        f"/api/v1/admin/csa-purchase-requests/{pending.pk}/mark-refunded",
        {},
        format="json",
    )
    assert rejected_response.status_code == 409
    assert not AuditLog.objects.filter(action="csa_purchase_request.refund_completed").exists()


@pytest.mark.django_db
def test_database_rejects_refund_pending_for_non_rejected_request(request_factory):
    item = request_factory()

    with pytest.raises(IntegrityError), transaction.atomic():
        CSAPurchaseRequest.objects.filter(pk=item.pk).update(
            refund_status=CSAPurchaseRefundStatus.PENDING
        )


@pytest.mark.django_db
def test_tracker_reads_do_not_create_contract_membership_or_pdf(request_factory):
    item = request_factory()
    before = (Membership.objects.count(), CSAContract.objects.count())
    client = APIClient()

    assert lookup(client, item).status_code == 200
    assert client.get(CONTRACT_URL).status_code == 404

    assert (Membership.objects.count(), CSAContract.objects.count()) == before
