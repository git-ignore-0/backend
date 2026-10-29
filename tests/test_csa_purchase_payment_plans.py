"""Payment-plan selection and immutable snapshots for public CSA purchases."""

import threading
import uuid
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from commerce.models import ShippingAddress, ShippingArea
from memberships.models import (
    CSAPurchaseRequest,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    MembershipPaymentType,
    Product,
    QuotaPolicy,
)
from memberships.purchases import update_membership_payment_settings

CREATE_URL = "/api/v1/public/csa-purchase-requests"
CATALOG_URL = "/api/v1/public/membership-packages"
ADMIN_URL = "/api/v1/admin/csa-purchase-requests"
BANK = {"bin": "970436", "name": "Vietcombank"}


@pytest.fixture(autouse=True)
def bank_directory(monkeypatch, db):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [BANK])
    ShippingArea.objects.update_or_create(
        ward_code="22015",
        defaults={"province_code": "66", "shipping_fee_vnd": 30_000, "is_active": True},
    )
    yield
    cache.clear()


@pytest.fixture
def catalog():
    package = MembershipPackage.objects.create(name="CSA 3 months", quota_policy=QuotaPolicy.EXPIRE)
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=400_000
    )
    product = Product.objects.create(name="Vegetables", unit_size=Decimal("1"))
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=1)
    return package, option


@pytest.fixture
def payment_settings():
    return update_membership_payment_settings(
        bank_bin=BANK["bin"],
        bank_name=BANK["name"],
        account_number="123456789",
        account_name="NFV",
        direct_transfer_enabled=True,
    )


@pytest.fixture
def guest_payload(catalog):
    package, option = catalog
    return {
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "name": "Buyer",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "12 Main Street",
        "terms_accepted": True,
    }


def make_plan(option, *, payment_type=MembershipPaymentType.FULL, active=True, order=0):
    count = 1 if payment_type == MembershipPaymentType.FULL else 2
    amount = Decimal("1100000") if count == 1 else Decimal("1300000")
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Pay in full" if count == 1 else "Pay twice",
        payment_type=payment_type,
        installment_count=count,
        total_amount=amount,
        is_active=active,
        sort_order=order,
    )
    values = (
        [(1, amount, 3)] if count == 1 else [(1, Decimal("500000"), 1), (2, Decimal("800000"), 2)]
    )
    for sequence, installment_amount, cycles in values:
        MembershipPaymentPlanInstallment.objects.create(
            payment_plan=plan,
            sequence=sequence,
            amount=installment_amount,
            cycle_count=cycles,
        )
    return plan


def post_purchase(payload):
    return APIClient().post(CREATE_URL, payload, format="json")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payment_type", [MembershipPaymentType.FULL, MembershipPaymentType.INSTALLMENT]
)
def test_request_snapshots_selected_plan_and_uses_plan_amount(
    catalog, payment_settings, guest_payload, payment_type
):
    _package, option = catalog
    plan = make_plan(option, payment_type=payment_type)
    response = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk), "amount": "1"})

    assert response.status_code == 201
    data = response.json()["data"]
    item = CSAPurchaseRequest.objects.get(pk=data["id"])
    assert item.payment_plan_id == plan.pk
    assert item.amount_snapshot == int(plan.total_amount) != option.total_price_vnd
    assert item.total_payment_amount_snapshot == plan.total_amount
    assert item.payment_plan_name_snapshot == plan.name
    assert item.payment_type_snapshot == payment_type
    assert item.installment_count_snapshot == plan.installment_count
    assert item.payment_installments_snapshot == data["payment_plan"]["installments"]
    assert data["payment_plan"] == {
        "id": str(plan.pk),
        "name": plan.name,
        "payment_type": payment_type,
        "total_amount": format(plan.total_amount, "f"),
        "installment_count": plan.installment_count,
        "initial_payment_amount": format(
            plan.installments.order_by("sequence").first().amount, "f"
        ),
        "installments": [
            {
                "sequence": line.sequence,
                "amount": format(line.amount, "f"),
                "cycle_count": line.cycle_count,
            }
            for line in plan.installments.all()
        ],
    }
    assert data["amount"] == format(plan.total_amount, "f")
    assert (
        data["initial_payment_amount"]
        == data["qr_payload"]["amount"]
        == format(plan.installments.order_by("sequence").first().amount, "f")
    )
    assert data["qr_payload"]["amount"] != "1"
    assert "guest_confirmation_token" in data
    assert not any(
        key in data
        for key in ("guest_token_hash", "terms_acceptance_ip", "terms_acceptance_user_agent")
    )


@pytest.mark.django_db
def test_request_snapshot_and_admin_detail_do_not_read_changed_live_plan(
    catalog, payment_settings, guest_payload, active_user, access_token
):
    _package, option = catalog
    plan = make_plan(option, payment_type=MembershipPaymentType.INSTALLMENT)
    created = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk)})
    assert created.status_code == 201
    request_id = created.json()["data"]["id"]
    original = created.json()["data"]["payment_plan"]

    MembershipPaymentPlan.objects.filter(pk=plan.pk).update(
        name="Changed", total_amount=Decimal("1500000"), is_active=False
    )
    MembershipPaymentPlanInstallment.objects.filter(payment_plan=plan, sequence=1).update(
        amount=Decimal("700000"), cycle_count=2
    )
    MembershipPaymentPlanInstallment.objects.filter(payment_plan=plan, sequence=2).update(
        amount=Decimal("800000"), cycle_count=1
    )
    MembershipPackagePriceOption.objects.filter(pk=option.pk).update(
        monthly_price_vnd=999_999, is_active=False, active_duration_key=None
    )
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = admin.get(f"{ADMIN_URL}/{request_id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["payment_plan"] == original
    assert data["amount"] == "1300000"
    assert data["initial_payment_amount"] == data["qr_payload"]["amount"] == "500000"
    assert data["payment_plan"]["initial_payment_amount"] == "500000"
    assert "guest_confirmation_token" not in data
    assert "guest_token_hash" not in data
    assert "terms_acceptance_ip" not in data
    assert "terms_acceptance_user_agent" not in data
    item = CSAPurchaseRequest.objects.get(pk=request_id)
    assert item.payment_installments_snapshot == original["installments"]
    item.payment_plan_name_snapshot = "Tampered"
    with pytest.raises(ValidationError):
        item.save(update_fields=["payment_plan_name_snapshot"])
    with pytest.raises(ProtectedError):
        plan.delete()


@pytest.mark.django_db
def test_legacy_create_and_read_without_plan(catalog, payment_settings, guest_payload):
    response = post_purchase(guest_payload)
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["payment_plan"] is None
    assert data["amount"] == "1200000"
    assert data["initial_payment_amount"] == data["qr_payload"]["amount"] == "1200000"
    item = CSAPurchaseRequest.objects.get(pk=data["id"])
    assert item.payment_plan_id is None
    assert item.payment_installments_snapshot is None


@pytest.mark.django_db
def test_confirm_and_tracker_return_plan_snapshot_without_guest_secret(
    catalog, payment_settings, guest_payload
):
    plan = make_plan(catalog[1])
    created = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk)})
    data = created.json()["data"]
    confirmed = APIClient().post(
        f"{CREATE_URL}/{data['id']}/confirm-payment",
        {"guest_confirmation_token": data["guest_confirmation_token"]},
        format="json",
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["payment_plan"] == data["payment_plan"]
    assert confirmed.json()["data"]["initial_payment_amount"] == data["initial_payment_amount"]
    assert "guest_confirmation_token" not in confirmed.json()["data"]

    tracked = APIClient().post(
        "/api/v1/public/csa-contract-tracker/lookup",
        {"reference_code": data["request_code"], "phone": guest_payload["phone"]},
        format="json",
    )
    assert tracked.status_code == 200
    assert tracked.json()["data"]["payment_plan"] == data["payment_plan"]
    assert tracked.json()["data"]["initial_payment_amount"] == data["initial_payment_amount"]
    assert "guest_confirmation_token" not in tracked.json()["data"]
    assert "guest_token_hash" not in tracked.json()["data"]


@pytest.mark.django_db
def test_active_plan_requires_explicit_selection(catalog, payment_settings, guest_payload):
    make_plan(catalog[1])
    response = post_purchase(guest_payload)
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_payment_plan_required"}]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_nonexistent_or_unavailable_plan_is_rejected(catalog, payment_settings, guest_payload):
    missing = post_purchase({**guest_payload, "payment_plan_id": str(uuid.uuid4())})
    assert missing.status_code == 409
    assert missing.json()["errors"] == [{"code": "csa_purchase_payment_plan_unavailable"}]
    inactive = make_plan(catalog[1], active=False)
    response = post_purchase({**guest_payload, "payment_plan_id": str(inactive.pk)})
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_payment_plan_unavailable"}]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_plan_from_other_option_or_package_is_rejected(catalog, payment_settings, guest_payload):
    package, _option = catalog
    other_option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=6, monthly_price_vnd=400_000
    )
    other_package = MembershipPackage.objects.create(name="Other", quota_policy=QuotaPolicy.EXPIRE)
    third_option = MembershipPackagePriceOption.objects.create(
        package=other_package, duration_months=3, monthly_price_vnd=400_000
    )
    for option in (other_option, third_option):
        plan = make_plan(option)
        response = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk)})
        assert response.status_code == 409
        assert response.json()["errors"] == [{"code": "csa_purchase_payment_plan_unavailable"}]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("corruption", ["missing", "amount", "cycles", "sequence"])
def test_invalid_plan_config_is_never_snapshotted(
    catalog, payment_settings, guest_payload, corruption
):
    plan = make_plan(catalog[1], payment_type=MembershipPaymentType.INSTALLMENT)
    lines = list(plan.installments.all())
    if corruption == "missing":
        lines[1].delete()
    elif corruption == "amount":
        MembershipPaymentPlanInstallment.objects.filter(pk=lines[1].pk).update(amount=1)
    elif corruption == "cycles":
        MembershipPaymentPlanInstallment.objects.filter(pk=lines[1].pk).update(cycle_count=3)
    else:
        MembershipPaymentPlanInstallment.objects.filter(pk=lines[1].pk).update(sequence=3)
    response = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk)})
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_payment_plan_unavailable"}]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field",
    [
        "installment_count_snapshot",
        "payment_installments_snapshot",
        "total_payment_amount_snapshot",
        "installment_count",
        "installments",
    ],
)
def test_client_cannot_supply_snapshot_fields(catalog, payment_settings, guest_payload, field):
    response = post_purchase({**guest_payload, field: "fake"})
    assert response.status_code == 400
    assert field in response.json()["errors"][0]["fields"]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_authenticated_create_accepts_only_selected_plan_and_server_identity(
    catalog, payment_settings, active_user, access_token
):
    package, option = catalog
    plan = make_plan(option)
    ShippingAddress.objects.create(
        user=active_user,
        recipient_name="Delivery recipient",
        phone_number="+84901234567",
        province_code="66",
        ward_code="22015",
        address="Server address",
    )
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.post(
        CREATE_URL,
        {
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "payment_plan_id": str(plan.pk),
            "name": "Fake",
            "phone": "0987654321",
            "terms_accepted": True,
        },
        format="json",
    )
    assert response.status_code == 201
    item = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    assert item.user_id == active_user.pk
    assert item.name_snapshot == "Delivery recipient"
    assert item.phone_e164 == "+84901234567"
    assert item.payment_plan_id == plan.pk
    assert "guest_confirmation_token" not in response.json()["data"]


@pytest.mark.django_db
def test_catalog_only_includes_active_valid_plans_in_order_without_n_plus_one(
    catalog, payment_settings
):
    _package, option = catalog
    later = make_plan(option, payment_type=MembershipPaymentType.INSTALLMENT, order=2)
    earlier = make_plan(option, order=1)
    invalid = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Invalid three payments",
        payment_type=MembershipPaymentType.INSTALLMENT,
        installment_count=3,
        total_amount=Decimal("1300000"),
        is_active=True,
        sort_order=0,
    )
    assert invalid.installments.count() == 0
    response = APIClient().get(CATALOG_URL)
    assert response.status_code == 200
    options = response.json()["data"][0]["price_options"]
    assert [plan["id"] for plan in options[0]["payment_plans"]] == [str(earlier.pk), str(later.pk)]
    assert options[0]["payment_plans"][0]["installments"][0]["amount"] == "1100000"
    assert options[0]["payment_plans"][0]["initial_payment_amount"] == "1100000"
    assert options[0]["payment_plans"][1]["initial_payment_amount"] == "500000"


@pytest.mark.django_db
def test_corrupt_installment_snapshot_cannot_confirm_or_offer_a_qr(
    catalog, payment_settings, guest_payload, active_user, access_token
):
    plan = make_plan(catalog[1], payment_type=MembershipPaymentType.INSTALLMENT)
    created = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk)})
    assert created.status_code == 201
    data = created.json()["data"]
    item = CSAPurchaseRequest.objects.get(pk=data["id"])
    corrupted = [dict(row) for row in item.payment_installments_snapshot]
    corrupted[0]["amount"] = "1"
    CSAPurchaseRequest.objects.filter(pk=item.pk).update(payment_installments_snapshot=corrupted)

    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    admin = APIClient()
    admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    detail = admin.get(f"{ADMIN_URL}/{item.pk}")
    assert detail.status_code == 200
    assert detail.json()["data"]["initial_payment_amount"] is None
    assert detail.json()["data"]["qr_payload"]["amount"] == ""

    confirm = APIClient().post(
        f"{CREATE_URL}/{item.pk}/confirm-payment",
        {"guest_confirmation_token": data["guest_confirmation_token"]},
        format="json",
    )
    assert confirm.status_code == 400
    item.refresh_from_db()
    assert item.status == "pending"

    with CaptureQueriesContext(connection) as one_query_set:
        APIClient().get(CATALOG_URL)
    extra = MembershipPackage.objects.create(name="Second", quota_policy=QuotaPolicy.EXPIRE)
    other_option = MembershipPackagePriceOption.objects.create(
        package=extra, duration_months=3, monthly_price_vnd=400_000
    )
    product = Product.objects.first()
    MembershipPackageItem.objects.create(package=extra, product=product, quota_units=1)
    make_plan(other_option)
    with CaptureQueriesContext(connection) as two_query_set:
        APIClient().get(CATALOG_URL)
    assert len(two_query_set) <= len(one_query_set) + 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_plan_deactivation_cannot_create_request_with_stale_active_plan(
    catalog, payment_settings, guest_payload
):
    plan = make_plan(catalog[1])
    locked = threading.Event()
    release = threading.Event()
    outcomes = []

    def deactivate():
        close_old_connections()
        try:
            with transaction.atomic():
                locked_plan = MembershipPaymentPlan.objects.select_for_update().get(pk=plan.pk)
                locked.set()
                assert release.wait(timeout=10)
                locked_plan.is_active = False
                locked_plan.save(update_fields=["is_active", "updated_at"])
        finally:
            close_old_connections()

    def create():
        close_old_connections()
        try:
            response = post_purchase({**guest_payload, "payment_plan_id": str(plan.pk)})
            outcomes.append((response.status_code, response.json()))
        finally:
            close_old_connections()

    writer = threading.Thread(target=deactivate)
    writer.start()
    assert locked.wait(timeout=10)
    creator = threading.Thread(target=create)
    creator.start()
    release.set()
    writer.join(timeout=10)
    creator.join(timeout=10)

    assert not writer.is_alive() and not creator.is_alive()
    assert outcomes == [(409, {"errors": [{"code": "csa_purchase_payment_plan_unavailable"}]})]
    assert not CSAPurchaseRequest.objects.exists()
