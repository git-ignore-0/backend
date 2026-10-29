import uuid

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from memberships.models import (
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
    PurchasePackageUnavailable,
    PurchaseRequestConflict,
    create_csa_purchase_request,
    update_membership_payment_settings,
)

TEST_BANK = {
    "bin": "970436",
    "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
    "short_name": "Vietcombank",
    "code": "VCB",
}


@pytest.fixture(autouse=True)
def vietqr_bank_directory(monkeypatch, default_supported_shipping_area):
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])


@pytest.fixture
def package_and_option():
    package = MembershipPackage.objects.create(
        name="Gói CSA 6 tháng",
        description="Rau theo mùa",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=6,
        monthly_price_vnd=500_000,
    )
    return package, option


@pytest.fixture
def payment_settings():
    return update_membership_payment_settings(
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
        direct_transfer_enabled=True,
    )


def create_request(package, option, *, phone="0912345678"):
    return create_csa_purchase_request(
        package_id=package.pk,
        price_option_id=option.pk,
        phone=phone,
        name="Nguyễn Văn An",
        province_code="66",
        ward_code="22015",
        address="123 Đường Vườn Rau",
    )


@pytest.mark.django_db
def test_create_purchase_request_normalizes_phone_and_captures_required_snapshots(
    package_and_option, payment_settings
):
    package, option = package_and_option
    customer_count = CSACustomer.objects.count()
    membership_count = Membership.objects.count()

    purchase = create_request(package, option)

    assert isinstance(purchase.pk, uuid.UUID)
    assert purchase.request_code.startswith("CSA-")
    assert purchase.transfer_content == purchase.request_code
    assert purchase.phone_e164 == "+84912345678"
    assert purchase.package_name_snapshot == "Gói CSA 6 tháng"
    assert purchase.price_option_name_snapshot == "6 months"
    assert purchase.duration_months_snapshot == 6
    assert purchase.amount_snapshot == 3_000_000
    assert purchase.currency_snapshot == "VND"
    assert purchase.bank_bin_snapshot == TEST_BANK["bin"]
    assert purchase.bank_name_snapshot == TEST_BANK["name"]
    assert purchase.account_number_snapshot == payment_settings.account_number
    assert purchase.account_name_snapshot == payment_settings.account_name
    assert purchase.payment_config_version == payment_settings.version
    assert purchase.terms_version == "CSA_TERMS_V2"
    assert purchase.terms_locale == "vi"
    assert purchase.terms_snapshot
    assert len(purchase.terms_hash) == 64
    assert purchase.terms_accepted_at is not None
    assert purchase.terms_acceptance_method == "online"
    assert purchase.status == CSAPurchaseRequestStatus.PENDING
    assert purchase.open_slot is True
    assert purchase.customer is None
    assert purchase.membership is None
    assert CSACustomer.objects.count() == customer_count
    assert Membership.objects.count() == membership_count


@pytest.mark.django_db
def test_request_code_and_transfer_content_are_unique_and_immutable(
    package_and_option, payment_settings
):
    package, option = package_and_option
    first = create_request(package, option, phone="0912345678")
    second = create_request(package, option, phone="0987654321")

    assert first.request_code != second.request_code
    assert first.transfer_content != second.transfer_content
    assert CSAPurchaseRequest._meta.get_field("request_code").unique is True
    assert CSAPurchaseRequest._meta.get_field("transfer_content").unique is True

    for field, changed in (
        ("request_code", "CSA-CHANGED"),
        ("terms_version", "CSA_TERMS_CHANGED"),
        ("terms_hash", "0" * 64),
        ("email_snapshot", "changed@example.com"),
    ):
        current = CSAPurchaseRequest.objects.get(pk=first.pk)
        setattr(current, field, changed)
        with pytest.raises(ValidationError, match="immutable"):
            current.save()


@pytest.mark.parametrize("unique_field", ["request_code", "transfer_content"])
@pytest.mark.django_db
def test_database_rejects_duplicate_purchase_identifiers(
    unique_field, package_and_option, payment_settings
):
    package, option = package_and_option
    first = create_request(package, option, phone="0912345678")
    duplicate = create_request(package, option, phone="0987654321")

    with pytest.raises(IntegrityError), transaction.atomic():
        CSAPurchaseRequest.objects.filter(pk=duplicate.pk).update(
            **{unique_field: getattr(first, unique_field)}
        )


@pytest.mark.django_db
def test_snapshots_do_not_change_with_package_or_price_updates(
    package_and_option, payment_settings
):
    package, option = package_and_option
    purchase = create_request(package, option)

    package.name = "Tên gói mới"
    package.save(update_fields=["name", "updated_at"])
    option.monthly_price_vnd = 900_000
    option.save(update_fields=["monthly_price_vnd", "updated_at"])
    purchase.refresh_from_db()

    assert purchase.package_name_snapshot == "Gói CSA 6 tháng"
    assert purchase.duration_months_snapshot == 6
    assert purchase.amount_snapshot == 3_000_000


@pytest.mark.django_db
def test_only_one_open_purchase_request_is_allowed_per_normalized_phone(
    package_and_option, payment_settings
):
    package, option = package_and_option
    create_request(package, option, phone="0912345678")

    with pytest.raises(PurchaseRequestConflict):
        create_request(package, option, phone="+84912345678")


@pytest.mark.django_db
def test_payment_confirmed_request_still_blocks_same_phone(package_and_option, payment_settings):
    package, option = package_and_option
    purchase = create_request(package, option)
    purchase.status = CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    purchase.save(update_fields=["status", "updated_at"])

    with pytest.raises(PurchaseRequestConflict):
        create_request(package, option, phone="84912345678")


@pytest.mark.parametrize(
    "terminal_status",
    [CSAPurchaseRequestStatus.APPROVED, CSAPurchaseRequestStatus.REJECTED],
)
@pytest.mark.django_db
def test_terminal_request_allows_a_new_request_for_same_phone(
    terminal_status, package_and_option, payment_settings
):
    package, option = package_and_option
    previous = create_request(package, option)
    previous.status = terminal_status
    previous.save(update_fields=["status", "updated_at"])

    current = create_request(package, option, phone="+84912345678")

    previous.refresh_from_db()
    assert previous.open_slot is None
    assert current.open_slot is True


@pytest.mark.parametrize("inactive_target", ["package", "option"])
@pytest.mark.django_db
def test_inactive_package_or_price_option_is_rejected(
    inactive_target, package_and_option, payment_settings
):
    package, option = package_and_option
    target = package if inactive_target == "package" else option
    target.is_active = False
    target.save(update_fields=["is_active", "updated_at"])

    with pytest.raises(PurchasePackageUnavailable):
        create_request(package, option)


@pytest.mark.django_db
def test_payment_settings_use_singleton_and_increment_version():
    first = update_membership_payment_settings(
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
        direct_transfer_enabled=True,
    )
    second = update_membership_payment_settings(
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="987654321",
        account_name="NATURAL FARMING VIETNAM",
        direct_transfer_enabled=True,
    )

    assert first.key == second.key == "default"
    assert second.version == 2
    assert second.account_number == "987654321"
    assert MembershipPaymentSettings.objects.count() == 1


@pytest.mark.parametrize(
    ("overrides", "error_field"),
    [
        ({"bank_bin": "97043"}, "bank_bin"),
        ({"bank_bin": "999999"}, "bank_bin"),
        ({"bank_name": "Sai tên ngân hàng"}, "bank_name"),
        ({"account_number": "12A456"}, "account_number"),
        ({"account_number": "12345"}, "account_number"),
    ],
)
@pytest.mark.django_db
def test_payment_settings_validate_vietqr_bank_and_account_number(overrides, error_field):
    values = {
        "bank_bin": TEST_BANK["bin"],
        "bank_name": TEST_BANK["name"],
        "account_number": "123456789",
        "account_name": "NATURAL FARMING VIETNAM",
        "direct_transfer_enabled": True,
    }
    values.update(overrides)

    with pytest.raises(ValidationError) as caught:
        MembershipPaymentSettings(**values).full_clean()

    assert error_field in caught.value.message_dict
