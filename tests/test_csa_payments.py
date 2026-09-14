import copy
import hashlib
import threading
import uuid
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APIClient

import memberships.contract_versions as contract_versions
from identity.models import AuditLog
from memberships.contract_access import contract_payload
from memberships.contract_pdfs import CSAContractPDFStorageError, get_contract_pdf_storage
from memberships.models import (
    CSAContract,
    CSAContractVersion,
    CSAContractVersionReason,
    CSACustomer,
    CSAPurchasePayment,
    CSAPurchasePaymentStatus,
    CSAPurchaseRequestStatus,
    Membership,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    MembershipPaymentType,
    Product,
    QuotaPolicy,
)
from memberships.payments import (
    CSAPaymentAmountMismatch,
    CSAPaymentStateConflict,
    confirm_csa_payment,
    ensure_csa_payment_schedule,
    mark_overdue_csa_payments,
    payment_due_at,
    reject_csa_payment,
)
from memberships.purchases import (
    approve_csa_purchase_request,
    confirm_csa_purchase_payment,
    create_public_csa_purchase_request,
    update_membership_payment_settings,
)
from memberships.services import (
    MembershipNotRevocable,
    add_months,
    assign_membership_with_contract,
    ensure_due_cycles,
    revoke_membership,
)
from memberships.tracker import tracker_contract_payload

BANK = {"bin": "970436", "name": "Vietcombank"}


@pytest.fixture(autouse=True)
def payment_test_environment(monkeypatch, settings, tmp_path):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [BANK])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"
    update_membership_payment_settings(
        bank_bin=BANK["bin"],
        bank_name=BANK["name"],
        account_number="123456789",
        account_name="NFV",
        direct_transfer_enabled=True,
    )
    yield
    cache.clear()


@pytest.fixture
def catalog():
    product = Product.objects.create(name="Vegetables", unit_size=Decimal("1"))
    package = MembershipPackage.objects.create(name="Six months", quota_policy=QuotaPolicy.EXPIRE)
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=10)
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=6, monthly_price_vnd=1_000_000
    )
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Two installments",
        payment_type=MembershipPaymentType.INSTALLMENT,
        total_amount=Decimal("6000001"),
        installment_count=2,
        is_active=True,
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=plan, sequence=1, amount=Decimal("3000000"), cycle_count=3
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=plan, sequence=2, amount=Decimal("3000001"), cycle_count=3
    )
    return package, option, plan


@pytest.fixture
def request_and_token(catalog):
    package, option, plan = catalog
    return create_public_csa_purchase_request(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=plan.pk,
        phone="0912345678",
        name="Buyer",
        province_code="66",
        ward_code="22015",
        address="12 Main Street",
    )


@pytest.fixture
def reported_request(request_and_token):
    item, token = request_and_token
    confirm_csa_purchase_payment(request_id=item.pk, guest_confirmation_token=token)
    item.refresh_from_db()
    return item


@pytest.fixture
def approved_request(reported_request, active_user):
    item, membership = approve_csa_purchase_request(
        request_id=reported_request.pk, start_date=timezone.localdate(), actor=active_user
    )
    return item, membership


@pytest.mark.django_db
def test_schedule_uses_snapshot_and_is_idempotent(request_and_token, catalog):
    item, _token = request_and_token
    _package, _option, plan = catalog
    payments = ensure_csa_payment_schedule(request_id=item.pk)
    assert [(p.installment_sequence, p.amount_due, p.cycle_count) for p in payments] == [
        (1, Decimal("3000000"), 3),
        (2, Decimal("3000001"), 3),
    ]
    assert CSAPurchasePayment.objects.filter(purchase_request=item).count() == 2
    plan.total_amount = Decimal("7000000")
    plan.is_active = False
    plan.save(update_fields=["total_amount", "is_active"])
    plan.installments.filter(sequence=2).update(amount=Decimal("4000000"))
    assert [p.pk for p in ensure_csa_payment_schedule(request_id=item.pk)] == [
        p.pk for p in payments
    ]


@pytest.mark.django_db
def test_report_only_marks_first_payment_and_creates_no_membership(reported_request):
    assert reported_request.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert [p.status for p in reported_request.payments.all()] == [
        CSAPurchasePaymentStatus.REPORTED,
        CSAPurchasePaymentStatus.PENDING,
    ]
    assert not Membership.objects.exists()
    assert not CSACustomer.objects.exists()


@pytest.mark.django_db
def test_legacy_request_materializes_one_payment_only_when_confirmed(catalog, active_user):
    package, option, plan = catalog
    plan.is_active = False
    plan.save(update_fields=["is_active"])
    item, token = create_public_csa_purchase_request(
        package_id=package.pk,
        price_option_id=option.pk,
        phone="0912345678",
        name="Buyer",
        province_code="66",
        ward_code="22015",
        address="12 Main Street",
    )
    assert not item.payments.exists()
    confirm_csa_purchase_payment(request_id=item.pk, guest_confirmation_token=token)
    assert item.payments.count() == 1
    approved, membership = approve_csa_purchase_request(
        request_id=item.pk, start_date=timezone.localdate(), actor=active_user
    )
    payment = approved.payments.get()
    assert payment.status == CSAPurchasePaymentStatus.CONFIRMED
    assert payment.cycle_count == 6
    assert payment.amount_paid == Decimal("6000000")
    assert membership.cycles.count() == 1


@pytest.mark.django_db
def test_full_plan_approval_confirms_single_payment(catalog, active_user):
    package, option, _installment_plan = catalog
    full_plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Full payment",
        payment_type=MembershipPaymentType.FULL,
        total_amount=Decimal("5700000"),
        installment_count=1,
        is_active=True,
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=full_plan,
        sequence=1,
        amount=Decimal("5700000"),
        cycle_count=6,
    )
    item, token = create_public_csa_purchase_request(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=full_plan.pk,
        phone="0912345678",
        name="Buyer",
        province_code="66",
        ward_code="22015",
        address="12 Main Street",
    )
    confirm_csa_purchase_payment(request_id=item.pk, guest_confirmation_token=token)
    approved, membership = approve_csa_purchase_request(
        request_id=item.pk, start_date=timezone.localdate(), actor=active_user
    )
    assert approved.payments.count() == 1
    assert approved.payments.get().status == CSAPurchasePaymentStatus.CONFIRMED
    assert membership.total_price_vnd_snapshot == 5_700_000
    assert membership.pricing_basis == "payment_plan"
    ensure_due_cycles(membership, as_of=add_months(membership.start_date, 5))
    assert membership.cycles.count() == 6


@pytest.mark.django_db
def test_approval_confirms_first_payment_and_caps_quota(approved_request, active_user):
    item, membership = approved_request
    assert membership.duration_months_snapshot == 6
    assert membership.total_price_vnd_snapshot == 6_000_001
    assert membership.pricing_basis == "payment_plan"
    assert CSAContract.objects.filter(purchase_request=item).count() == 1
    payments = list(item.payments.all())
    assert payments[0].status == CSAPurchasePaymentStatus.CONFIRMED
    assert payments[0].amount_paid == Decimal("3000000")
    assert payments[0].confirmed_by_id == active_user.pk
    assert payments[1].status == CSAPurchasePaymentStatus.PENDING
    assert payments[1].due_at == payment_due_at(membership.start_date, 3)
    ensure_due_cycles(membership, as_of=add_months(membership.start_date, 5))
    assert list(membership.cycles.values_list("sequence", flat=True)) == [1, 2, 3]

    same_item, same_membership = approve_csa_purchase_request(
        request_id=item.pk, start_date=membership.start_date, actor=active_user
    )
    assert same_item.pk == item.pk and same_membership.pk == membership.pk
    assert item.payments.count() == 2
    assert Membership.objects.count() == 1
    assert AuditLog.objects.filter(action="csa_payment.confirmed").count() == 1


@pytest.mark.django_db
def test_contract_user_and_guest_schedule_is_snapshot_based_and_safe(approved_request):
    item, membership = approved_request
    contract = (
        CSAContract.objects.select_related(
            "membership__customer", "purchase_request", "current_version"
        )
        .prefetch_related("versions", "purchase_request__payments")
        .get(membership=membership)
    )
    before = (CSAContractVersion.objects.count(), CSAPurchasePayment.objects.count())
    for data in (contract_payload(contract, admin=False), tracker_contract_payload(contract)):
        assert data["payment_plan"]["payment_type"] == "installment"
        assert data["payment_summary"] == {
            "total_amount": "6000001",
            "paid_amount": "3000000",
            "remaining_amount": "3000001",
            "confirmed_installments": 1,
            "installment_count": 2,
            "paid_cycles": 3,
            "total_cycles": 6,
        }
        assert [row["installment_sequence"] for row in data["payments"]] == [1, 2]
        assert [row["status"] for row in data["payments"]] == ["confirmed", "pending"]
        assert all("confirmed_by" not in row and "note" not in row for row in data["payments"])
        assert "storage_key" not in str(data["payments"])
    assert (CSAContractVersion.objects.count(), CSAPurchasePayment.objects.count()) == before


@pytest.mark.django_db
def test_issued_and_later_payment_versions_preserve_original_pdfs(approved_request, active_user):
    item, membership = approved_request
    contract = CSAContract.objects.get(membership=membership)
    first = contract.current_version
    assert first.version_number == 1
    assert first.version_reason == CSAContractVersionReason.ISSUED
    assert first.membership_status in {"scheduled", "active"}
    assert (first.paid_amount, first.remaining_amount, first.paid_cycles, first.total_cycles) == (
        Decimal("3000000"),
        Decimal("3000001"),
        3,
        6,
    )
    assert [row["status"] for row in first.payment_schedule_snapshot] == ["confirmed", "pending"]
    storage = get_contract_pdf_storage()
    with storage.open(first.pdf_vi_storage_key, "rb") as source:
        original_bytes = source.read()
    assert first.pdf_vi_sha256 == hashlib.sha256(original_bytes).hexdigest()
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(original_bytes)).pages)
    assert "Phiên bản PDF 1" in text
    assert "3.000.000" in text and "3.000.001" in text
    assert "Trả góp 2 lần" in text
    assert "Lần" in text and "1" in text and "2" in text
    assert "Tổng giá trị" in text and "Còn phải thanh toán" in text
    assert "Tùy cấu hình" in text

    confirm_csa_payment(
        request_id=item.pk,
        installment_sequence=2,
        amount_paid=Decimal("3000001"),
        actor=active_user,
    )
    contract.refresh_from_db()
    second = contract.current_version
    first.refresh_from_db()
    assert second.version_number == 2
    assert second.version_reason == CSAContractVersionReason.PAYMENT_CONFIRMED
    assert second.membership_status in {"scheduled", "active"}
    assert second.paid_amount == Decimal("6000001")
    assert second.remaining_amount == 0
    assert second.paid_cycles == 6
    assert first.paid_amount == Decimal("3000000")
    assert first.pdf_vi_storage_key != second.pdf_vi_storage_key
    assert contract.original_pdf_vi_storage_key == first.pdf_vi_storage_key
    with storage.open(first.pdf_vi_storage_key, "rb") as source:
        assert source.read() == original_bytes
    with storage.open(second.pdf_vi_storage_key, "rb") as source:
        current_pages = PdfReader(BytesIO(source.read())).pages
        current_text = "\n".join(page.extract_text() for page in current_pages)
    assert "Phiên bản PDF 2" in current_text
    assert "6.000.001" in current_text
    assert "Còn phải thanh toán" in current_text
    assert contract.versions.count() == 2


@pytest.mark.django_db
def test_version_ignores_changed_live_payment_plan(approved_request, catalog, active_user):
    item, membership = approved_request
    _package, _option, plan = catalog
    plan.installments.filter(sequence=2).update(amount=Decimal("9999999"), cycle_count=1)
    plan.is_active = False
    plan.save(update_fields=["is_active"])
    confirm_csa_payment(
        request_id=item.pk,
        installment_sequence=2,
        amount_paid=Decimal("3000001"),
        actor=active_user,
    )
    contract = CSAContract.objects.get(membership=membership)
    first, second = list(contract.versions.all())
    assert first.payment_schedule_snapshot[1]["amount_due"] == "3000001"
    assert first.payment_schedule_snapshot[1]["status"] == "pending"
    assert second.payment_schedule_snapshot[1]["amount_due"] == "3000001"
    assert second.payment_schedule_snapshot[1]["cycle_count"] == 3
    assert second.payment_schedule_snapshot[1]["status"] == "confirmed"


@pytest.mark.django_db
def test_admin_contract_detail_exposes_version_history_without_file_keys(
    approved_request, active_user, access_token
):
    item, membership = approved_request
    confirm_csa_payment(
        request_id=item.pk,
        installment_sequence=2,
        amount_paid=Decimal("3000001"),
        actor=active_user,
    )
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = admin.get(f"/api/v1/admin/memberships/{membership.pk}/contract")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["current_version"]["version_number"] == 2
    assert data["current_version"]["paid_amount"] == "6000001"
    assert data["current_version"]["remaining_amount"] == "0"
    assert [version["version_reason"] for version in data["versions"]] == [
        "issued",
        "payment_confirmed",
    ]
    assert "storage_key" not in str(data)
    assert "guest_token" not in str(data)


@pytest.mark.django_db
def test_revoke_publishes_immutable_version_without_erasing_original(approved_request, active_user):
    _item, membership = approved_request
    contract = CSAContract.objects.get(membership=membership)
    original_key = contract.current_version.pdf_vi_storage_key
    revoke_membership(membership_id=membership.pk, reason="Customer requested", actor=active_user)
    contract.refresh_from_db()
    revoked = contract.current_version
    assert revoked.version_number == 2
    assert revoked.version_reason == CSAContractVersionReason.MEMBERSHIP_REVOKED
    assert revoked.contract_status == "revoked"
    assert revoked.membership_status == "revoked"
    assert revoked.revocation_reason == "Customer requested"
    assert revoked.revoked_by_name_snapshot == active_user.name
    assert contract.original_pdf_vi_storage_key == original_key
    with get_contract_pdf_storage().open(revoked.pdf_vi_storage_key, "rb") as source:
        text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(source.read())).pages)
    assert "ĐÃ THU HỒI" in text
    assert "Customer requested" in text
    assert active_user.name in text
    assert "Phiên bản PDF 2" in text
    with pytest.raises(MembershipNotRevocable):
        revoke_membership(membership_id=membership.pk, reason="Replay", actor=active_user)
    assert contract.versions.count() == 2


@pytest.mark.django_db
def test_failed_later_pdf_rolls_back_payment_quota_audit_and_version(
    approved_request, active_user, monkeypatch
):
    item, membership = approved_request
    contract = CSAContract.objects.get(membership=membership)
    first_key = contract.current_version.pdf_vi_storage_key
    original_renderer = contract_versions.render_contract_pdf

    def fail_english(contract, locale, *, pdf_version, version):
        if locale == "en":
            raise CSAContractPDFStorageError("PDF unavailable")
        return original_renderer(contract, locale, pdf_version=pdf_version, version=version)

    monkeypatch.setattr("memberships.contract_versions.render_contract_pdf", fail_english)
    with pytest.raises(CSAContractPDFStorageError):
        confirm_csa_payment(
            request_id=item.pk,
            installment_sequence=2,
            amount_paid=Decimal("3000001"),
            actor=active_user,
        )
    contract.refresh_from_db()
    assert contract.current_version.version_number == 1
    assert contract.current_version.pdf_vi_storage_key == first_key
    assert contract.versions.count() == 1
    assert item.payments.get(installment_sequence=2).status == "pending"
    assert membership.cycles.count() == 1
    assert AuditLog.objects.filter(action="csa_payment.confirmed").count() == 1


@pytest.mark.django_db
def test_failed_revoked_pdf_rolls_back_membership_contract_and_version(
    approved_request, active_user, monkeypatch
):
    _item, membership = approved_request
    contract = CSAContract.objects.get(membership=membership)
    original_key = contract.current_version.pdf_vi_storage_key

    def fail(*_args, **_kwargs):
        raise CSAContractPDFStorageError("PDF unavailable")

    monkeypatch.setattr("memberships.contract_versions.render_contract_pdf", fail)
    with pytest.raises(CSAContractPDFStorageError):
        revoke_membership(
            membership_id=membership.pk, reason="Customer requested", actor=active_user
        )
    membership.refresh_from_db()
    contract.refresh_from_db()
    assert membership.status != "revoked"
    assert contract.status == "active"
    assert contract.current_version.version_number == 1
    assert contract.versions.count() == 1
    assert get_contract_pdf_storage().exists(original_key)


@pytest.mark.django_db
def test_second_locale_storage_failure_removes_first_locale_orphan(
    approved_request, active_user, monkeypatch
):
    item, membership = approved_request
    storage = get_contract_pdf_storage()
    original_save = storage.save
    first_key = []

    def fail_english(name, content, *args, **kwargs):
        if name.endswith(".en.pdf"):
            raise OSError("Storage unavailable")
        first_key.append(name)
        return original_save(name, content, *args, **kwargs)

    monkeypatch.setattr(storage, "save", fail_english)
    monkeypatch.setattr("memberships.contract_pdfs.get_contract_pdf_storage", lambda: storage)
    with pytest.raises(CSAContractPDFStorageError):
        confirm_csa_payment(
            request_id=item.pk,
            installment_sequence=2,
            amount_paid=Decimal("3000001"),
            actor=active_user,
        )
    assert len(first_key) == 1
    assert not storage.exists(first_key[0])
    assert item.payments.get(installment_sequence=2).status == "pending"
    assert CSAContract.objects.get(membership=membership).versions.count() == 1


@pytest.mark.django_db
def test_published_version_cannot_be_updated_or_deleted(approved_request):
    _item, membership = approved_request
    version = CSAContract.objects.get(membership=membership).current_version
    version.paid_amount = Decimal("1")
    with pytest.raises(ValidationError):
        version.save()
    with pytest.raises(ValidationError):
        CSAContractVersion.objects.filter(pk=version.pk).update(paid_amount=Decimal("1"))
    with pytest.raises(ValidationError):
        CSAContractVersion.objects.filter(pk=version.pk).delete()
    with pytest.raises(ValidationError):
        version.delete()


@pytest.mark.django_db
def test_contract_version_database_rejects_duplicate_and_nonpositive_number(approved_request):
    _item, membership = approved_request
    published = CSAContract.objects.get(membership=membership).current_version
    for number in (1, 0):
        duplicate = copy.copy(published)
        duplicate.pk = uuid.uuid4()
        duplicate.version_number = number
        with pytest.raises(IntegrityError), transaction.atomic():
            CSAContractVersion.objects.bulk_create([duplicate])


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    connection.vendor == "sqlite", reason="SQLite lacks row-level select_for_update"
)
def test_concurrent_confirmation_creates_only_one_new_version(approved_request, active_user):
    item, membership = approved_request
    barrier = threading.Barrier(3)
    results = []
    errors = []

    def confirm_once():
        close_old_connections()
        try:
            barrier.wait()
            payment = confirm_csa_payment(
                request_id=item.pk,
                installment_sequence=2,
                amount_paid=Decimal("3000001"),
                actor=active_user,
            )
            results.append(payment.pk)
        except Exception as error:
            errors.append(error)
        finally:
            close_old_connections()

    workers = [threading.Thread(target=confirm_once) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join()
    assert not errors
    assert len(results) == 2 and results[0] == results[1]
    assert CSAContract.objects.get(membership=membership).versions.count() == 2
    assert AuditLog.objects.filter(action="csa_payment.confirmed").count() == 2


@pytest.mark.django_db
def test_later_confirmation_requires_exact_amount_and_only_opens_once(
    approved_request, active_user
):
    item, membership = approved_request
    for amount in (Decimal("3000000"), Decimal("3000002")):
        with pytest.raises(CSAPaymentAmountMismatch):
            confirm_csa_payment(
                request_id=item.pk, installment_sequence=2, amount_paid=amount, actor=active_user
            )
    payment = confirm_csa_payment(
        request_id=item.pk,
        installment_sequence=2,
        amount_paid=Decimal("3000001"),
        actor=active_user,
        note="Bank checked",
    )
    assert payment.status == CSAPurchasePaymentStatus.CONFIRMED
    assert payment.note == "Bank checked"
    assert (
        confirm_csa_payment(
            request_id=item.pk,
            installment_sequence=2,
            amount_paid=Decimal("3000001"),
            actor=active_user,
        ).pk
        == payment.pk
    )
    ensure_due_cycles(membership, as_of=add_months(membership.start_date, 5))
    assert list(membership.cycles.values_list("sequence", flat=True)) == [1, 2, 3, 4, 5, 6]
    assert AuditLog.objects.filter(action="csa_payment.confirmed").count() == 2


@pytest.mark.django_db
def test_overdue_is_idempotent_and_does_not_revoke_or_open_quota(approved_request):
    item, membership = approved_request
    second = item.payments.get(installment_sequence=2)
    assert mark_overdue_csa_payments(now=second.due_at + timedelta(seconds=1)) == 1
    assert mark_overdue_csa_payments(now=second.due_at + timedelta(seconds=1)) == 0
    second.refresh_from_db()
    membership.refresh_from_db()
    assert second.status == CSAPurchasePaymentStatus.OVERDUE
    assert (
        AuditLog.objects.filter(
            action="csa_payment.overdue", context__payment_id=str(second.pk)
        ).count()
        == 1
    )
    assert membership.status != "revoked"
    ensure_due_cycles(membership, as_of=add_months(membership.start_date, 5))
    assert membership.cycles.count() == 3


@pytest.mark.django_db
def test_reject_payment_requires_reason_and_cannot_reverse_confirmed(approved_request, active_user):
    item, _membership = approved_request
    with pytest.raises(CSAPaymentStateConflict):
        reject_csa_payment(request_id=item.pk, installment_sequence=2, reason="", actor=active_user)
    rejected = reject_csa_payment(
        request_id=item.pk, installment_sequence=2, reason="Unmatched transfer", actor=active_user
    )
    assert rejected.status == CSAPurchasePaymentStatus.REJECTED
    assert (
        reject_csa_payment(
            request_id=item.pk,
            installment_sequence=2,
            reason="Unmatched transfer",
            actor=active_user,
        ).pk
        == rejected.pk
    )
    with pytest.raises(CSAPaymentStateConflict):
        confirm_csa_payment(
            request_id=item.pk,
            installment_sequence=2,
            amount_paid=Decimal("3000001"),
            actor=active_user,
        )
    assert AuditLog.objects.filter(action="csa_payment.rejected").count() == 1


@pytest.mark.django_db
def test_approval_rolls_back_payment_membership_and_audit_on_pdf_failure(
    reported_request, active_user, monkeypatch
):
    def fail(*_args, **_kwargs):
        raise CSAContractPDFStorageError("Storage failed")

    monkeypatch.setattr("memberships.purchases.issue_contract_version", fail)
    with pytest.raises(CSAContractPDFStorageError):
        approve_csa_purchase_request(
            request_id=reported_request.pk,
            start_date=timezone.localdate(),
            actor=active_user,
        )
    reported_request.refresh_from_db()
    assert reported_request.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert (
        reported_request.payments.get(installment_sequence=1).status
        == CSAPurchasePaymentStatus.REPORTED
    )
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not AuditLog.objects.filter(action="csa_payment.confirmed").exists()


@pytest.mark.django_db
def test_manual_assignment_gets_one_confirmed_payment(catalog, active_user):
    package, option, _plan = catalog
    from memberships.services import create_managed_csa_customer

    customer = create_managed_csa_customer(
        name="Walk-in",
        phone="0901234567",
        province_code="66",
        ward_code="22015",
        address="Store",
    )
    membership = assign_membership_with_contract(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate(),
        farmbrite_order_reference="ORDER-123",
        actor=active_user,
    )
    payment = membership.manual_payments.get()
    assert payment.purchase_request_id is None
    assert payment.status == CSAPurchasePaymentStatus.CONFIRMED
    assert payment.amount_paid == payment.amount_due == Decimal("6000000")
    assert payment.cycle_count == 6
    contract = CSAContract.objects.get(membership=membership)
    version = contract.current_version
    assert version.version_number == 1
    assert version.payment_type == MembershipPaymentType.FULL
    assert version.paid_amount == Decimal("6000000")
    assert version.remaining_amount == 0
    assert version.paid_cycles == 6


@pytest.mark.django_db
def test_model_rejects_invalid_state_and_duplicate_sequence(request_and_token, active_user):
    item, _token = request_and_token
    first = item.payments.get(installment_sequence=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        CSAPurchasePayment.objects.create(
            purchase_request=item,
            installment_sequence=3,
            amount_due=Decimal("1"),
            cycle_count=1,
            status=CSAPurchasePaymentStatus.CONFIRMED,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        CSAPurchasePayment.objects.bulk_create(
            [
                CSAPurchasePayment(
                    purchase_request=item,
                    installment_sequence=1,
                    amount_due=Decimal("1"),
                    cycle_count=1,
                )
            ]
        )
    first.status = CSAPurchasePaymentStatus.CONFIRMED
    first.amount_paid = first.amount_due
    first.confirmed_at = timezone.now()
    first.confirmed_by = active_user
    first.save()
    first.status = CSAPurchasePaymentStatus.PENDING
    with pytest.raises(ValidationError):
        first.save()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "changes",
    [
        {"installment_sequence": 0},
        {"amount_due": Decimal("0")},
        {"cycle_count": 0},
        {"currency": "USD"},
        {"status": CSAPurchasePaymentStatus.REJECTED},
        {"status": CSAPurchasePaymentStatus.CONFIRMED},
    ],
)
def test_payment_database_constraints_reject_invalid_rows(request_and_token, changes):
    item, _token = request_and_token
    payment = CSAPurchasePayment(
        purchase_request=item,
        installment_sequence=3,
        amount_due=Decimal("1"),
        cycle_count=1,
    )
    for field, value in changes.items():
        setattr(payment, field, value)
    with pytest.raises(IntegrityError), transaction.atomic():
        CSAPurchasePayment.objects.bulk_create([payment])


@pytest.mark.django_db
def test_admin_endpoint_permission_payload_and_public_amount_guard(
    approved_request, active_user, access_token
):
    item, _membership = approved_request
    path = f"/api/v1/admin/csa-purchase-requests/{item.pk}/payments/2/confirm"
    assert APIClient().post(path, {"amount_paid": "3000001"}, format="json").status_code in (
        401,
        403,
    )
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    wrong = admin.post(path, {"amount_paid": "1"}, format="json")
    assert wrong.status_code == 400
    assert wrong.json()["errors"] == [{"code": "csa_payment_amount_mismatch"}]
    response = admin.post(path, {"amount_paid": "3000001", "note": "Checked"}, format="json")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["payment_summary"]["paid_amount"] == "6000001"
    assert data["payment_summary"]["paid_cycles"] == 6
    assert data["payments"][1]["confirmed_by"]["id"] == str(active_user.pk)
    assert "guest_token_hash" not in data
    assert "terms_acceptance_ip" not in data
    assert "terms_acceptance_user_agent" not in data
    assert admin.get(path).status_code == 405

    # The public confirmation endpoint does not accept a client-chosen paid amount.
    public_path = f"/api/v1/public/csa-purchase-requests/{item.pk}/confirm-payment"
    public = APIClient().post(public_path, {"amount_paid": "3000001"}, format="json")
    assert public.status_code == 400
    assert item.payments.filter(status=CSAPurchasePaymentStatus.CONFIRMED).count() == 2


@pytest.mark.django_db
def test_admin_reject_endpoint_requires_reason_and_cannot_reject_confirmed(
    approved_request, active_user, access_token
):
    item, _membership = approved_request
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    path = f"/api/v1/admin/csa-purchase-requests/{item.pk}/payments/2/reject"
    assert admin.post(path, {"rejection_reason": ""}, format="json").status_code == 400
    response = admin.post(path, {"rejection_reason": "No matching payment"}, format="json")
    assert response.status_code == 200
    assert response.json()["data"]["payments"][1]["status"] == "rejected"
    assert response.json()["data"]["payments"][1]["rejection_reason"] == "No matching payment"
    assert (
        admin.post(path, {"rejection_reason": "No matching payment"}, format="json").status_code
        == 200
    )
    assert AuditLog.objects.filter(action="csa_payment.rejected").count() == 1
    assert admin.get(path).status_code == 405
