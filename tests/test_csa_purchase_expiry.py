import threading
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AuditLog
from memberships.expiry import (
    EXPIRED_REASON,
    MAX_EXPIRY_SECONDS,
    MIN_EXPIRY_SECONDS,
    expire_csa_purchase_requests,
    purchase_request_expiry_seconds,
)
from memberships.models import (
    CSAContract,
    CSACustomer,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    Membership,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    QuotaPolicy,
)
from memberships.purchases import (
    PurchaseRequestExpired,
    approve_csa_purchase_request,
    confirm_csa_purchase_payment,
    create_public_csa_purchase_request,
    mark_csa_purchase_request_refunded,
    reject_csa_purchase_request,
)
from memberships.terms import get_current_csa_terms
from memberships.views import admin_csa_purchase_payload

CREATE_URL = "/api/v1/public/csa-purchase-requests"
LOOKUP_URL = "/api/v1/public/csa-contract-tracker/lookup"
CONTRACT_URL = "/api/v1/public/csa-contract-tracker/contract"
PDF_URL = f"{CONTRACT_URL}/pdf"
TEST_BANK = {"bin": "970436", "name": "Vietcombank"}


@pytest.fixture(autouse=True)
def expiry_environment(monkeypatch, settings, tmp_path, default_supported_shipping_area):
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"


@pytest.fixture
def package_and_option():
    package = MembershipPackage.objects.create(
        name="Expiry package",
        description="Seasonal vegetables",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
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
        item, token = create_public_csa_purchase_request(
            package_id=package.pk,
            price_option_id=option.pk,
            name="Expiry customer",
            phone=phone,
            province_code="66",
            ward_code="22015",
            address="12 Orchard Road",
        )
        if status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED:
            item.status = status
            item.payment_confirmed_at = timezone.now()
            item.save(update_fields=["status", "payment_confirmed_at", "updated_at"])
        return item, token

    return create


def make_due(item, *, now=None):
    now = now or timezone.now()
    CSAPurchaseRequest.objects.filter(pk=item.pk).update(expires_at=now - timedelta(seconds=1))
    item.refresh_from_db()
    return now


@override_settings(CSA_PURCHASE_REQUEST_EXPIRY_SECONDS=3600)
@pytest.mark.django_db
def test_new_request_uses_server_time_for_expiry(request_factory):
    before = timezone.now()
    item, _token = request_factory()
    after = timezone.now()

    assert item.expires_at is not None
    assert before + timedelta(seconds=3600) <= item.expires_at
    assert item.expires_at <= after + timedelta(seconds=3600)
    assert item.expires_at - item.terms_accepted_at == timedelta(seconds=3600)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(0, MIN_EXPIRY_SECONDS), (10**9, MAX_EXPIRY_SECONDS)],
)
def test_runtime_expiry_setting_is_bounded(settings, configured, expected):
    settings.CSA_PURCHASE_REQUEST_EXPIRY_SECONDS = configured

    assert purchase_request_expiry_seconds() == expected


@override_settings(CSA_PURCHASE_REQUEST_EXPIRY_SECONDS=3600)
@pytest.mark.django_db
def test_public_create_rejects_client_expiry_and_returns_server_expiry(
    package_and_option, payment_settings
):
    package, option = package_and_option
    terms = get_current_csa_terms("vi")
    payload = {
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "name": "Expiry customer",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "12 Orchard Road",
        "terms_accepted": True,
        "terms_version": terms["version"],
        "terms_hash": terms["hash"],
    }
    rejected = APIClient().post(
        CREATE_URL,
        {**payload, "expires_at": "2099-01-01T00:00:00Z"},
        format="json",
    )
    response = APIClient().post(CREATE_URL, payload, format="json")

    assert rejected.status_code == 400
    assert response.status_code == 201
    item = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    assert response.json()["data"]["expires_at"] == item.expires_at.isoformat()
    assert item.expires_at.year != 2099


@pytest.mark.parametrize(
    "initial_status",
    [CSAPurchaseRequestStatus.PENDING, CSAPurchaseRequestStatus.PAYMENT_CONFIRMED],
)
@pytest.mark.django_db
def test_overdue_open_request_expires_once(initial_status, request_factory):
    item, _token = request_factory(status=initial_status)
    now = make_due(item)

    assert expire_csa_purchase_requests(now=now, batch_size=1) == 1
    assert expire_csa_purchase_requests(now=now, batch_size=1) == 0

    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.EXPIRED
    assert item.open_slot is None
    assert item.expired_at == now
    assert item.expired_reason == EXPIRED_REASON
    assert item.customer_id is None
    assert item.membership_id is None
    logs = AuditLog.objects.filter(action="csa_purchase_request.expired")
    assert logs.count() == 1
    assert logs.get().context == {
        "request_id": str(item.pk),
        "expired_at": now.isoformat(),
        "expired_reason": EXPIRED_REASON,
    }


@pytest.mark.django_db
def test_not_due_and_existing_terminal_requests_are_not_expired(request_factory):
    not_due, _token = request_factory(phone="0912345678")
    approved, _token = request_factory(phone="0922345678")
    rejected, _token = request_factory(phone="0932345678")
    approved.status = CSAPurchaseRequestStatus.APPROVED
    approved.save(update_fields=["status", "updated_at"])
    rejected.status = CSAPurchaseRequestStatus.REJECTED
    rejected.save(update_fields=["status", "updated_at"])
    CSAPurchaseRequest.objects.filter(pk__in=[approved.pk, rejected.pk]).update(
        expires_at=timezone.now() - timedelta(days=1)
    )

    assert expire_csa_purchase_requests() == 0
    assert CSAPurchaseRequest.objects.get(pk=not_due.pk).status == CSAPurchaseRequestStatus.PENDING
    assert (
        CSAPurchaseRequest.objects.get(pk=approved.pk).status == CSAPurchaseRequestStatus.APPROVED
    )
    assert (
        CSAPurchaseRequest.objects.get(pk=rejected.pk).status == CSAPurchaseRequestStatus.REJECTED
    )


@pytest.mark.django_db
def test_expired_request_cannot_confirm_approve_or_reject(request_factory, active_user):
    item, token = request_factory(status=CSAPurchaseRequestStatus.PAYMENT_CONFIRMED)
    now = make_due(item)
    assert expire_csa_purchase_requests(now=now) == 1

    with pytest.raises(PurchaseRequestExpired):
        confirm_csa_purchase_payment(
            request_id=item.pk,
            guest_confirmation_token=token,
        )
    with pytest.raises(PurchaseRequestExpired):
        approve_csa_purchase_request(
            request_id=item.pk,
            start_date=timezone.localdate(),
            actor=active_user,
        )
    with pytest.raises(PurchaseRequestExpired):
        reject_csa_purchase_request(
            request_id=item.pk,
            reason="Must remain expired",
            actor=active_user,
        )
    with pytest.raises(PurchaseRequestExpired):
        mark_csa_purchase_request_refunded(
            request_id=item.pk,
            actor=active_user,
            note="Must remain expired",
        )

    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.EXPIRED
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()


@pytest.mark.django_db
def test_confirm_endpoint_returns_stable_expired_error(request_factory):
    item, token = request_factory()
    make_due(item)

    response = APIClient().post(
        f"{CREATE_URL}/{item.pk}/confirm-payment",
        {"guest_confirmation_token": token},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_request_expired"}]


@pytest.mark.django_db
def test_new_request_with_same_phone_is_allowed_after_automatic_expiry(request_factory):
    previous, _token = request_factory()
    make_due(previous)

    current, _token = request_factory(phone="+84912345678")

    previous.refresh_from_db()
    assert previous.status == CSAPurchaseRequestStatus.EXPIRED
    assert previous.open_slot is None
    assert current.status == CSAPurchaseRequestStatus.PENDING
    assert current.open_slot is True
    assert AuditLog.objects.filter(action="csa_purchase_request.expired").count() == 1


@pytest.mark.django_db
def test_tracker_auto_expires_request_and_denies_contract_and_pdf(request_factory):
    item, _token = request_factory()
    make_due(item)
    client = APIClient()

    lookup = client.post(
        LOOKUP_URL,
        {"reference_code": item.request_code, "phone": "0912345678"},
        format="json",
    )

    assert lookup.status_code == 200
    data = lookup.json()["data"]
    assert data["reference_code"] == item.request_code
    assert data["status"] == CSAPurchaseRequestStatus.EXPIRED
    assert data["expires_at"] is not None
    assert data["contract"] is None
    assert data["pdf_available"] is False
    assert "membership_start_date" not in data
    assert client.get(CONTRACT_URL).status_code == 404
    assert client.get(PDF_URL).status_code == 404


@pytest.mark.django_db
def test_admin_payload_exposes_expiry_state_without_guest_secret(request_factory):
    item, token = request_factory()
    now = make_due(item)
    expire_csa_purchase_requests(now=now)
    item.refresh_from_db()

    data = admin_csa_purchase_payload(item)

    assert data["status"] == CSAPurchaseRequestStatus.EXPIRED
    assert data["expires_at"] == item.expires_at.isoformat()
    assert data["expired_at"] == now.isoformat()
    assert data["expired_reason"] == EXPIRED_REASON
    assert token not in str(data)
    assert item.guest_token_hash not in str(data)


@pytest.mark.django_db
def test_legacy_request_without_expiry_remains_readable_and_confirmable(request_factory):
    item, token = request_factory()
    CSAPurchaseRequest.objects.filter(pk=item.pk).update(expires_at=None)

    assert expire_csa_purchase_requests() == 0
    confirmed = confirm_csa_purchase_payment(
        request_id=item.pk,
        guest_confirmation_token=token,
    )

    assert confirmed.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert confirmed.expires_at is None


@pytest.mark.django_db
def test_command_batches_is_idempotent_and_does_not_output_sensitive_data(request_factory):
    items = [
        request_factory(phone=phone)[0] for phone in ("0912345678", "0922345678", "0932345678")
    ]
    for item in items:
        make_due(item)
    output = StringIO()

    call_command("expire_csa_purchase_requests", batch_size=1, stdout=output)

    assert output.getvalue().strip() == "Expired 3 CSA purchase request(s)."
    assert not any(item.phone_e164 in output.getvalue() for item in items)
    assert not any(str(item.pk) in output.getvalue() for item in items)
    second_output = StringIO()
    call_command("expire_csa_purchase_requests", batch_size=2, stdout=second_output)
    assert second_output.getvalue().strip() == "Expired 0 CSA purchase request(s)."
    assert AuditLog.objects.filter(action="csa_purchase_request.expired").count() == 3


@pytest.mark.django_db
def test_command_dry_run_does_not_change_requests(request_factory):
    item, _token = request_factory()
    make_due(item)
    output = StringIO()

    call_command("expire_csa_purchase_requests", dry_run=True, stdout=output)

    item.refresh_from_db()
    assert output.getvalue().strip() == "Would expire 1 CSA purchase request(s)."
    assert item.status == CSAPurchaseRequestStatus.PENDING
    assert not AuditLog.objects.filter(action="csa_purchase_request.expired").exists()


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_expire_and_confirm_have_one_terminal_winner(request_factory):
    item, token = request_factory()
    make_due(item)
    barrier = threading.Barrier(3)
    outcomes = []

    def expire_once():
        close_old_connections()
        try:
            barrier.wait()
            outcomes.append(f"expired:{expire_csa_purchase_requests(batch_size=1)}")
        finally:
            close_old_connections()

    def confirm_once():
        close_old_connections()
        try:
            barrier.wait()
            confirm_csa_purchase_payment(
                request_id=item.pk,
                guest_confirmation_token=token,
            )
            outcomes.append("confirmed")
        except PurchaseRequestExpired:
            outcomes.append("expired-error")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=expire_once), threading.Thread(target=confirm_once)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.EXPIRED
    assert "confirmed" not in outcomes
    assert AuditLog.objects.filter(action="csa_purchase_request.expired").count() == 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_expire_and_approve_create_no_domain_objects(request_factory, active_user):
    item, _token = request_factory(status=CSAPurchaseRequestStatus.PAYMENT_CONFIRMED)
    make_due(item)
    barrier = threading.Barrier(3)
    outcomes = []

    def expire_once():
        close_old_connections()
        try:
            barrier.wait()
            expire_csa_purchase_requests(batch_size=1)
            outcomes.append("expire-finished")
        finally:
            close_old_connections()

    def approve_once():
        close_old_connections()
        try:
            barrier.wait()
            approve_csa_purchase_request(
                request_id=item.pk,
                start_date=timezone.localdate(),
                actor=active_user,
            )
            outcomes.append("approved")
        except PurchaseRequestExpired:
            outcomes.append("expired-error")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=expire_once), threading.Thread(target=approve_once)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.EXPIRED
    assert "approved" not in outcomes
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
