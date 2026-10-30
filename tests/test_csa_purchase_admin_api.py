import threading
from datetime import timedelta
from decimal import Decimal
from io import BytesIO

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from pypdf import PdfReader
from rest_framework.test import APIClient

from commerce.models import ShippingAddress, ShippingArea
from identity.models import AccountStatus, AuditLog, User
from members.models import MemberProfile
from memberships.models import (
    CSAContract,
    CSAContractSource,
    CSAContractTermsAcceptanceMethod,
    CSAContractVersion,
    CSACoupon,
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
from memberships.purchases import (
    PurchaseAdminWorkflowConflict,
    approve_csa_purchase_request,
    confirm_csa_payment_quote,
    create_csa_payment_quote,
    create_public_csa_purchase_request,
    reject_csa_purchase_request,
    update_membership_payment_settings,
)
from memberships.services import MembershipError, assign_membership
from memberships.views import csa_purchase_admin_queryset

LIST_URL = "/api/v1/admin/csa-purchase-requests"
TEST_BANK = {
    "bin": "970436",
    "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
    "short_name": "Vietcombank",
    "code": "VCB",
}


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture(autouse=True)
def purchase_admin_bank_directory(monkeypatch, settings, tmp_path, default_supported_shipping_area):
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
        name="Giỏ rau",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    package = MembershipPackage.objects.create(
        name="Gói CSA Online",
        description="Rau theo mùa",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=2)
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=400_000,
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


@pytest.fixture
def request_factory(package_and_option, payment_settings):
    package, option = package_and_option

    def create(
        *,
        status=CSAPurchaseRequestStatus.PAYMENT_CONFIRMED,
        phone="0901234567",
        name="Nguyễn Văn An",
        province_code="66",
        ward_code="22015",
        address="Số 12, ngõ 5",
        user=None,
        terms_locale="vi",
    ):
        item, _token = create_public_csa_purchase_request(
            package_id=package.pk,
            price_option_id=option.pk,
            phone=phone,
            name=name,
            province_code=province_code,
            ward_code=ward_code,
            address=address,
            user=user,
            terms_locale=terms_locale,
        )
        if status != CSAPurchaseRequestStatus.PENDING:
            item.status = status
            item.payment_confirmed_at = (
                timezone.now() if status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED else None
            )
            item.save(update_fields=["status", "payment_confirmed_at", "updated_at"])
        return item

    return create


def request_url(item, action=None):
    url = f"{LIST_URL}/{item.pk}"
    return f"{url}/{action}" if action else url


def confirmed_coupon_request(
    *,
    package,
    option,
    discount_type,
    discount_value,
    code="SNAPSHOTCOUPON",
    maximum_discount_amount=None,
    payment_plan=None,
):
    now = timezone.now()
    coupon = CSACoupon.objects.create(
        code=code,
        discount_type=discount_type,
        discount_value=Decimal(discount_value),
        maximum_discount_amount=(
            Decimal(maximum_discount_amount) if maximum_discount_amount is not None else None
        ),
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
        usage_limit=5,
    )
    identity = {
        "name": "Khách coupon snapshot",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "Địa chỉ snapshot",
    }
    quote_token, _quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        payment_plan_id=payment_plan.pk if payment_plan is not None else None,
        coupon_code=coupon.code,
        guest_identity=identity,
    )
    item, _guest_token = confirm_csa_payment_quote(
        quote_token=quote_token,
        guest_identity=identity,
    )
    return item, coupon


def customer_from_request(item, *, user=None, **overrides):
    values = {
        "name": item.name_snapshot,
        "phone_e164": item.phone_e164,
        "province_code": item.province_code_snapshot,
        "ward_code": item.ward_code_snapshot,
        "address": item.address_snapshot,
        "user": user,
    }
    values.update(overrides)
    return CSACustomer.objects.create(**values)


def create_purchase_user(*, phone="0901234567"):
    user = User.objects.create_user(
        email=f"buyer-{phone}@example.com",
        name="Nguyễn Văn An",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    MemberProfile.objects.create(
        user=user,
        phone_number=phone,
        province_code="66",
        ward_code="22015",
        address="Số 12, ngõ 5",
    )
    ShippingAddress.objects.create(
        user=user,
        recipient_name=user.name,
        phone_number=phone,
        province_code="66",
        ward_code="22015",
        address="Số 12, ngõ 5",
    )
    return user


@pytest.mark.django_db
def test_admin_list_and_detail_require_website_admin(active_user, access_token, request_factory):
    item = request_factory()

    anonymous = APIClient().get(LIST_URL)
    non_admin = bearer_client(access_token).get(request_url(item))

    assert anonymous.status_code == 401
    assert non_admin.status_code == 403


@pytest.mark.django_db
def test_admin_list_searches_filters_paginates_and_detail_hides_guest_hash(
    admin_api, request_factory
):
    pending = request_factory(
        status=CSAPurchaseRequestStatus.PENDING,
        phone="0901111111",
        name="Khách cũ",
    )
    confirmed = request_factory(phone="0902222222", name="Khách mới")

    response = admin_api.get(
        LIST_URL,
        {"search": confirmed.request_code, "status": "payment_confirmed", "page_size": 1},
    )
    detail = admin_api.get(request_url(confirmed))

    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [str(confirmed.pk)]
    assert response.json()["meta"] == {"page": 1, "page_size": 1, "total": 1}
    assert detail.status_code == 200
    assert detail.json()["data"]["phone"] == confirmed.phone_e164
    assert "guest_token_hash" not in detail.json()["data"]
    assert "guest_confirmation_token" not in detail.json()["data"]
    assert str(confirmed.guest_token_hash) not in detail.content.decode()
    newest_first = admin_api.get(LIST_URL).json()["data"]
    assert [row["id"] for row in newest_first[:2]] == [str(confirmed.pk), str(pending.pk)]


@pytest.mark.django_db
def test_admin_purchase_prefetches_both_payment_actors(request_factory, active_user):
    requests = [
        request_factory(phone="0903333333"),
        request_factory(phone="0904444444"),
    ]
    for index, item in enumerate(requests):
        CSAPurchasePayment.objects.create(
            purchase_request=item,
            installment_sequence=1,
            amount_due=Decimal("1200000"),
            amount_paid=Decimal("1200000") if index == 0 else None,
            cycle_count=3,
            status=(
                CSAPurchasePaymentStatus.CONFIRMED
                if index == 0
                else CSAPurchasePaymentStatus.REJECTED
            ),
            confirmed_at=timezone.now() if index == 0 else None,
            confirmed_by=active_user if index == 0 else None,
            rejected_at=timezone.now() if index == 1 else None,
            rejected_by=active_user if index == 1 else None,
            rejection_reason="Bank transfer not found" if index == 1 else "",
        )
    with CaptureQueriesContext(connection) as queries:
        records = list(csa_purchase_admin_queryset().filter(pk__in=[item.pk for item in requests]))
        actors = [
            (payment.confirmed_by or payment.rejected_by).name
            for item in records
            for payment in item.payments.all()
        ]
    assert actors == [active_user.name, active_user.name]
    assert len(queries) == 2  # requests, then payments with both actors joined


@pytest.mark.parametrize(
    "initial_status",
    [CSAPurchaseRequestStatus.PENDING, CSAPurchaseRequestStatus.PAYMENT_CONFIRMED],
)
@pytest.mark.django_db
def test_admin_rejects_open_request_and_audits(
    initial_status, admin_api, active_user, request_factory
):
    item = request_factory(status=initial_status)

    response = admin_api.post(
        request_url(item, "reject"), {"rejection_reason": "Không nhận được tiền"}, format="json"
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.REJECTED
    assert item.rejected_by == active_user
    assert item.rejected_at is not None
    assert item.rejection_reason == "Không nhận được tiền"
    assert item.customer is None
    assert item.membership is None
    log = AuditLog.objects.get(action="csa_purchase_request.rejected")
    assert log.user == active_user
    assert log.created_at is not None
    assert log.context["request_id"] == str(item.pk)
    assert log.context["rejection_reason"] == "Không nhận được tiền"
    assert "guest_token" not in str(log.context)
    assert "account_number" not in str(log.context)


@pytest.mark.django_db
def test_reject_requires_reason(admin_api, request_factory):
    item = request_factory()

    response = admin_api.post(request_url(item, "reject"), {}, format="json")

    assert response.status_code == 400
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_admin_cannot_reject_approved_request(admin_api, request_factory):
    item = request_factory(status=CSAPurchaseRequestStatus.APPROVED)

    response = admin_api.post(
        request_url(item, "reject"), {"rejection_reason": "Retry"}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_request_state_conflict"}]
    assert AuditLog.objects.count() == 0


@pytest.mark.django_db
def test_repeated_reject_is_idempotent(admin_api, request_factory):
    item = request_factory()

    first = admin_api.post(
        request_url(item, "reject"), {"rejection_reason": "Không thấy giao dịch"}, format="json"
    )
    second = admin_api.post(
        request_url(item, "reject"), {"rejection_reason": "Lý do gửi lại"}, format="json"
    )

    assert first.status_code == second.status_code == 200
    item.refresh_from_db()
    assert item.rejection_reason == "Không thấy giao dịch"
    assert AuditLog.objects.filter(action="csa_purchase_request.rejected").count() == 1
    assert not Membership.objects.exists()


@pytest.mark.django_db
def test_admin_cannot_approve_pending_request(admin_api, request_factory):
    item = request_factory(status=CSAPurchaseRequestStatus.PENDING)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_request_state_conflict"}]
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()


@pytest.mark.parametrize("inactive_target", ["package", "option"])
@pytest.mark.django_db
def test_approve_creates_customer_and_one_membership_from_purchase_snapshot(
    inactive_target, admin_api, active_user, request_factory, package_and_option
):
    package, option = package_and_option
    item = request_factory()
    package.name = "Tên gói hiện tại đã đổi"
    package.is_active = inactive_target != "package"
    package.save(update_fields=["name", "is_active", "updated_at"])
    option.duration_months = 6
    option.monthly_price_vnd = 900_000
    option.is_active = inactive_target != "option"
    option.save(update_fields=["duration_months", "monthly_price_vnd", "is_active", "updated_at"])

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    item.refresh_from_db()
    membership = Membership.objects.get()
    customer = CSACustomer.objects.get()
    assert item.status == CSAPurchaseRequestStatus.APPROVED
    assert item.customer == customer
    assert item.membership == membership
    assert item.approved_by == active_user
    assert item.approved_at is not None
    assert membership.customer == customer
    assert membership.package_name_snapshot == "Gói CSA Online"
    assert membership.duration_months_snapshot == 3
    assert membership.monthly_price_vnd_snapshot == 400_000
    assert membership.total_price_vnd_snapshot == 1_200_000
    assert membership.farmbrite_order_reference == item.request_code
    contract = CSAContract.objects.get()
    assert response.json()["data"]["contract"] == {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "status": "active",
    }
    assert contract.reference_code == item.request_code
    assert contract.source == CSAContractSource.ONLINE
    assert contract.purchase_request == item
    assert contract.membership == membership
    assert contract.member_name_snapshot == item.name_snapshot
    assert contract.package_name_snapshot == item.package_name_snapshot
    assert contract.price_option_name_snapshot == item.price_option_name_snapshot
    assert contract.amount_snapshot == item.amount_snapshot
    assert contract.payment_snapshot == {
        "bank_bin": item.bank_bin_snapshot,
        "bank_name": item.bank_name_snapshot,
        "account_number": item.account_number_snapshot,
        "account_name": item.account_name_snapshot,
        "transfer_content": item.transfer_content,
        "payment_config_version": item.payment_config_version,
    }
    assert contract.terms_locale == item.terms_locale
    assert contract.terms_snapshot == item.terms_snapshot
    assert contract.terms_snapshot_vi == item.terms_snapshot
    assert contract.terms_snapshot_vi and contract.terms_snapshot_en
    assert contract.terms_acceptance_method == CSAContractTermsAcceptanceMethod.ONLINE
    assert (contract.start_date, contract.end_date) == (
        membership.start_date,
        membership.end_date,
    )
    assert contract.pdf_vi_storage_key and contract.pdf_en_storage_key
    assert contract.pdf_vi_sha256 and contract.pdf_en_sha256
    with open(
        settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT / contract.pdf_vi_storage_key, "rb"
    ) as pdf_file:
        pdf_text = "\n".join(
            page.extract_text() for page in PdfReader(BytesIO(pdf_file.read())).pages
        )
    assert (contract.end_date - timedelta(days=1)).strftime("%d/%m/%Y") in pdf_text
    assert "Mã giảm giá" not in pdf_text
    log = AuditLog.objects.get(action="csa_purchase_request.approved")
    assert log.user == active_user
    assert log.created_at is not None
    assert log.context["request_id"] == str(item.pk)
    assert log.context["customer_id"] == str(customer.pk)
    assert log.context["membership_id"] == str(membership.pk)
    assert log.context["contract_id"] == str(contract.pk)
    assert log.context["contract_reference_code"] == contract.reference_code
    assert log.context["start_date"] == str(timezone.localdate())
    second = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )
    assert second.status_code == 200
    assert second.json()["data"]["membership_id"] == str(membership.pk)
    assert Membership.objects.count() == 1
    assert CSAContract.objects.count() == 1
    assert AuditLog.objects.filter(action="csa_purchase_request.approved").count() == 1


@pytest.mark.django_db
def test_existing_full_payment_coupon_request_approves_from_immutable_snapshot(
    admin_api, package_and_option, payment_settings
):
    package, _option = package_and_option
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=1,
        monthly_price_vnd=1_635_000,
    )
    now = timezone.now()
    coupon = CSACoupon.objects.create(
        code="PRODUCTION10",
        discount_type="percent",
        discount_value=Decimal("10"),
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
        usage_limit=5,
    )
    identity = {
        "name": "Khách coupon cũ",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "Địa chỉ snapshot",
    }
    quote_token, _quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        coupon_code=coupon.code,
        guest_identity=identity,
    )
    item, _guest_token = confirm_csa_payment_quote(
        quote_token=quote_token,
        guest_identity=identity,
    )
    identity_snapshot = (
        item.guest_token_hash,
        item.name_snapshot,
        item.phone_e164,
        item.address_snapshot,
    )
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.payment_plan_id is None
    assert item.payment_installments_snapshot is None
    assert item.amount_snapshot == 1_635_000
    assert item.customer_payable_total_snapshot == Decimal("1471500")
    assert item.payments.get().amount_due == Decimal("1471500")
    coupon.refresh_from_db()
    assert coupon.used_count == 1

    CSACoupon.objects.filter(pk=coupon.pk).update(
        is_active=False,
        discount_value=Decimal("99"),
    )
    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )

    assert response.status_code == 200, response.json()
    item.refresh_from_db()
    coupon.refresh_from_db()
    payment = item.payments.get()
    contract = CSAContract.objects.get(purchase_request=item)
    version = contract.current_version
    assert payment.status == CSAPurchasePaymentStatus.CONFIRMED
    assert payment.amount_paid == Decimal("1471500")
    assert item.membership.duration_months_snapshot == 1
    assert contract.amount_snapshot == 1_635_000
    assert version.amount_snapshot == Decimal("1635000")
    assert version.total_payment_amount == Decimal("1471500")
    assert version.paid_amount == Decimal("1471500")
    assert version.remaining_amount == 0
    assert version.pricing_snapshot == {
        "original_total": "1635000",
        "discount_amount": "163500",
        "customer_payable_total": "1471500",
        "initial_payment_before_discount": "1635000",
        "initial_payment_after_discount": "1471500",
        "coupon": {
            "code": "PRODUCTION10",
            "discount_type": "percent",
            "discount_value": "10.00",
            "maximum_discount_amount": None,
        },
    }
    assert identity_snapshot == (
        item.guest_token_hash,
        item.name_snapshot,
        item.phone_e164,
        item.address_snapshot,
    )
    assert coupon.used_count == 1
    for key, expected in (
        (
            version.pdf_vi_storage_key,
            (
                "Mã giảm giá",
                "PRODUCTION10",
                "1.635.000",
                "163.500",
                "1.471.500",
                "Đã thanh toán",
                "Còn phải thanh toán",
            ),
        ),
        (
            version.pdf_en_storage_key,
            (
                "Coupon code",
                "PRODUCTION10",
                "1.635.000",
                "163.500",
                "1.471.500",
                "Paid",
                "Remaining",
            ),
        ),
    ):
        with open(settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT / key, "rb") as source:
            text = "\n".join(
                page.extract_text() for page in PdfReader(BytesIO(source.read())).pages
            )
        assert all(value in text for value in expected)

    replay = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )
    assert replay.status_code == 200
    coupon.refresh_from_db()
    assert coupon.used_count == 1
    assert Membership.objects.count() == 1
    assert CSAContract.objects.count() == 1
    assert CSAContractVersion.objects.count() == 1
    assert CSAPurchasePayment.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("discount_type", "discount_value"),
    [("percent", "100"), ("fixed", "1200000")],
)
def test_zero_payable_coupon_approves_versions_pdfs_and_replays(
    admin_api,
    package_and_option,
    payment_settings,
    discount_type,
    discount_value,
):
    package, option = package_and_option
    item, coupon = confirmed_coupon_request(
        package=package,
        option=option,
        discount_type=discount_type,
        discount_value=discount_value,
        code=f"ZERO{discount_type.upper()}",
    )
    payment = item.payments.get()
    assert item.amount_snapshot == 1_200_000
    assert item.customer_payable_total_snapshot == 0
    assert payment.amount_due == 0
    assert payment.coupon_zero_amount is True

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )

    assert response.status_code == 200, response.json()
    item.refresh_from_db()
    coupon.refresh_from_db()
    payment.refresh_from_db()
    contract = CSAContract.objects.get(purchase_request=item)
    version = contract.current_version
    assert payment.status == CSAPurchasePaymentStatus.CONFIRMED
    assert payment.amount_paid == 0
    assert version.amount_snapshot == Decimal("1200000")
    assert version.total_payment_amount == 0
    assert version.paid_amount == 0
    assert version.remaining_amount == 0
    assert version.pricing_snapshot["original_total"] == "1200000"
    assert version.pricing_snapshot["discount_amount"] == "1200000"
    assert version.pricing_snapshot["customer_payable_total"] == "0"
    assert version.pricing_snapshot["coupon"]["code"] == coupon.code
    assert coupon.used_count == 1

    for key, expected in (
        (
            version.pdf_vi_storage_key,
            (
                "Giá gốc trước mã giảm giá",
                "Số tiền giảm",
                "Tổng phải thanh toán sau giảm",
                "Đã thanh toán",
                "Còn phải thanh toán",
                "1.200.000",
                "0 VND",
            ),
        ),
        (
            version.pdf_en_storage_key,
            (
                "Original total before coupon",
                "Discount amount",
                "Customer payable total",
                "Paid",
                "Remaining",
                "1.200.000",
                "0 VND",
            ),
        ),
    ):
        with open(settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT / key, "rb") as source:
            text = "\n".join(
                page.extract_text() for page in PdfReader(BytesIO(source.read())).pages
            )
        assert coupon.code in text
        assert all(value in text for value in expected)

    replay = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )
    assert replay.status_code == 200
    coupon.refresh_from_db()
    assert coupon.used_count == 1
    assert Membership.objects.count() == 1
    assert CSAContract.objects.count() == 1
    assert CSAContractVersion.objects.count() == 1
    assert CSAPurchasePayment.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("discount_type", "discount_value", "maximum_discount"),
    [("fixed", "100000", None), ("percent", "50", "100000")],
)
def test_nonzero_full_coupon_variants_approve_with_valid_initial_snapshot(
    admin_api,
    package_and_option,
    payment_settings,
    discount_type,
    discount_value,
    maximum_discount,
):
    package, option = package_and_option
    item, _coupon = confirmed_coupon_request(
        package=package,
        option=option,
        discount_type=discount_type,
        discount_value=discount_value,
        maximum_discount_amount=maximum_discount,
    )

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )

    assert response.status_code == 200, response.json()
    version = CSAContract.objects.get(purchase_request=item).current_version
    assert version.amount_snapshot == Decimal("1200000")
    assert version.total_payment_amount == Decimal("1100000")
    assert version.paid_amount == Decimal("1100000")
    assert version.remaining_amount == 0
    assert version.pricing_snapshot["discount_amount"] == "100000"
    assert version.pricing_snapshot["customer_payable_total"] == "1100000"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "missing_field",
    [
        "amount_before_discount",
        "discount_amount",
        "amount_after_discount",
        "months_covered",
        "legacy_keyset",
        "extra_field",
        "wrong_month_type",
        "invalid_amount",
    ],
)
def test_installment_coupon_missing_metadata_returns_stable_schedule_error(
    admin_api,
    package_and_option,
    payment_settings,
    missing_field,
):
    package, option = package_and_option
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Two installments",
        payment_type=MembershipPaymentType.INSTALLMENT,
        total_amount=Decimal("1200000"),
        installment_count=2,
        is_active=True,
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=plan,
        sequence=1,
        amount=Decimal("600000"),
        cycle_count=1,
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=plan,
        sequence=2,
        amount=Decimal("600000"),
        cycle_count=2,
    )
    item, _coupon = confirmed_coupon_request(
        package=package,
        option=option,
        payment_plan=plan,
        discount_type="fixed",
        discount_value="100000",
    )
    broken = [dict(row) for row in item.payment_installments_snapshot]
    coupon_fields = {
        "amount_before_discount",
        "discount_amount",
        "amount_after_discount",
        "months_covered",
    }
    if missing_field == "legacy_keyset":
        for row in broken:
            for field in coupon_fields:
                row.pop(field)
    elif missing_field == "extra_field":
        broken[0]["unexpected"] = "value"
    elif missing_field == "wrong_month_type":
        broken[0]["months_covered"] = "1"
    elif missing_field == "invalid_amount":
        broken[0]["amount_before_discount"] = "NaN"
    else:
        broken[0].pop(missing_field)
    type(item).objects.filter(pk=item.pk).update(payment_installments_snapshot=broken)

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_payment_schedule_invalid"}]
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.membership_id is None
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not CSAContractVersion.objects.exists()
    assert not AuditLog.objects.filter(action="csa_purchase_request.approved").exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("discount_type", "discount_value", "maximum_discount", "snapshot_changes"),
    [
        (
            "fixed",
            "100000",
            None,
            {
                "initial_payment_before_discount": "1000000",
                "initial_payment_after_discount": "900000",
            },
        ),
        (
            "fixed",
            "100000",
            None,
            {"initial_payment_after_discount": "1000001"},
        ),
        (
            "percent",
            "50",
            "100000",
            {
                "initial_payment_before_discount": "1000000",
                "initial_payment_after_discount": "900000",
            },
        ),
        ("fixed", "100000", None, {"coupon_code": " SNAPSHOTCOUPON"}),
        ("fixed", "100000", None, {"coupon_code": "snapshotcoupon"}),
        ("fixed", "100000", None, {"coupon_code": "BAD CODE"}),
    ],
)
def test_full_coupon_malformed_initial_amount_or_code_is_rejected(
    admin_api,
    package_and_option,
    payment_settings,
    discount_type,
    discount_value,
    maximum_discount,
    snapshot_changes,
):
    package, option = package_and_option
    item, _coupon = confirmed_coupon_request(
        package=package,
        option=option,
        discount_type=discount_type,
        discount_value=discount_value,
        maximum_discount_amount=maximum_discount,
    )
    broken = {**item.coupon_snapshot, **snapshot_changes}
    type(item).objects.filter(pk=item.pk).update(coupon_snapshot=broken)

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_payment_schedule_invalid"}]
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.membership_id is None
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not CSAContractVersion.objects.exists()
    assert not AuditLog.objects.filter(action="csa_purchase_request.approved").exists()


@pytest.mark.django_db
def test_coupon_snapshot_mismatch_is_rejected_without_partial_approval(
    admin_api, package_and_option, payment_settings
):
    package, option = package_and_option
    now = timezone.now()
    coupon = CSACoupon.objects.create(
        code="BROKEN10",
        discount_type="percent",
        discount_value=Decimal("10"),
        starts_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
    )
    identity = {
        "name": "Khách snapshot lỗi",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "Địa chỉ snapshot",
    }
    quote_token, _quote = create_csa_payment_quote(
        package_id=package.pk,
        price_option_id=option.pk,
        coupon_code=coupon.code,
        guest_identity=identity,
    )
    item, _guest_token = confirm_csa_payment_quote(
        quote_token=quote_token,
        guest_identity=identity,
    )
    broken = {**item.coupon_snapshot, "discount_amount": "1"}
    type(item).objects.filter(pk=item.pk).update(coupon_snapshot=broken)

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate())},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_payment_schedule_invalid"}]
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.membership_id is None
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not AuditLog.objects.filter(action="csa_purchase_request.approved").exists()


@pytest.mark.django_db
def test_approve_reuses_matching_customer_without_creating_second(admin_api, request_factory):
    item = request_factory()
    existing = customer_from_request(item)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.customer == existing
    assert CSACustomer.objects.count() == 1
    assert Membership.objects.get().customer == existing


@pytest.mark.django_db
def test_online_contract_keeps_exact_accepted_english_terms_and_same_version_vietnamese(
    admin_api, request_factory
):
    item = request_factory(terms_locale="en")

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    contract = CSAContract.objects.get(purchase_request=item)
    assert contract.terms_locale == "en"
    assert contract.terms_snapshot == item.terms_snapshot
    assert contract.terms_snapshot_en == item.terms_snapshot
    assert contract.terms_snapshot_vi
    assert contract.terms_snapshot_vi != contract.terms_snapshot_en


@pytest.mark.django_db
def test_customer_difference_requires_explicit_resolution(admin_api, request_factory):
    item = request_factory()
    existing = customer_from_request(item, name="Tên hiện tại", address="Địa chỉ hiện tại")

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 409
    error = response.json()["errors"][0]
    assert error["code"] == "csa_purchase_customer_resolution_required"
    assert error["context"]["customer"]["id"] == str(existing.pk)
    assert error["context"]["customer"]["name"] == "Tên hiện tại"
    assert error["context"]["request"]["name"] == item.name_snapshot
    item.refresh_from_db()
    existing.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert existing.name == "Tên hiện tại"
    assert not Membership.objects.exists()


@pytest.mark.django_db
def test_keep_existing_resolution_does_not_overwrite_customer(admin_api, request_factory):
    item = request_factory()
    existing = customer_from_request(item, name="Tên hiện tại", address="Địa chỉ hiện tại")

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate()), "customer_resolution": "keep_existing"},
        format="json",
    )

    assert response.status_code == 200
    existing.refresh_from_db()
    assert existing.name == "Tên hiện tại"
    assert existing.address == "Địa chỉ hiện tại"
    assert CSACustomer.objects.count() == 1
    log = AuditLog.objects.get(action="csa_purchase_request.conflict_resolved")
    assert log.context["customer_resolution"] == "keep_existing"


@pytest.mark.django_db
def test_use_request_data_resolution_updates_customer_and_audits_old_and_new(
    admin_api, request_factory
):
    item = request_factory()
    existing = customer_from_request(
        item,
        name="Tên hiện tại",
        province_code="01",
        ward_code="00004",
        address="Địa chỉ hiện tại",
    )

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate()), "customer_resolution": "use_request_data"},
        format="json",
    )

    assert response.status_code == 200
    existing.refresh_from_db()
    assert _customer_values(existing) == {
        "name": item.name_snapshot,
        "province_code": item.province_code_snapshot,
        "ward_code": item.ward_code_snapshot,
        "address": item.address_snapshot,
    }
    assert CSACustomer.objects.count() == 1
    log = AuditLog.objects.get(action="csa_purchase_request.conflict_resolved")
    assert log.context["customer_resolution"] == "use_request_data"
    assert log.context["old_customer_data"]["name"] == "Tên hiện tại"
    assert log.context["new_customer_data"]["name"] == item.name_snapshot


def _customer_values(customer):
    return {
        "name": customer.name,
        "province_code": customer.province_code,
        "ward_code": customer.ward_code,
        "address": customer.address,
    }


@pytest.mark.django_db
def test_reject_conflict_resolution_uses_reject_flow(admin_api, request_factory):
    item = request_factory()
    customer_from_request(item, name="Tên hiện tại")
    reason = "Thông tin người mua không khớp hồ sơ Customer."

    response = admin_api.post(
        request_url(item, "approve"),
        {
            "start_date": str(timezone.localdate()),
            "customer_resolution": "reject_conflict",
            "rejection_reason": reason,
        },
        format="json",
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.REJECTED
    assert item.rejection_reason == reason
    assert item.membership is None
    assert not Membership.objects.exists()
    assert set(AuditLog.objects.values_list("action", flat=True)) == {
        "csa_purchase_request.conflict_resolved",
        "csa_purchase_request.rejected",
    }
    assert (
        AuditLog.objects.get(action="csa_purchase_request.rejected").context["rejection_reason"]
        == reason
    )


@pytest.mark.django_db
def test_reject_conflict_requires_reason_without_mutating_request(admin_api, request_factory):
    item = request_factory()
    customer_from_request(item, name="Tên hiện tại")

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate()), "customer_resolution": "reject_conflict"},
        format="json",
    )

    assert response.status_code == 400
    assert "rejection_reason" in response.json()
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.rejection_reason == ""
    assert not Membership.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_request_user_with_existing_customer_uses_that_customer(admin_api, request_factory):
    user = create_purchase_user()
    customer = CSACustomer.objects.create(
        user=user,
        name="Tên Customer",
        phone_e164="+84909999999",
        province_code="66",
        ward_code="22015",
        address="Địa chỉ Customer",
    )
    item = request_factory(user=user)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.customer == customer
    assert CSACustomer.objects.count() == 1


@pytest.mark.django_db
def test_request_user_without_customer_creates_and_links_customer(admin_api, request_factory):
    user = create_purchase_user()
    item = request_factory(user=user)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    item.refresh_from_db()
    assert item.customer.user == user
    assert item.membership.customer == item.customer
    assert CSACustomer.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_request_user_links_matching_unmanaged_phone_customer(admin_api, request_factory):
    user = create_purchase_user()
    item = request_factory(user=user)
    existing = customer_from_request(item)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    existing.refresh_from_db()
    item.refresh_from_db()
    assert existing.user == user
    assert item.customer == existing
    assert CSACustomer.objects.count() == 1


@pytest.mark.django_db
def test_request_user_link_reports_unavailable_shipping_address_without_mutating_customer(
    admin_api, request_factory
):
    user = create_purchase_user()
    item = request_factory(user=user)
    existing = customer_from_request(item)
    user.shipping_address.delete()

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [
        {"code": "csa_purchase_customer_shipping_address_unavailable"}
    ]
    item.refresh_from_db()
    existing.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.customer_id is None
    assert existing.user_id is None
    assert existing.address == item.address_snapshot
    assert not Membership.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_linked_purchase_conflict_audit_uses_final_shipping_address(admin_api, request_factory):
    ShippingArea.objects.create(
        province_code="66",
        ward_code="22045",
        shipping_fee_vnd=30_000,
        is_active=True,
    )
    user = create_purchase_user()
    item = request_factory(user=user)
    existing = customer_from_request(item, name="Tên hiện tại", address="Địa chỉ hiện tại")
    shipping_address = user.shipping_address
    shipping_address.ward_code = "22045"
    shipping_address.address = "Địa chỉ giao hàng mới"
    shipping_address.save(update_fields=["ward_code", "address", "updated_at"])

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate()), "customer_resolution": "use_request_data"},
        format="json",
    )

    assert response.status_code == 200
    existing.refresh_from_db()
    assert existing.user_id == user.pk
    assert existing.name == item.name_snapshot
    assert existing.province_code == "66"
    assert existing.ward_code == "22045"
    assert existing.address == "Địa chỉ giao hàng mới"
    log = AuditLog.objects.get(action="csa_purchase_request.conflict_resolved")
    assert log.context["new_customer_data"] == {
        "name": item.name_snapshot,
        "province_code": "66",
        "ward_code": "22045",
        "address": "Địa chỉ giao hàng mới",
    }


@pytest.mark.django_db
def test_request_user_with_incomplete_customer_is_completed_from_purchase_snapshot(
    admin_api, request_factory
):
    user = create_purchase_user()
    customer = CSACustomer.objects.create(
        user=user,
        name="Customer thiếu địa chỉ",
        phone_e164="+84909999999",
        province_code="",
        ward_code="",
        address="",
    )
    item = request_factory(user=user)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 200
    customer.refresh_from_db()
    item.refresh_from_db()
    assert customer.name == item.name_snapshot
    assert customer.phone_e164 == item.phone_e164
    assert customer.province_code == item.province_code_snapshot
    assert customer.ward_code == item.ward_code_snapshot
    assert customer.address == item.address_snapshot
    assert item.customer == customer
    assert Membership.objects.get().customer == customer


@pytest.mark.django_db
def test_user_link_conflict_is_rejected_without_moving_link(admin_api, request_factory):
    user = create_purchase_user()
    item = request_factory(user=user)
    matching_phone = customer_from_request(item)
    linked_elsewhere = CSACustomer.objects.create(
        user=user,
        name="Customer khác",
        phone_e164="+84909999999",
        province_code="66",
        ward_code="22015",
        address="Địa chỉ khác",
    )

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate()), "customer_resolution": "keep_existing"},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_customer_conflict"}]
    matching_phone.refresh_from_db()
    linked_elsewhere.refresh_from_db()
    assert matching_phone.user is None
    assert linked_elsewhere.user == user
    assert not Membership.objects.exists()


@pytest.mark.django_db
def test_approve_rejects_overlapping_start_date(
    admin_api, active_user, request_factory, package_and_option
):
    package, option = package_and_option
    item = request_factory()
    customer = customer_from_request(item)
    existing = assign_membership(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate(),
        farmbrite_order_reference="EXISTING-OVERLAP",
        actor=active_user,
    )

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(existing.start_date)}, format="json"
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "membership_overlap"}]
    assert Membership.objects.count() == 1
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED


@pytest.mark.django_db
def test_approve_rejects_date_before_next_available_customer_date(
    admin_api, active_user, request_factory, package_and_option
):
    package, option = package_and_option
    item = request_factory()
    customer = customer_from_request(item)
    assign_membership(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate() + timedelta(days=240),
        farmbrite_order_reference="EXISTING-FUTURE",
        actor=active_user,
    )

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_purchase_start_date_before_next_available"}]
    assert Membership.objects.count() == 1


@pytest.mark.django_db
def test_approve_rejects_start_date_in_the_past(admin_api, request_factory):
    item = request_factory()

    response = admin_api.post(
        request_url(item, "approve"),
        {"start_date": str(timezone.localdate() - timedelta(days=1))},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_purchase_start_date_before_next_available"}]
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()


@pytest.mark.django_db
def test_approve_rolls_back_new_customer_when_membership_creation_fails(
    admin_api, request_factory, monkeypatch
):
    item = request_factory()

    def fail_assignment(**_kwargs):
        raise MembershipError("Simulated assignment failure")

    monkeypatch.setattr("memberships.purchases.assign_membership", fail_assignment)

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 409
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.customer is None
    assert item.membership is None
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_approve_rolls_back_customer_update_when_membership_creation_fails(
    admin_api, request_factory, monkeypatch
):
    item = request_factory()
    customer = customer_from_request(item, name="Tên cũ", address="Địa chỉ cũ")

    def fail_assignment(**_kwargs):
        raise MembershipError("Simulated assignment failure")

    monkeypatch.setattr("memberships.purchases.assign_membership", fail_assignment)
    response = admin_api.post(
        request_url(item, "approve"),
        {
            "start_date": str(timezone.localdate()),
            "customer_resolution": "use_request_data",
        },
        format="json",
    )

    assert response.status_code == 409
    item.refresh_from_db()
    customer.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.customer is None
    assert item.membership is None
    assert customer.name == "Tên cũ"
    assert customer.address == "Địa chỉ cũ"
    assert not Membership.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_approve_rolls_back_when_contract_creation_fails(admin_api, request_factory, monkeypatch):
    item = request_factory()

    def fail_contract(**_kwargs):
        from memberships.contracts import CSAContractSnapshotInvalid

        raise CSAContractSnapshotInvalid("Simulated contract failure")

    monkeypatch.setattr("memberships.purchases.create_online_contract", fail_contract)
    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_contract_snapshot_invalid"}]
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.customer_id is None and item.membership_id is None
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_approve_rolls_back_when_pdf_generation_fails(admin_api, request_factory, monkeypatch):
    item = request_factory()

    def fail_pdf(*_args, **_kwargs):
        from memberships.contract_pdfs import CSAContractPDFStorageError

        raise CSAContractPDFStorageError("Simulated PDF storage failure")

    monkeypatch.setattr("memberships.purchases.issue_contract_version", fail_pdf)
    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 503
    assert response.json()["errors"] == [{"code": "csa_contract_pdf_storage_error"}]
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.customer_id is None and item.membership_id is None
    assert not CSACustomer.objects.exists()
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_legacy_request_without_terms_cannot_be_approved(admin_api, request_factory):
    item = request_factory()
    type(item).objects.filter(pk=item.pk).update(
        terms_version=None,
        terms_locale=None,
        terms_snapshot=None,
        terms_hash=None,
        terms_accepted_at=None,
        terms_acceptance_method=None,
    )

    response = admin_api.post(
        request_url(item, "approve"), {"start_date": str(timezone.localdate())}, format="json"
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_contract_terms_snapshot_incomplete"}]
    item.refresh_from_db()
    assert item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert item.customer_id is None and item.membership_id is None
    assert not Membership.objects.exists()
    assert not CSAContract.objects.exists()
    assert not AuditLog.objects.exists()


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_approve_creates_exactly_one_membership(active_user, request_factory):
    item = request_factory()
    barrier = threading.Barrier(3)
    outcomes = []

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
        except PurchaseAdminWorkflowConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=approve_once) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert outcomes == ["approved", "approved"]
    assert Membership.objects.count() == 1
    assert AuditLog.objects.filter(action="csa_purchase_request.approved").count() == 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_approve_and_reject_have_one_terminal_winner(active_user, request_factory):
    item = request_factory()
    barrier = threading.Barrier(3)
    outcomes = []

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
        except PurchaseAdminWorkflowConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    def reject_once():
        close_old_connections()
        try:
            barrier.wait()
            reject_csa_purchase_request(
                request_id=item.pk,
                reason="Admin rejected concurrently",
                actor=active_user,
            )
            outcomes.append("rejected")
        except PurchaseAdminWorkflowConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=approve_once), threading.Thread(target=reject_once)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    item.refresh_from_db()
    assert sorted(outcomes) in (["approved", "conflict"], ["conflict", "rejected"])
    assert item.status in {
        CSAPurchaseRequestStatus.APPROVED,
        CSAPurchaseRequestStatus.REJECTED,
    }
    assert Membership.objects.count() == (
        1 if item.status == CSAPurchaseRequestStatus.APPROVED else 0
    )
    assert (
        AuditLog.objects.filter(
            action__in=["csa_purchase_request.approved", "csa_purchase_request.rejected"]
        ).count()
        == 1
    )
