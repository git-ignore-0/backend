import re
import threading
import uuid
from dataclasses import replace
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from identity.models import AccountStatus, User
from memberships.contracts import (
    CSAContractConflict,
    CSAContractPurchaseInvalid,
    CSAContractSnapshot,
    CSAContractSnapshotInvalid,
    build_manual_contract_snapshot,
    create_manual_contract,
    create_online_contract,
)
from memberships.models import (
    CSAContract,
    CSAContractSource,
    CSAContractStatus,
    CSAContractTermsAcceptanceMethod,
    CSACustomer,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    CSATermsClause,
    CSATermsDocument,
    Membership,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    MembershipStatus,
    QuotaPolicy,
)
from memberships.phones import normalize_vietnam_phone
from memberships.terms import publish_csa_terms


@pytest.fixture
def actor():
    return User.objects.create_user(
        email="contract-admin@example.com",
        name="Contract admin",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )


@pytest.fixture
def package_and_option():
    package = MembershipPackage.objects.create(
        name="Gói CSA hợp đồng",
        description="Rau theo mùa",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=500_000,
    )
    return package, option


def create_membership(package, option, *, phone="0912345678", reference=None):
    reference = reference or f"ORDER-{uuid.uuid4()}"
    customer = CSACustomer.objects.create(
        name="Nguyễn Văn An",
        phone_e164=normalize_vietnam_phone(phone),
        province_code="66",
        ward_code="22015",
        address="123 Đường Vườn Rau",
    )
    return Membership.objects.create(
        customer=customer,
        package=package,
        price_option=option,
        package_name_snapshot="Gói CSA hợp đồng",
        package_description_snapshot="Rau theo mùa",
        monthly_price_vnd_snapshot=500_000,
        total_price_vnd_snapshot=1_500_000,
        duration_months_snapshot=3,
        quota_policy_snapshot=QuotaPolicy.ROLLOVER,
        farmbrite_order_reference=reference,
        farmbrite_order_reference_key=reference.lower(),
        start_date=date(2026, 10, 1),
        end_date=date(2027, 1, 1),
        status=MembershipStatus.SCHEDULED,
        current_slot=True,
    )


def snapshot_for(
    membership,
    *,
    acceptance_method=CSAContractTermsAcceptanceMethod.IN_STORE,
):
    customer = membership.customer
    return CSAContractSnapshot(
        member_name=customer.name,
        phone_e164=customer.phone_e164,
        email=None,
        zalo_whatsapp=None,
        province_code=customer.province_code,
        ward_code=customer.ward_code,
        address=customer.address,
        package_name=membership.package_name_snapshot,
        price_option_name="3 months",
        duration_months=membership.duration_months_snapshot,
        amount=membership.total_price_vnd_snapshot,
        currency="VND",
        quota={"policy": "rollover", "items": [{"product": "Basket", "units": 2}]},
        terms_version="2026-09",
        terms_locale="vi",
        terms={
            "title": "Điều khoản CSA",
            "body": "Nội dung đã đồng ý",
            "payment": {"bank_bin": "970436", "account_number": "123456789"},
        },
        terms_accepted_at=timezone.now(),
        terms_acceptance_method=acceptance_method,
        terms_acceptance_ip="203.0.113.10",
        terms_acceptance_user_agent="Contract test",
        start_date=membership.start_date,
        end_date=membership.end_date,
    )


def approved_purchase_for(membership):
    request = CSAPurchaseRequest.objects.create(
        phone_e164=membership.customer.phone_e164,
        name_snapshot=membership.customer.name,
        province_code_snapshot=membership.customer.province_code,
        ward_code_snapshot=membership.customer.ward_code,
        address_snapshot=membership.customer.address,
        package=membership.package,
        price_option=membership.price_option,
        package_name_snapshot=membership.package_name_snapshot,
        price_option_name_snapshot="3 months",
        duration_months_snapshot=membership.duration_months_snapshot,
        amount_snapshot=membership.total_price_vnd_snapshot,
        currency_snapshot="VND",
        bank_bin_snapshot="970436",
        bank_name_snapshot="Vietcombank",
        account_number_snapshot="123456789",
        account_name_snapshot="NFV",
        transfer_content=f"CSA-{uuid.uuid4().hex[:12].upper()}",
        payment_config_version=1,
        status=CSAPurchaseRequestStatus.APPROVED,
        customer=membership.customer,
        membership=membership,
    )
    return request


@pytest.mark.django_db
def test_online_contract_uses_purchase_request_code(package_and_option):
    membership = create_membership(*package_and_option)
    purchase = approved_purchase_for(membership)

    contract = create_online_contract(
        membership_id=membership.pk,
        purchase_request_id=purchase.pk,
        snapshot=snapshot_for(
            membership, acceptance_method=CSAContractTermsAcceptanceMethod.ONLINE
        ),
    )

    assert isinstance(contract.pk, uuid.UUID)
    assert contract.reference_code == purchase.request_code
    assert contract.source == CSAContractSource.ONLINE
    assert contract.membership.customer == membership.customer
    assert contract.purchase_request == purchase


@pytest.mark.django_db
def test_manual_contract_uses_secure_nonsequential_reference(package_and_option):
    membership = create_membership(*package_and_option)

    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=snapshot_for(membership),
    )

    assert re.fullmatch(r"CSA-\d{6}-[A-HJ-NP-Z2-9]{6}", contract.reference_code)
    assert contract.source == CSAContractSource.ADMIN_MANUAL
    assert contract.purchase_request is None


@pytest.mark.django_db
def test_different_memberships_receive_distinct_contracts(package_and_option):
    first_membership = create_membership(*package_and_option, phone="0912345678")
    second_membership = create_membership(*package_and_option, phone="0987654321")

    first = create_manual_contract(
        membership_id=first_membership.pk,
        snapshot=snapshot_for(first_membership),
    )
    second = create_manual_contract(
        membership_id=second_membership.pk,
        snapshot=snapshot_for(second_membership),
    )

    assert first.reference_code != second.reference_code
    assert first.membership_id != second.membership_id


@pytest.mark.django_db
def test_manual_reference_collision_retries_with_a_new_secure_code(package_and_option, monkeypatch):
    first_membership = create_membership(*package_and_option, phone="0912345678")
    first = create_manual_contract(
        membership_id=first_membership.pk,
        snapshot=snapshot_for(first_membership),
    )
    second_membership = create_membership(*package_and_option, phone="0987654321")
    generated = iter([first.reference_code, "CSA-202609-ABCD23"])
    monkeypatch.setattr(
        "memberships.contracts.generate_manual_contract_reference",
        lambda **_kwargs: next(generated),
    )

    second = create_manual_contract(
        membership_id=second_membership.pk,
        snapshot=snapshot_for(second_membership),
    )

    assert second.reference_code == "CSA-202609-ABCD23"
    assert CSAContract._meta.get_field("reference_code").unique is True


@pytest.mark.django_db
def test_membership_cannot_have_two_contracts(package_and_option):
    membership = create_membership(*package_and_option)
    snapshot = snapshot_for(membership)
    create_manual_contract(membership_id=membership.pk, snapshot=snapshot)

    with pytest.raises(CSAContractConflict) as caught:
        create_manual_contract(membership_id=membership.pk, snapshot=snapshot)

    assert caught.value.code == "csa_contract_exists"
    assert CSAContract.objects.count() == 1


@pytest.mark.django_db
def test_purchase_request_cannot_have_two_contracts(package_and_option):
    first_membership = create_membership(*package_and_option, phone="0912345678")
    purchase = approved_purchase_for(first_membership)
    first_snapshot = snapshot_for(
        first_membership, acceptance_method=CSAContractTermsAcceptanceMethod.ONLINE
    )
    create_online_contract(
        membership_id=first_membership.pk,
        purchase_request_id=purchase.pk,
        snapshot=first_snapshot,
    )
    second_membership = create_membership(*package_and_option, phone="0987654321")

    purchase.membership = second_membership
    purchase.customer = second_membership.customer
    purchase.save(update_fields=["membership", "customer", "updated_at"])
    with pytest.raises(CSAContractConflict):
        create_online_contract(
            membership_id=second_membership.pk,
            purchase_request_id=purchase.pk,
            snapshot=replace(
                snapshot_for(
                    second_membership,
                    acceptance_method=CSAContractTermsAcceptanceMethod.ONLINE,
                ),
                province_code=purchase.province_code_snapshot,
                ward_code=purchase.ward_code_snapshot,
                address=purchase.address_snapshot,
            ),
        )

    assert CSAContract.objects.filter(purchase_request=purchase).count() == 1


@pytest.mark.django_db
def test_online_contract_requires_purchase_request(package_and_option):
    membership = create_membership(*package_and_option)
    values = CSAContract(
        reference_code="CSA-202609-ABC234",
        source=CSAContractSource.ONLINE,
        membership=membership,
        **contract_model_values(snapshot_for(membership)),
    )

    with pytest.raises(ValidationError, match="purchase request"):
        values.save()


@pytest.mark.django_db
def test_manual_contract_allows_no_purchase_request(package_and_option):
    membership = create_membership(*package_and_option)
    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=snapshot_for(membership),
    )
    assert contract.purchase_request_id is None


@pytest.mark.django_db
def test_manual_contract_uses_the_current_published_terms(package_and_option, actor):
    membership = create_membership(*package_and_option)
    draft = CSATermsDocument.objects.create(
        version="CSA_TERMS_CONTRACT_TEST",
        title_vi="Điều khoản hợp đồng mới",
        title_en="New contract terms",
    )
    CSATermsClause.objects.create(
        document=draft,
        key="delivery",
        sort_order=10,
        paragraphs_vi=["Nội dung mới."],
        paragraphs_en=["New content."],
    )
    publish_csa_terms(draft, actor=actor, expected_revision=1)
    payment_settings = MembershipPaymentSettings(
        bank_bin="970436",
        bank_name="Vietcombank",
        account_number="123456789",
        account_name="NFV",
    )

    snapshot = build_manual_contract_snapshot(
        customer=membership.customer,
        membership=membership,
        payment_settings=payment_settings,
    )

    assert snapshot.terms_version == "CSA_TERMS_CONTRACT_TEST"
    assert snapshot.terms_vi["title"] == "Điều khoản hợp đồng mới"
    assert snapshot.terms_en["title"] == "New contract terms"


@pytest.mark.django_db
def test_contract_snapshot_is_unchanged_after_live_data_changes(package_and_option):
    package, option = package_and_option
    membership = create_membership(package, option)
    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=snapshot_for(membership),
    )
    MembershipPaymentSettings.objects.bulk_create(
        [
            MembershipPaymentSettings(
                bank_bin="970436",
                bank_name="Vietcombank",
                account_number="123456789",
                account_name="NFV",
            )
        ]
    )

    package.name = "Tên package mới"
    package.save(update_fields=["name", "updated_at"])
    option.monthly_price_vnd = 900_000
    option.save(update_fields=["monthly_price_vnd", "updated_at"])
    membership.customer.name = "Tên khách mới"
    membership.customer.address = "Địa chỉ mới"
    membership.customer.save(update_fields=["name", "address", "updated_at"])
    MembershipPaymentSettings.objects.filter(pk="default").update(account_number="987654321")
    contract.refresh_from_db()

    assert contract.member_name_snapshot == "Nguyễn Văn An"
    assert contract.address_snapshot == "123 Đường Vườn Rau"
    assert contract.package_name_snapshot == "Gói CSA hợp đồng"
    assert contract.amount_snapshot == 1_500_000
    assert contract.terms_snapshot["body"] == "Nội dung đã đồng ý"
    assert contract.terms_snapshot["payment"]["account_number"] == "123456789"


@pytest.mark.django_db
def test_contract_snapshot_fields_are_immutable(package_and_option):
    membership = create_membership(*package_and_option)
    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=snapshot_for(membership),
    )

    contract.member_name_snapshot = "Tên bị sửa"
    with pytest.raises(ValidationError, match="immutable"):
        contract.save()


@pytest.mark.django_db
def test_contract_phone_is_normalized_and_invalid_phone_is_rejected(package_and_option):
    membership = create_membership(*package_and_option)
    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=replace(snapshot_for(membership), phone_e164="0912345678"),
    )
    assert contract.phone_e164_snapshot == "+84912345678"

    second_membership = create_membership(*package_and_option, phone="0987654321")
    with pytest.raises(CSAContractSnapshotInvalid):
        create_manual_contract(
            membership_id=second_membership.pk,
            snapshot=replace(snapshot_for(second_membership), phone_e164="not-a-phone"),
        )


@pytest.mark.django_db
def test_active_contract_rejects_revocation_data(package_and_option, actor):
    membership = create_membership(*package_and_option)
    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=snapshot_for(membership),
    )
    contract.revoked_at = timezone.now()
    contract.revoked_by = actor
    contract.revocation_reason = "Không tiếp tục"

    with pytest.raises(ValidationError, match="Active contracts"):
        contract.save()


@pytest.mark.django_db
def test_revoked_contract_requires_time_actor_and_reason(package_and_option, actor):
    membership = create_membership(*package_and_option)
    contract = create_manual_contract(
        membership_id=membership.pk,
        snapshot=snapshot_for(membership),
    )
    contract.status = CSAContractStatus.REVOKED

    with pytest.raises(ValidationError, match="Revoked contracts"):
        contract.save()

    contract.revoked_at = timezone.now()
    contract.revoked_by = actor
    contract.revocation_reason = "Khách hàng yêu cầu thu hồi"
    contract.save(
        update_fields=["status", "revoked_at", "revoked_by", "revocation_reason", "updated_at"]
    )
    contract.refresh_from_db()
    assert contract.status == CSAContractStatus.REVOKED


@pytest.mark.django_db
def test_rejected_purchase_request_cannot_create_contract(package_and_option):
    membership = create_membership(*package_and_option)
    purchase = approved_purchase_for(membership)
    purchase.status = CSAPurchaseRequestStatus.REJECTED
    purchase.membership = None
    purchase.save(update_fields=["status", "membership", "updated_at"])

    with pytest.raises(CSAContractPurchaseInvalid):
        create_online_contract(
            membership_id=membership.pk,
            purchase_request_id=purchase.pk,
            snapshot=snapshot_for(
                membership, acceptance_method=CSAContractTermsAcceptanceMethod.ONLINE
            ),
        )
    assert not CSAContract.objects.exists()


@pytest.mark.django_db
def test_creation_rolls_back_when_snapshot_validation_fails(package_and_option):
    membership = create_membership(*package_and_option)
    invalid = replace(snapshot_for(membership), terms={})

    with pytest.raises(CSAContractSnapshotInvalid):
        create_manual_contract(membership_id=membership.pk, snapshot=invalid)

    assert not CSAContract.objects.exists()


def test_contract_model_does_not_store_guest_token():
    field_names = {field.name for field in CSAContract._meta.get_fields()}
    assert "guest_token" not in field_names
    assert "guest_token_hash" not in field_names


def contract_model_values(snapshot):
    return {
        "status": CSAContractStatus.ACTIVE,
        "member_name_snapshot": snapshot.member_name,
        "phone_e164_snapshot": snapshot.phone_e164,
        "email_snapshot": snapshot.email,
        "zalo_whatsapp_snapshot": snapshot.zalo_whatsapp,
        "province_code_snapshot": snapshot.province_code,
        "ward_code_snapshot": snapshot.ward_code,
        "address_snapshot": snapshot.address,
        "package_name_snapshot": snapshot.package_name,
        "price_option_name_snapshot": snapshot.price_option_name,
        "duration_months_snapshot": snapshot.duration_months,
        "amount_snapshot": snapshot.amount,
        "currency_snapshot": snapshot.currency,
        "quota_snapshot": snapshot.quota,
        "terms_version": snapshot.terms_version,
        "terms_locale": snapshot.terms_locale,
        "terms_snapshot": snapshot.terms,
        "terms_accepted_at": snapshot.terms_accepted_at,
        "terms_acceptance_method": snapshot.terms_acceptance_method,
        "terms_acceptance_ip": snapshot.terms_acceptance_ip,
        "terms_acceptance_user_agent": snapshot.terms_acceptance_user_agent,
        "start_date": snapshot.start_date,
        "end_date": snapshot.end_date,
        "issued_at": timezone.now(),
    }


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_contract_creation_maps_duplicate_to_domain_error(package_and_option):
    membership = create_membership(*package_and_option)
    snapshot = snapshot_for(membership)
    barrier = threading.Barrier(3)
    outcomes = []

    def create_once():
        close_old_connections()
        try:
            barrier.wait()
            create_manual_contract(membership_id=membership.pk, snapshot=snapshot)
            outcomes.append("created")
        except CSAContractConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=create_once) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "created"]
    assert CSAContract.objects.filter(membership=membership).count() == 1


@pytest.mark.django_db(transaction=True)
def test_contract_migration_does_not_backfill_legacy_memberships():
    migrate_from = ("memberships", "0013_csapurchaserequest_guest_token_hash")
    migrate_to = ("memberships", "0014_csacontract")
    migrate_latest = (
        "memberships",
        "0018_remove_csapurchaserequest_memberships_csa_purchase_status_valid_and_more",
    )
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([migrate_from])
        old = executor.loader.project_state([migrate_from]).apps
        Package = old.get_model("memberships", "MembershipPackage")
        PriceOption = old.get_model("memberships", "MembershipPackagePriceOption")
        Customer = old.get_model("memberships", "CSACustomer")
        LegacyMembership = old.get_model("memberships", "Membership")
        package = Package.objects.create(name="Legacy package", quota_policy="expire")
        option = PriceOption.objects.create(
            package=package,
            duration_months=3,
            monthly_price_vnd=500_000,
            active_duration_key=3,
        )
        customer = Customer.objects.create(
            name="Legacy member",
            phone_e164="+84912345678",
            province_code="66",
            ward_code="22015",
            address="Legacy address",
        )
        LegacyMembership.objects.create(
            customer=customer,
            package=package,
            price_option=option,
            package_name_snapshot="Legacy package",
            monthly_price_vnd_snapshot=500_000,
            total_price_vnd_snapshot=1_500_000,
            duration_months_snapshot=3,
            quota_policy_snapshot="expire",
            farmbrite_order_reference="LEGACY-CONTRACT-1",
            farmbrite_order_reference_key="legacy-contract-1",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 4, 1),
            status="ended",
            current_slot=None,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([migrate_to])
        migrated = executor.loader.project_state([migrate_to]).apps
        assert migrated.get_model("memberships", "Membership").objects.count() == 1
        assert migrated.get_model("memberships", "CSAContract").objects.count() == 0
    finally:
        MigrationExecutor(connection).migrate([migrate_latest])
