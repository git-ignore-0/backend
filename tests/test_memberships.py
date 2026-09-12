import importlib
import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Group
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AccountStatus, AuditLog, User
from members.models import MemberProfile
from memberships.models import (
    CSAContract,
    CSAContractSource,
    CSAContractTermsAcceptanceMethod,
    CSACustomer,
    Membership,
    MembershipCycle,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPackageTranslation,
    MembershipPaymentSettings,
    MembershipStatus,
    MembershipUsageAllocation,
    MembershipUsageLine,
    MembershipUsageOrder,
    MembershipUsageReversal,
    Product,
    ProductTranslation,
    QuotaPolicy,
    UsageOrderStatus,
)
from memberships.pricing import MAX_POSITIVE_BIG_INTEGER
from memberships.services import (
    DuplicateFarmbriteOrderReference,
    InsufficientQuota,
    MembershipNotRevocable,
    MembershipOverlap,
    PackageUnavailable,
    PriceOptionTotalInvalid,
    PriceOptionUnavailable,
    ProductNotInMembership,
    StartDateInPast,
    UsageIntegrityConflict,
    UsageUndoConflict,
    add_months,
    assign_membership,
    create_csa_customer_for_user,
    create_managed_csa_customer,
    create_usage_order,
    ensure_due_cycles,
    revoke_membership,
    sync_membership_status,
    undo_usage_order,
)


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture
def manual_payment_settings(monkeypatch, settings, tmp_path):
    bank = {"bin": "970436", "name": "Vietcombank"}
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [bank])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"
    return MembershipPaymentSettings.objects.create(
        bank_bin=bank["bin"],
        bank_name=bank["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
    )


def create_active_user(email, *, phone="0901234567"):
    user = User.objects.create_user(
        email=email,
        name="Membership User",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    if phone is not None:
        MemberProfile.objects.create(user=user, phone_number=phone)
    return user


def customer_for(user):
    try:
        return CSACustomer.objects.get(user=user)
    except CSACustomer.DoesNotExist:
        suffix = str(user.pk.int)[-8:].zfill(8)
        return create_csa_customer_for_user(
            user=user,
            name=user.name,
            phone=f"090{suffix[-7:]}",
            province_code="66",
            ward_code="22015",
            address="Số 12, ngõ 5",
        )


def product_translations(
    *,
    vi_name="Giỏ rau theo mùa",
    en_name="Seasonal basket",
    vi_description="Rau tươi",
    en_description="Fresh vegetables",
):
    return [
        {"locale": "vi", "name": vi_name, "short_description": vi_description},
        {"locale": "en", "name": en_name, "short_description": en_description},
    ]


def package_translations(
    *,
    vi_name="Gói CSA",
    en_name="CSA package",
    vi_description="Giao mỗi chu kỳ",
    en_description="One delivery per cycle",
):
    return [
        {"locale": "vi", "name": vi_name, "description": vi_description},
        {"locale": "en", "name": en_name, "description": en_description},
    ]


def create_package(
    *,
    policy=QuotaPolicy.EXPIRE,
    quota=2,
    duration=6,
    monthly_price=1_000_000,
):
    product = Product.objects.create(
        name="Giỏ rau theo mùa",
        short_description="Rau tươi",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    ProductTranslation.objects.bulk_create(
        [ProductTranslation(product=product, **item) for item in product_translations()]
    )
    package = MembershipPackage.objects.create(
        name="Gói CSA",
        description="Giao mỗi chu kỳ",
        quota_policy=policy,
    )
    MembershipPackageTranslation.objects.bulk_create(
        [MembershipPackageTranslation(package=package, **item) for item in package_translations()]
    )
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=quota)
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=duration,
        monthly_price_vnd=monthly_price,
        is_active=True,
        sort_order=0,
    )
    return package, product, option


def assign(
    user,
    *,
    package=None,
    option=None,
    actor=None,
    start_date=None,
    reference=None,
):
    if package is None:
        package, product, option = create_package()
    else:
        product = package.items.get().product
    membership = assign_membership(
        customer_id=customer_for(user).pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=start_date or timezone.localdate(),
        farmbrite_order_reference=reference or f"FB-{uuid.uuid4()}",
        actor=actor or user,
    )
    return membership, product, option


def assign_with_past_start(user, *, policy=QuotaPolicy.EXPIRE, quota=2, months_ago=1):
    package, product, option = create_package(policy=policy, quota=quota)
    membership, _product, _option = assign(user, package=package, option=option)
    MembershipCycleQuota.objects.filter(cycle__membership=membership).delete()
    membership.cycles.all().delete()
    membership.start_date = add_months(timezone.localdate(), -months_ago)
    membership.end_date = add_months(membership.start_date, option.duration_months)
    membership.save(update_fields=["start_date", "end_date", "updated_at"])
    return membership, product, option


@pytest.fixture
def admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


def product_api_payload():
    return {
        "translations": product_translations(),
        "unit_size": "0.500",
        "unit_label_vi": "kg",
        "unit_label_en": "kg",
    }


def package_api_payload(product_id, *, options=None):
    return {
        "translations": package_translations(),
        "quota_policy": "expire",
        "items": [{"product_id": str(product_id), "quota_units": 2}],
        "price_options": options
        or [
            {
                "duration_months": 6,
                "monthly_price_vnd": 1_000_000,
                "is_active": True,
                "sort_order": 0,
            }
        ],
    }


@pytest.mark.django_db
def test_admin_package_requires_an_active_price_option_and_rejects_unknown_total(admin_api):
    product = admin_api.post(
        "/api/v1/admin/membership-products", product_api_payload(), format="json"
    ).json()["data"]
    missing = package_api_payload(product["id"])
    missing.pop("price_options")
    response = admin_api.post("/api/v1/admin/membership-packages", missing, format="json")
    assert response.status_code == 400
    assert "price_options" in response.json()

    inactive = package_api_payload(
        product["id"],
        options=[
            {
                "duration_months": 6,
                "monthly_price_vnd": 1_000_000,
                "is_active": False,
            }
        ],
    )
    response = admin_api.post("/api/v1/admin/membership-packages", inactive, format="json")
    assert response.status_code == 400

    forged_total = package_api_payload(product["id"])
    forged_total["price_options"][0]["total_price_vnd"] = 1
    response = admin_api.post("/api/v1/admin/membership-packages", forged_total, format="json")
    assert response.status_code == 400
    assert response.json()["price_options"][0]["total_price_vnd"] == ("This field is not allowed.")


@pytest.mark.django_db
def test_active_package_duration_is_unique_and_total_is_computed_by_backend(admin_api):
    product_id = admin_api.post(
        "/api/v1/admin/membership-products", product_api_payload(), format="json"
    ).json()["data"]["id"]
    duplicate = package_api_payload(
        product_id,
        options=[
            {"duration_months": 3, "monthly_price_vnd": 400_000},
            {"duration_months": 3, "monthly_price_vnd": 500_000},
        ],
    )
    response = admin_api.post("/api/v1/admin/membership-packages", duplicate, format="json")
    assert response.status_code == 400
    assert "price_options" in response.json()

    valid = package_api_payload(
        product_id,
        options=[
            {
                "duration_months": 3,
                "monthly_price_vnd": 400_001,
                "is_active": True,
                "sort_order": 2,
            }
        ],
    )
    response = admin_api.post("/api/v1/admin/membership-packages", valid, format="json")
    assert response.status_code == 201
    option = response.json()["data"]["price_options"][0]
    assert option["monthly_price_vnd"] == "400001"
    assert option["total_price_vnd"] == "1200003"

    package = MembershipPackage.objects.get(pk=response.json()["data"]["id"])
    with pytest.raises(IntegrityError), transaction.atomic():
        MembershipPackagePriceOption.objects.create(
            package=package, duration_months=3, monthly_price_vnd=1
        )

    historical = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=300_000,
        is_active=False,
    )
    assert historical.active_duration_key is None


@pytest.mark.django_db
def test_last_active_option_cannot_be_deactivated_or_removed(admin_api):
    package, _product, option = create_package()
    response = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package.pk}",
        {
            "price_options": [
                {
                    "id": str(option.pk),
                    "duration_months": option.duration_months,
                    "monthly_price_vnd": option.monthly_price_vnd,
                    "is_active": False,
                    "sort_order": 0,
                }
            ]
        },
        format="json",
    )
    assert response.status_code == 400
    option.refresh_from_db()
    assert option.is_active is True

    response = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package.pk}",
        {"price_options": []},
        format="json",
    )
    assert response.status_code == 400
    assert package.price_options.filter(is_active=True).count() == 1


@pytest.mark.django_db
def test_used_price_option_price_and_duration_are_immutable(active_user, admin_api):
    package, _product, option = create_package()
    assign(active_user, package=package, option=option, reference="FB-USED-OPTION")
    original = (option.duration_months, option.monthly_price_vnd)

    for changes in (
        {"duration_months": 12},
        {"monthly_price_vnd": 900_000},
    ):
        response = admin_api.patch(
            f"/api/v1/admin/membership-packages/{package.pk}",
            {
                "price_options": [
                    {
                        "id": str(option.pk),
                        "duration_months": changes.get("duration_months", option.duration_months),
                        "monthly_price_vnd": changes.get(
                            "monthly_price_vnd", option.monthly_price_vnd
                        ),
                        "is_active": True,
                        "sort_order": 0,
                    }
                ]
            },
            format="json",
        )
        assert response.status_code == 400
        option.refresh_from_db()
        assert (option.duration_months, option.monthly_price_vnd) == original


@pytest.mark.django_db
def test_used_option_can_deactivate_then_new_price_can_reuse_duration(active_user, admin_api):
    package, _product, option = create_package()
    membership, _product, _option = assign(
        active_user, package=package, option=option, reference="FB-HISTORICAL-OPTION"
    )
    second = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=12,
        monthly_price_vnd=800_000,
        is_active=True,
        sort_order=1,
    )
    deactivate = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package.pk}",
        {
            "price_options": [
                {
                    "id": str(option.pk),
                    "duration_months": 6,
                    "monthly_price_vnd": 1_000_000,
                    "is_active": False,
                    "sort_order": 5,
                },
                {
                    "id": str(second.pk),
                    "duration_months": 12,
                    "monthly_price_vnd": 800_000,
                    "is_active": True,
                    "sort_order": 1,
                },
            ]
        },
        format="json",
    )
    assert deactivate.status_code == 200
    option.refresh_from_db()
    assert option.is_active is False
    assert option.active_duration_key is None
    assert option.sort_order == 5

    replace = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package.pk}",
        {
            "price_options": [
                {
                    "duration_months": 6,
                    "monthly_price_vnd": 1_100_000,
                    "is_active": True,
                    "sort_order": 0,
                },
                {
                    "id": str(option.pk),
                    "duration_months": 6,
                    "monthly_price_vnd": 1_000_000,
                    "is_active": False,
                    "sort_order": 5,
                },
                {
                    "id": str(second.pk),
                    "duration_months": 12,
                    "monthly_price_vnd": 800_000,
                    "is_active": True,
                    "sort_order": 1,
                },
            ]
        },
        format="json",
    )
    assert replace.status_code == 200
    replacement = package.price_options.get(is_active=True, duration_months=6)
    assert replacement.monthly_price_vnd == 1_100_000
    assert membership.price_option_id == option.pk
    with pytest.raises(ProtectedError):
        option.delete()


@pytest.mark.django_db
def test_public_catalog_localizes_and_only_exposes_active_safe_price_options(client):
    package, product, option = create_package()
    MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=6,
        monthly_price_vnd=800_000,
        is_active=False,
        sort_order=1,
    )
    response = client.get("/api/v1/public/membership-packages?locale=en")
    assert response.status_code == 200
    data = response.json()["data"][0]
    assert data["name"] == "CSA package"
    assert data["items"][0]["product_id"] == str(product.pk)
    assert data["price_options"] == [
        {
            "id": str(option.pk),
            "duration_months": 6,
            "monthly_price_vnd": "1000000",
            "total_price_vnd": "6000000",
        }
    ]
    assert "created_at" not in data
    assert "is_active" not in data["price_options"][0]


@pytest.mark.django_db
def test_public_catalog_orders_packages_by_created_at_then_id(client):
    oldest, _oldest_product, _oldest_option = create_package()
    tied_a, _tied_a_product, _tied_a_option = create_package()
    tied_b, _tied_b_product, _tied_b_option = create_package()
    now = timezone.now()
    oldest_at = now - timedelta(days=2)
    tied_at = now - timedelta(days=1)
    MembershipPackage.objects.filter(pk=oldest.pk).update(
        name="Zulu package",
        created_at=oldest_at,
    )
    MembershipPackage.objects.filter(pk__in=[tied_a.pk, tied_b.pk]).update(
        name="Alpha package",
        created_at=tied_at,
    )

    response = client.get("/api/v1/public/membership-packages?locale=vi")

    assert response.status_code == 200
    tied_ids = sorted([str(tied_a.pk), str(tied_b.pk)])
    assert [item["id"] for item in response.json()["data"]] == [
        str(oldest.pk),
        *tied_ids,
    ]


@pytest.mark.django_db
def test_manual_assignment_snapshots_bilingual_catalog_price_and_products(active_user):
    package, product, option = create_package(quota=3, duration=3, monthly_price=400_001)
    membership, _product, _option = assign(
        active_user,
        package=package,
        option=option,
        reference="  FARM-ORDER-001  ",
    )
    snapshot = membership.product_snapshots.get()
    assert membership.package_name_vi_snapshot == "Gói CSA"
    assert membership.package_name_en_snapshot == "CSA package"
    assert membership.package_description_vi_snapshot == "Giao mỗi chu kỳ"
    assert membership.package_description_en_snapshot == "One delivery per cycle"
    assert membership.duration_months_snapshot == 3
    assert membership.monthly_price_vnd_snapshot == 400_001
    assert membership.total_price_vnd_snapshot == 1_200_003
    assert membership.farmbrite_order_reference == "FARM-ORDER-001"
    assert membership.end_date == add_months(membership.start_date, 3)
    assert snapshot.product_id == product.pk
    assert snapshot.product_name_vi_snapshot == "Giỏ rau theo mùa"
    assert snapshot.product_name_en_snapshot == "Seasonal basket"
    assert snapshot.unit_size == Decimal("0.500")
    assert snapshot.unit_label_vi == snapshot.unit_label_en == "kg"
    assert snapshot.quota_units_per_cycle == 3


@pytest.mark.django_db
def test_assignment_rejects_wrong_or_inactive_package_or_option(active_user):
    package, _product, package_option = create_package()
    other_package, _other_product, other_option = create_package()
    with pytest.raises(PriceOptionUnavailable):
        assign_membership(
            customer_id=customer_for(active_user).pk,
            package_id=package.pk,
            price_option_id=other_option.pk,
            start_date=timezone.localdate(),
            farmbrite_order_reference="FB-WRONG-PACKAGE",
            actor=active_user,
        )
    package.is_active = False
    package.save(update_fields=["is_active", "updated_at"])
    with pytest.raises(PackageUnavailable):
        assign_membership(
            customer_id=customer_for(active_user).pk,
            package_id=package.pk,
            price_option_id=package_option.pk,
            start_date=timezone.localdate(),
            farmbrite_order_reference="FB-INACTIVE-PACKAGE",
            actor=active_user,
        )
    other_option.is_active = False
    other_option.save(update_fields=["is_active", "updated_at"])
    with pytest.raises(PriceOptionUnavailable):
        assign_membership(
            customer_id=customer_for(active_user).pk,
            package_id=other_package.pk,
            price_option_id=other_option.pk,
            start_date=timezone.localdate(),
            farmbrite_order_reference="FB-INACTIVE",
            actor=active_user,
        )


@pytest.mark.django_db
def test_price_total_overflow_returns_400_for_package_and_assignment(admin_api, active_user):
    product_id = admin_api.post(
        "/api/v1/admin/membership-products", product_api_payload(), format="json"
    ).json()["data"]["id"]
    overflow_monthly = MAX_POSITIVE_BIG_INTEGER // 120 + 1
    package_response = admin_api.post(
        "/api/v1/admin/membership-packages",
        package_api_payload(
            product_id,
            options=[
                {
                    "duration_months": 120,
                    "monthly_price_vnd": overflow_monthly,
                    "is_active": True,
                }
            ],
        ),
        format="json",
    )
    assert package_response.status_code == 400
    assert "monthly_price_vnd" in package_response.json()["price_options"][0]

    package, _product, option = create_package()
    customer = customer_for(active_user)
    patch_response = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package.pk}",
        {
            "price_options": [
                {
                    "id": str(option.pk),
                    "duration_months": 120,
                    "monthly_price_vnd": overflow_monthly,
                    "is_active": True,
                    "sort_order": 0,
                }
            ]
        },
        format="json",
    )
    assert patch_response.status_code == 400
    option.refresh_from_db()
    assert (option.duration_months, option.monthly_price_vnd) == (6, 1_000_000)

    overflow_option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=120,
        monthly_price_vnd=overflow_monthly,
        is_active=True,
    )
    response = admin_api.post(
        "/api/v1/admin/memberships",
        {
            "customer_id": str(customer.pk),
            "package_id": str(package.pk),
            "price_option_id": str(overflow_option.pk),
            "start_date": timezone.localdate().isoformat(),
            "farmbrite_order_reference": "FB-OVERFLOW",
        },
        format="json",
    )
    assert response.status_code == 400
    assert "price_option_id" in response.json()
    assert not Membership.objects.filter(customer__user=active_user).exists()

    with pytest.raises(PriceOptionTotalInvalid):
        assign_membership(
            customer_id=customer.pk,
            package_id=package.pk,
            price_option_id=overflow_option.pk,
            start_date=timezone.localdate(),
            farmbrite_order_reference="FB-OVERFLOW-SERVICE",
            actor=active_user,
        )


@pytest.mark.django_db
def test_assignment_rejects_duplicate_reference_case_insensitively(active_user):
    package, _product, option = create_package()
    assign(active_user, package=package, option=option, reference="Farmbrite-ABC")
    other = create_active_user("farmbrite-duplicate@example.com")
    with pytest.raises(DuplicateFarmbriteOrderReference):
        assign(other, package=package, option=option, reference=" farmbrite-abc ")


@pytest.mark.django_db
def test_assignment_dates_allow_today_and_future_but_reject_past(active_user):
    package, _product, option = create_package(duration=3)
    past = timezone.localdate() - timedelta(days=1)
    with pytest.raises(StartDateInPast):
        assign_membership(
            customer_id=customer_for(active_user).pk,
            package_id=package.pk,
            price_option_id=option.pk,
            start_date=past,
            farmbrite_order_reference="FB-PAST",
            actor=active_user,
        )
    today_membership, _product, _option = assign(
        active_user, package=package, option=option, reference="FB-TODAY"
    )
    assert today_membership.status == MembershipStatus.ACTIVE

    future_user = create_active_user("future-membership@example.com")
    future = timezone.localdate() + timedelta(days=10)
    future_membership, _product, _option = assign(
        future_user,
        package=package,
        option=option,
        reference="FB-FUTURE",
        start_date=future,
    )
    assert future_membership.status == MembershipStatus.SCHEDULED
    assert not future_membership.cycles.exists()


@pytest.mark.django_db
def test_customer_memberships_cannot_overlap(active_user):
    package, _product, option = create_package()
    assign(active_user, package=package, option=option, reference="FB-FIRST")
    with pytest.raises(MembershipOverlap):
        assign(active_user, package=package, option=option, reference="FB-SECOND")
    assert Membership.objects.filter(customer__user=active_user).count() == 1


@pytest.mark.django_db
def test_live_catalog_and_price_changes_do_not_change_membership_snapshot(active_user):
    package, product, option = create_package(quota=3)
    membership, _product, _option = assign(
        active_user, package=package, option=option, reference="FB-IMMUTABLE"
    )
    package.translations.update(name="Changed package", description="Changed")
    product.translations.update(name="Changed product")
    Product.objects.filter(pk=product.pk).update(
        unit_size=Decimal("9.000"), unit_label_vi="túi", unit_label_en="bags"
    )
    option.is_active = False
    option.save(update_fields=["is_active", "updated_at"])
    membership.refresh_from_db()
    snapshot = membership.product_snapshots.get()
    assert membership.package_name_vi_snapshot == "Gói CSA"
    assert membership.package_name_en_snapshot == "CSA package"
    assert membership.duration_months_snapshot == 6
    assert membership.monthly_price_vnd_snapshot == 1_000_000
    assert membership.total_price_vnd_snapshot == 6_000_000
    assert snapshot.product_name_vi_snapshot == "Giỏ rau theo mùa"
    assert snapshot.product_name_en_snapshot == "Seasonal basket"
    assert snapshot.unit_size == Decimal("0.500")


@pytest.mark.django_db
def test_admin_assignment_api_rejects_unknown_fields_and_returns_snapshot(
    admin_api, active_user, manual_payment_settings
):
    package, _product, option = create_package()
    customer = customer_for(active_user)
    payload = {
        "customer_id": str(customer.pk),
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "start_date": timezone.localdate().isoformat(),
        "farmbrite_order_reference": "FB-API-001",
    }
    forged = admin_api.post(
        "/api/v1/admin/memberships", {**payload, "total_price_vnd": 1}, format="json"
    )
    assert forged.status_code == 400
    assert forged.json()["total_price_vnd"] == "This field is not allowed."

    response = admin_api.post("/api/v1/admin/memberships", payload, format="json")
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["customer"]["user"]["id"] == str(active_user.pk)
    assert data["price_option_id"] == str(option.pk)
    assert data["price_option"] == {
        "duration_months": 6,
        "monthly_price_vnd": "1000000",
        "total_price_vnd": "6000000",
    }
    assert data["farmbrite_order_reference"] == "FB-API-001"
    contract = CSAContract.objects.get(membership_id=data["id"])
    assert data["contract"] == {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "status": "active",
    }
    assert contract.source == CSAContractSource.ADMIN_MANUAL
    assert contract.purchase_request_id is None
    assert contract.terms_acceptance_method == CSAContractTermsAcceptanceMethod.IN_STORE
    assert contract.member_name_snapshot == customer.name
    assert contract.package_name_snapshot == data["package_name"]
    assert contract.quota_snapshot["policy"] == package.quota_policy
    assert contract.payment_snapshot == {
        "bank_bin": manual_payment_settings.bank_bin,
        "bank_name": manual_payment_settings.bank_name,
        "account_number": manual_payment_settings.account_number,
        "account_name": manual_payment_settings.account_name,
        "transfer_content": "FB-API-001",
        "payment_config_version": manual_payment_settings.version,
    }
    assert contract.terms_snapshot_vi and contract.terms_snapshot_en
    assert contract.pdf_vi_storage_key and contract.pdf_en_storage_key
    original_snapshot = (
        contract.member_name_snapshot,
        contract.payment_snapshot.copy(),
        contract.pdf_vi_sha256,
        contract.pdf_en_sha256,
    )
    customer.name = "Changed live customer"
    customer.save(update_fields=["name", "updated_at"])
    manual_payment_settings.account_number = "987654321"
    manual_payment_settings.save(update_fields=["account_number", "updated_at"])
    contract.refresh_from_db()
    assert (
        contract.member_name_snapshot,
        contract.payment_snapshot,
        contract.pdf_vi_sha256,
        contract.pdf_en_sha256,
    ) == original_snapshot
    assignment_log = AuditLog.objects.get(action="membership.assigned")
    assert assignment_log.context["contract_id"] == str(contract.pk)
    assert assignment_log.context["contract_reference_code"] == contract.reference_code


@pytest.mark.django_db
def test_manual_assignment_pdf_failure_rolls_back_and_retry_is_clean(
    admin_api, active_user, manual_payment_settings, monkeypatch
):
    package, _product, option = create_package()
    customer = customer_for(active_user)
    payload = {
        "customer_id": str(customer.pk),
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "start_date": timezone.localdate().isoformat(),
        "farmbrite_order_reference": "FB-PDF-RETRY",
    }
    from memberships import services
    from memberships.contract_pdfs import CSAContractPDFStorageError

    original_generator = services.generate_contract_documents

    def fail_pdf(*_args, **_kwargs):
        raise CSAContractPDFStorageError("Simulated storage failure")

    monkeypatch.setattr(services, "generate_contract_documents", fail_pdf)
    failed = admin_api.post("/api/v1/admin/memberships", payload, format="json")

    assert failed.status_code == 503
    assert failed.json()["errors"] == [{"code": "csa_contract_pdf_storage_error"}]
    assert not Membership.objects.filter(customer=customer).exists()
    assert not CSAContract.objects.exists()
    assert not AuditLog.objects.filter(action="membership.assigned").exists()

    monkeypatch.setattr(services, "generate_contract_documents", original_generator)
    retried = admin_api.post("/api/v1/admin/memberships", payload, format="json")
    assert retried.status_code == 201
    assert Membership.objects.filter(customer=customer).count() == 1
    assert CSAContract.objects.count() == 1
    assert AuditLog.objects.filter(action="membership.assigned").count() == 1


@pytest.mark.django_db
def test_self_current_quota_and_usage_use_immutable_snapshot(active_user, access_token):
    membership, product, _option = assign(active_user, reference="FB-SELF")
    create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 1}],
    )
    client = bearer_client(access_token)
    current = client.get("/api/v1/memberships/current?locale=en")
    quota = client.get("/api/v1/memberships/quota?locale=en")
    usage = client.get("/api/v1/memberships/usage?locale=en&page=1")
    assert current.status_code == quota.status_code == usage.status_code == 200
    assert current.json()["data"]["package_name"] == "CSA package"
    assert current.json()["data"]["price_option"]["total_price_vnd"] == "6000000"
    assert quota.json()["data"]["products"][0]["remaining_units"] == 1
    assert usage.json()["data"][0]["lines"][0]["product_name"] == "Seasonal basket"
    assert current["Cache-Control"] == "private, no-store"


@pytest.mark.django_db
def test_usage_and_undo_deduct_and_restore_exact_units(active_user):
    membership, product, _option = assign(active_user)
    key = uuid.uuid4()
    order, created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=key,
        lines=[{"product_id": product.pk, "units": 2}],
    )
    replay, replay_created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=key,
        lines=[{"product_id": product.pk, "units": 2}],
    )
    assert created is True and replay_created is False and replay.pk == order.pk
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == 0
    with pytest.raises(InsufficientQuota):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": product.pk, "units": 1}],
        )
    undo_usage_order(order_id=order.pk, actor=active_user, reason="Wrong delivery")
    quota.refresh_from_db()
    order.refresh_from_db()
    assert quota.remaining_units == 2
    assert order.status == UsageOrderStatus.REVERSED
    assert MembershipUsageReversal.objects.get(order=order).reason == "Wrong delivery"
    with pytest.raises(UsageUndoConflict):
        undo_usage_order(order_id=order.pk, actor=active_user, reason="Again")


@pytest.mark.django_db
def test_rollover_allocates_fifo_and_undo_restores_each_cycle(active_user):
    membership, product, _option = assign_with_past_start(
        active_user, policy=QuotaPolicy.ROLLOVER, quota=2
    )
    ensure_due_cycles(membership)
    order, _created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 3}],
    )
    allocations = list(
        MembershipUsageAllocation.objects.filter(line__order=order).order_by(
            "cycle_quota__cycle__sequence"
        )
    )
    assert [(item.cycle_quota.cycle.sequence, item.units) for item in allocations] == [
        (1, 2),
        (2, 1),
    ]
    undo_usage_order(order_id=order.pk, actor=active_user, reason="Wrong order")
    assert list(
        MembershipCycleQuota.objects.filter(cycle__membership=membership)
        .order_by("cycle__sequence")
        .values_list("remaining_units", flat=True)
    ) == [2, 2]


@pytest.mark.django_db
def test_usage_rejects_other_product_and_corrupted_membership_user(active_user):
    membership, _product, _option = assign(active_user)
    _package, other_product, _option = create_package()
    with pytest.raises(ProductNotInMembership):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": other_product.pk, "units": 1}],
        )
    product = membership.product_snapshots.get().product
    order, _created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 1}],
    )
    other_customer = create_managed_csa_customer(
        name="Other customer",
        phone="0909999999",
        province_code="66",
        ward_code="22015",
        address="Số 20",
    )
    MembershipUsageOrder.objects.filter(pk=order.pk).update(customer=other_customer)
    with pytest.raises(UsageIntegrityConflict):
        undo_usage_order(order_id=order.pk, actor=active_user, reason="Must fail")


@pytest.mark.django_db
def test_cycle_anniversary_and_future_status(active_user):
    future = timezone.localdate() + timedelta(days=2)
    membership, _product, _option = assign(active_user, start_date=future, reference="FB-SCHEDULED")
    assert membership.status == MembershipStatus.SCHEDULED
    assert ensure_due_cycles(membership, as_of=future - timedelta(days=1)) == []
    membership = sync_membership_status(membership, as_of=future)
    ensure_due_cycles(membership, as_of=future)
    assert membership.cycles.count() == 1
    membership = sync_membership_status(membership, as_of=membership.end_date)
    assert membership.status == MembershipStatus.ENDED
    assert membership.current_slot is None
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


@pytest.mark.django_db
def test_old_request_and_payment_routes_are_removed(admin_api):
    paths = [
        "/api/v1/memberships/requests",
        f"/api/v1/memberships/requests/{uuid.uuid4()}/payment-submitted",
        "/api/v1/public/membership-payment-availability",
        "/api/v1/admin/membership-requests",
        f"/api/v1/admin/membership-requests/{uuid.uuid4()}/approve",
    ]
    for path in paths:
        assert admin_api.get(path).status_code == 404
        assert admin_api.post(path, {}, format="json").status_code == 404


def test_legacy_price_migration_refuses_non_divisible_money():
    migration = importlib.import_module(
        "memberships.migrations.0008_farmbrite_memberships_and_price_options"
    )
    assert (
        migration.exact_monthly_price(total=1_200_000, duration=3, identity="test package")
        == 400_000
    )
    with pytest.raises(RuntimeError, match="not exactly divisible"):
        migration.exact_monthly_price(total=1_000_001, duration=3, identity="test package")


@pytest.mark.django_db(transaction=True)
def test_price_option_migration_preserves_historical_membership_price_and_usage():
    migrate_from = (
        "memberships",
        "0007_membership_package_description_en_snapshot_and_more",
    )
    migrate_to = (
        "memberships",
        "0008_farmbrite_memberships_and_price_options",
    )
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([migrate_from])
        old_apps = executor.loader.project_state([migrate_from]).apps
        LegacyUser = old_apps.get_model("identity", "User")
        LegacyProduct = old_apps.get_model("memberships", "Product")
        LegacyPackage = old_apps.get_model("memberships", "MembershipPackage")
        LegacyPackageItem = old_apps.get_model("memberships", "MembershipPackageItem")
        LegacyRequest = old_apps.get_model("memberships", "MembershipRequest")
        LegacyMembership = old_apps.get_model("memberships", "Membership")
        LegacySnapshot = old_apps.get_model("memberships", "MembershipProductSnapshot")
        LegacyCycle = old_apps.get_model("memberships", "MembershipCycle")
        LegacyQuota = old_apps.get_model("memberships", "MembershipCycleQuota")
        LegacyUsage = old_apps.get_model("memberships", "MembershipUsageOrder")
        LegacyUsageLine = old_apps.get_model("memberships", "MembershipUsageLine")
        LegacyAllocation = old_apps.get_model("memberships", "MembershipUsageAllocation")

        user = LegacyUser.objects.create(
            email="legacy-price@example.com",
            name="Legacy member",
            password="!",
            status="active",
            is_active=True,
        )
        product = LegacyProduct.objects.create(
            name="Legacy basket",
            short_description="Historical product",
            unit_size=Decimal("0.500"),
            unit_label_vi="kg",
            unit_label_en="kg",
        )
        package = LegacyPackage.objects.create(
            name="Legacy package",
            description="Live package price",
            upfront_price=Decimal("4500000"),
            duration_months=6,
            quota_policy="rollover",
        )
        LegacyPackageItem.objects.create(package=package, product=product, quota_units=2)
        request = LegacyRequest.objects.create(
            short_code="LEGACY01",
            user=user,
            package=package,
            package_name_snapshot="Legacy package snapshot",
            package_description_snapshot="Historical description",
            package_name_vi_snapshot="Gói lịch sử",
            package_name_en_snapshot="Historical package",
            package_description_vi_snapshot="Mô tả lịch sử",
            package_description_en_snapshot="Historical description",
            upfront_price_snapshot=Decimal("4614000"),
            duration_months_snapshot=6,
            quota_policy_snapshot="rollover",
            phone_snapshot="0901234567",
            status="approved",
            open_slot=None,
            approved_at=timezone.now(),
        )
        membership = LegacyMembership.objects.create(
            user=user,
            request=request,
            package=package,
            package_name_snapshot="Legacy package snapshot",
            package_description_snapshot="Historical description",
            package_name_vi_snapshot="Gói lịch sử",
            package_name_en_snapshot="Historical package",
            package_description_vi_snapshot="Mô tả lịch sử",
            package_description_en_snapshot="Historical description",
            upfront_price_snapshot=Decimal("4614000"),
            duration_months_snapshot=6,
            quota_policy_snapshot="rollover",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 7, 1),
            status="active",
            current_slot=True,
            activated_by=user,
        )
        snapshot = LegacySnapshot.objects.create(
            membership=membership,
            product=product,
            product_name="Legacy basket snapshot",
            product_name_vi_snapshot="Giỏ lịch sử",
            product_name_en_snapshot="Historical basket",
            unit_size=Decimal("0.500"),
            unit_label_vi="kg",
            unit_label_en="kg",
            quota_units_per_cycle=2,
        )
        cycle = LegacyCycle.objects.create(
            membership=membership,
            sequence=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 2, 1),
        )
        quota = LegacyQuota.objects.create(
            cycle=cycle,
            product_snapshot=snapshot,
            granted_units=2,
            remaining_units=1,
            expired_units=0,
        )
        usage = LegacyUsage.objects.create(
            membership=membership,
            user=user,
            idempotency_key=uuid.uuid4(),
            status="applied",
            note="Historical delivery",
            created_by=user,
        )
        line = LegacyUsageLine.objects.create(order=usage, product_snapshot=snapshot, units=1)
        LegacyAllocation.objects.create(line=line, cycle_quota=quota, units=1)

        executor = MigrationExecutor(connection)
        executor.migrate([migrate_to])
        new_apps = executor.loader.project_state([migrate_to]).apps
        PriceOption = new_apps.get_model("memberships", "MembershipPackagePriceOption")
        MigratedMembership = new_apps.get_model("memberships", "Membership")
        MigratedQuota = new_apps.get_model("memberships", "MembershipCycleQuota")
        MigratedUsage = new_apps.get_model("memberships", "MembershipUsageOrder")
        MigratedAllocation = new_apps.get_model("memberships", "MembershipUsageAllocation")

        options = list(
            PriceOption.objects.filter(package_id=package.pk).order_by(
                "-is_active", "monthly_price_vnd"
            )
        )
        assert [
            (
                item.duration_months,
                item.monthly_price_vnd,
                item.is_active,
                item.active_duration_key,
            )
            for item in options
        ] == [
            (6, 750_000, True, 6),
            (6, 769_000, False, None),
        ]
        migrated = MigratedMembership.objects.get(pk=membership.pk)
        historical_option = PriceOption.objects.get(
            package_id=package.pk,
            duration_months=6,
            monthly_price_vnd=769_000,
        )
        assert migrated.price_option_id == historical_option.pk
        assert migrated.monthly_price_vnd_snapshot == 769_000
        assert migrated.total_price_vnd_snapshot == 4_614_000
        assert migrated.package_name_vi_snapshot == "Gói lịch sử"
        assert migrated.product_snapshots.get().product_name_en_snapshot == ("Historical basket")
        assert MigratedQuota.objects.get(pk=quota.pk).remaining_units == 1
        assert MigratedUsage.objects.get(pk=usage.pk).note == "Historical delivery"
        assert MigratedAllocation.objects.get(line__order_id=usage.pk).units == 1
    finally:
        MigrationExecutor(connection).migrate([migrate_to])


@pytest.mark.django_db
def test_scheduling_has_no_single_current_membership_constraint():
    assert "memberships_one_current_per_customer" not in {
        item.name for item in Membership._meta.constraints
    }


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_manual_assignment_creates_only_one_current_membership():
    user = create_active_user("concurrent-assignment@example.com")
    customer = customer_for(user)
    package, _product, option = create_package()
    barrier = threading.Barrier(3)
    outcomes = []

    def assign_once(reference):
        close_old_connections()
        try:
            barrier.wait()
            assign_membership(
                customer_id=customer.pk,
                package_id=package.pk,
                price_option_id=option.pk,
                start_date=timezone.localdate(),
                farmbrite_order_reference=reference,
                actor=User.objects.get(pk=user.pk),
            )
            outcomes.append("created")
        except MembershipOverlap:
            outcomes.append("overlap")
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=assign_once, args=(reference,))
        for reference in ("FB-CONCURRENT-1", "FB-CONCURRENT-2")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["created", "overlap"]
    assert Membership.objects.filter(customer__user=user, current_slot=True).count() == 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_usage_cannot_overdraw_quota():
    user = create_active_user("concurrent-usage@example.com")
    membership, product, _option = assign(user)
    barrier = threading.Barrier(3)
    outcomes = []

    def use_quota():
        close_old_connections()
        try:
            barrier.wait()
            create_usage_order(
                membership_id=membership.pk,
                actor=User.objects.get(pk=user.pk),
                idempotency_key=uuid.uuid4(),
                lines=[{"product_id": product.pk, "units": 2}],
            )
            outcomes.append("created")
        except InsufficientQuota:
            outcomes.append("insufficient")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=use_quota) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["created", "insufficient"]
    assert MembershipCycleQuota.objects.get(cycle__membership=membership).remaining_units == 0


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_current_and_quota_views_initialize_cycles_once(active_user, access_token):
    membership, _product, _option = assign(active_user)
    MembershipCycleQuota.objects.filter(cycle__membership=membership).delete()
    membership.cycles.all().delete()
    barrier = threading.Barrier(3)
    outcomes = []

    def load(path):
        close_old_connections()
        try:
            barrier.wait()
            outcomes.append((path, bearer_client(access_token).get(path).status_code))
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=load, args=(path,))
        for path in ("/api/v1/memberships/current", "/api/v1/memberships/quota")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [
        ("/api/v1/memberships/current", 200),
        ("/api/v1/memberships/quota", 200),
    ]
    assert list(membership.cycles.values_list("sequence", flat=True)) == [1]
    assert MembershipCycleQuota.objects.filter(cycle__membership=membership).count() == 1


@pytest.mark.django_db
def test_revoke_active_membership_succeeds(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-001")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {"reason": "Gán nhầm"}, format="json")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "revoked"
    assert data["revoked_at"] is not None
    assert data["revoke_reason"] == "Gán nhầm"
    membership.refresh_from_db()
    assert membership.current_slot is None


@pytest.mark.django_db
def test_revoke_scheduled_membership_succeeds(active_user, admin_api):
    tomorrow = timezone.localdate() + timedelta(days=1)
    membership, _product, _option = assign(
        active_user, reference="REVOKE-SCH-001", start_date=tomorrow
    )
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {"reason": "Gán nhầm"}, format="json")
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "revoked"


@pytest.mark.django_db
def test_revoke_already_revoked_is_rejected(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-002")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    admin_api.post(url, {"reason": "first"}, format="json")
    response = admin_api.post(url, {"reason": "second"}, format="json")
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "membership_not_revocable"


@pytest.mark.django_db
def test_revoke_ended_membership_is_rejected(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-003")
    Membership.objects.filter(pk=membership.pk).update(
        status=MembershipStatus.ENDED,
        current_slot=None,
    )
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {"reason": "too late"}, format="json")
    assert response.status_code == 409


@pytest.mark.django_db
def test_revoke_stale_active_membership_past_end_transitions_to_ended(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-STALE-ENDED")
    today = timezone.localdate()
    Membership.objects.filter(pk=membership.pk).update(
        start_date=add_months(today, -1),
        end_date=today,
        status=MembershipStatus.ACTIVE,
        current_slot=True,
    )

    response = admin_api.post(
        f"/api/v1/admin/memberships/{membership.pk}/revoke",
        {"reason": "Too late"},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "membership_not_revocable"}]
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ENDED
    assert membership.current_slot is None
    assert membership.revoked_at is None
    assert membership.revoked_by_id is None
    assert membership.revoke_reason == ""


@pytest.mark.django_db
def test_revoke_empty_reason_is_rejected(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-004")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {"reason": "   "}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_revoke_missing_reason_is_rejected(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-005")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_revoke_reason_too_long_is_rejected(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-006")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {"reason": "x" * 501}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_revoke_nonexistent_membership_returns_404(admin_api):
    url = "/api/v1/admin/memberships/00000000-0000-0000-0000-000000000000/revoke"
    response = admin_api.post(url, {"reason": "test"}, format="json")
    assert response.status_code == 404


@pytest.mark.django_db
def test_revoke_frees_slot_for_new_assignment(active_user, admin_api):
    """After revoke, user can receive a new membership with a new UUID."""
    package, product, option = create_package()
    membership, _product, _option = assign(
        active_user, package=package, option=option, reference="REVOKE-007"
    )
    old_id = membership.pk
    url = f"/api/v1/admin/memberships/{old_id}/revoke"
    admin_api.post(url, {"reason": "Gán nhầm"}, format="json")

    new_membership, _product, _option = assign(
        active_user, package=package, option=option, reference="REVOKE-NEW-001"
    )
    assert new_membership.pk != old_id
    assert new_membership.status == MembershipStatus.ACTIVE


@pytest.mark.django_db
def test_revoke_does_not_alter_usage_history(active_user, admin_api):
    membership, product, _option = assign(active_user, reference="REVOKE-008")
    idempotency_key = uuid.uuid4()
    order, created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=idempotency_key,
        lines=[{"product_id": product.pk, "units": 1}],
        note="Historical delivery",
    )
    assert created is True
    line = MembershipUsageLine.objects.get(order=order)
    allocation = MembershipUsageAllocation.objects.get(line=line)
    quota = allocation.cycle_quota
    before = {
        "order": (order.status, order.note, order.reversed_at),
        "line": (line.product_snapshot_id, line.units),
        "allocation": (allocation.cycle_quota_id, allocation.units),
        "quota": (quota.granted_units, quota.remaining_units, quota.expired_units),
    }

    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    response = admin_api.post(url, {"reason": "test"}, format="json")
    assert response.status_code == 200

    usage_payload = {
        "membership_id": str(membership.pk),
        "lines": [{"product_id": str(product.pk), "units": 1}],
        "note": "Historical delivery",
    }
    replay = admin_api.post(
        "/api/v1/admin/membership-usage",
        {**usage_payload, "idempotency_key": str(idempotency_key)},
        format="json",
    )
    fresh = admin_api.post(
        "/api/v1/admin/membership-usage",
        {**usage_payload, "idempotency_key": str(uuid.uuid4())},
        format="json",
    )
    assert replay.status_code == fresh.status_code == 409
    assert replay.json()["errors"] == fresh.json()["errors"] == [{"code": "invalid_quota_usage"}]

    membership.refresh_from_db()
    order.refresh_from_db()
    line.refresh_from_db()
    allocation.refresh_from_db()
    quota.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED
    assert membership.revoke_reason == "test"
    assert MembershipUsageOrder.objects.filter(pk=order.pk).exists()
    assert MembershipUsageLine.objects.filter(pk=line.pk).exists()
    assert MembershipUsageAllocation.objects.filter(pk=allocation.pk).exists()
    assert MembershipCycleQuota.objects.filter(pk=quota.pk).exists()
    assert MembershipUsageOrder.objects.filter(membership=membership).count() == 1
    assert MembershipUsageLine.objects.filter(order=order).count() == 1
    assert MembershipUsageAllocation.objects.filter(line=line).count() == 1
    assert (order.status, order.note, order.reversed_at) == before["order"]
    assert (line.product_snapshot_id, line.units) == before["line"]
    assert (allocation.cycle_quota_id, allocation.units) == before["allocation"]
    assert (quota.granted_units, quota.remaining_units, quota.expired_units) == before["quota"]


@pytest.mark.django_db
def test_revoked_membership_is_terminal_when_cycles_are_ensured(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-TERMINAL")
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    original_quota = (quota.granted_units, quota.remaining_units, quota.expired_units)
    assert (
        admin_api.post(
            f"/api/v1/admin/memberships/{membership.pk}/revoke",
            {"reason": "terminal"},
            format="json",
        ).status_code
        == 200
    )

    assert ensure_due_cycles(membership, as_of=membership.end_date) == []
    membership.refresh_from_db()
    quota.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED
    assert membership.current_slot is None
    assert membership.cycles.count() == 1
    assert (quota.granted_units, quota.remaining_units, quota.expired_units) == original_quota


@pytest.mark.django_db
def test_admin_membership_detail_does_not_revive_revoked_membership(active_user, admin_api):
    membership, _product, _option = assign(active_user, reference="REVOKE-DETAIL")
    assert (
        admin_api.post(
            f"/api/v1/admin/memberships/{membership.pk}/revoke",
            {"reason": "Keep the audit trail"},
            format="json",
        ).status_code
        == 200
    )
    membership.refresh_from_db()
    revoked_at = membership.revoked_at.isoformat()

    response = admin_api.get(f"/api/v1/admin/memberships/{membership.pk}?locale=en")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == MembershipStatus.REVOKED
    assert data["current_slot"] is None
    assert data["revoked_at"] == revoked_at
    assert data["revoked_by"] == {
        "id": str(active_user.pk),
        "name": active_user.name,
        "email": active_user.email,
    }
    assert set(data["revoked_by"]) == {"id", "name", "email"}
    assert data["revoke_reason"] == "Keep the audit trail"
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED
    assert membership.current_slot is None
    assert membership.revoked_at.isoformat() == revoked_at
    assert membership.revoke_reason == "Keep the audit trail"


@pytest.mark.django_db
def test_admin_usage_undo_rejects_revoked_membership_without_mutating_history(
    active_user, admin_api
):
    membership, product, _option = assign(active_user, reference="REVOKE-UNDO")
    order, _created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 1}],
        note="Closed audit record",
    )
    line = MembershipUsageLine.objects.get(order=order)
    allocation = MembershipUsageAllocation.objects.get(line=line)
    quota = allocation.cycle_quota
    before = {
        "order": (order.status, order.reversed_at),
        "line": (line.product_snapshot_id, line.units),
        "allocation": (allocation.cycle_quota_id, allocation.units),
        "quota": (quota.granted_units, quota.remaining_units, quota.expired_units),
    }
    assert (
        admin_api.post(
            f"/api/v1/admin/memberships/{membership.pk}/revoke",
            {"reason": "Close audit record"},
            format="json",
        ).status_code
        == 200
    )

    response = admin_api.post(
        f"/api/v1/admin/membership-usage/{order.pk}/undo",
        {"reason": "Stale open dialog"},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "usage_undo_conflict"}]
    order.refresh_from_db()
    line.refresh_from_db()
    allocation.refresh_from_db()
    quota.refresh_from_db()
    assert not MembershipUsageReversal.objects.filter(order=order).exists()
    assert (order.status, order.reversed_at) == before["order"]
    assert (line.product_snapshot_id, line.units) == before["line"]
    assert (allocation.cycle_quota_id, allocation.units) == before["allocation"]
    assert (quota.granted_units, quota.remaining_units, quota.expired_units) == before["quota"]


@pytest.mark.django_db
def test_revoked_membership_not_in_current_quota(active_user, admin_api, access_token):
    membership, _product, _option = assign(active_user, reference="REVOKE-009")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    admin_api.post(url, {"reason": "test"}, format="json")
    member_client = bearer_client(access_token)
    for path in ("/api/v1/memberships/current?locale=vi", "/api/v1/memberships/quota?locale=vi"):
        response = member_client.get(path)
        assert response.status_code == 200
        assert response.json()["data"] is None


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_revoke_only_commits_once():
    user = create_active_user("concurrent-revoke@example.com")
    actor = create_active_user("concurrent-revoke-admin@example.com")
    membership, _product, _option = assign(user, reference="REVOKE-CONCURRENT")
    barrier = threading.Barrier(3)
    outcomes = []

    def revoke_once(reason):
        close_old_connections()
        try:
            barrier.wait()
            revoke_membership(
                membership_id=membership.pk,
                reason=reason,
                actor=User.objects.get(pk=actor.pk),
            )
            outcomes.append("revoked")
        except MembershipNotRevocable:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=revoke_once, args=(reason,)) for reason in ("first", "second")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "revoked"]
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.REVOKED
    assert membership.current_slot is None
    assert membership.revoke_reason in {"first", "second"}


@pytest.mark.django_db
def test_non_admin_cannot_revoke(active_user, access_token):
    membership, _product, _option = assign(active_user, reference="REVOKE-010")
    url = f"/api/v1/admin/memberships/{membership.pk}/revoke"
    member_client = bearer_client(access_token)
    response = member_client.post(url, {"reason": "test"}, format="json")
    assert response.status_code in [401, 403]


@pytest.mark.django_db
def test_reassign_same_package_creates_new_uuid(active_user, admin_api):
    """After revoke, reassigning the same package creates a new membership UUID."""
    package, product, option = create_package()
    membership, _product, _option = assign(
        active_user, package=package, option=option, reference="REVOKE-011"
    )
    old_id = membership.pk
    revoke_url = f"/api/v1/admin/memberships/{old_id}/revoke"
    admin_api.post(revoke_url, {"reason": "test"}, format="json")

    new_membership, _product, _option = assign(
        active_user,
        package=package,
        option=option,
        reference="REVOKE-SAME-PACKAGE-001",
    )
    assert new_membership.pk != old_id

    membership.refresh_from_db()
    assert membership.status == "revoked"


@pytest.mark.django_db
def test_reassign_creates_fresh_quota(active_user, admin_api):
    """New membership after revoke gets its own quota cycle, not the old one's."""
    package, product, option = create_package()
    membership, _product, _option = assign(
        active_user, package=package, option=option, reference="REVOKE-012"
    )
    old_id = membership.pk
    revoke_url = f"/api/v1/admin/memberships/{old_id}/revoke"
    admin_api.post(revoke_url, {"reason": "test"}, format="json")

    new_membership, _product, _option = assign(
        active_user,
        package=package,
        option=option,
        reference="REVOKE-NEW-QUOTA-001",
    )

    old_cycles = set(
        MembershipCycle.objects.filter(membership_id=old_id).values_list("pk", flat=True)
    )
    new_cycles = set(
        MembershipCycle.objects.filter(membership_id=new_membership.pk).values_list("pk", flat=True)
    )
    assert old_cycles.isdisjoint(new_cycles)
