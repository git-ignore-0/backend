"""Admin payment-plan configuration must not mutate purchased snapshots."""

import threading
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from memberships.models import (
    CSAPurchaseRequest,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    Product,
    QuotaPolicy,
)
from memberships.payment_plans import PaymentPlanValidationError, set_admin_payment_plan_active
from memberships.purchases import update_membership_payment_settings


@pytest.fixture
def option():
    package = MembershipPackage.objects.create(
        name="CSA", description="Vegetables", quota_policy=QuotaPolicy.EXPIRE
    )
    product = Product.objects.create(name="Vegetables", unit_size=Decimal("1"))
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=1)
    return MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=400_000
    )


@pytest.fixture
def admin_client(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def collection(option):
    return f"/api/v1/admin/membership-price-options/{option.pk}/payment-plans"


def full_plan():
    return {
        "name": "Pay in full",
        "payment_type": "full",
        "total_amount": "1100000",
        "installment_count": 1,
        "sort_order": 0,
        "installments": [{"sequence": 1, "amount": "1100000", "cycle_count": 3}],
    }


def installment_plan():
    return {
        "name": "Pay twice",
        "payment_type": "installment",
        "total_amount": "1200000",
        "installment_count": 2,
        "installments": [
            {"sequence": 1, "amount": "600000", "cycle_count": 1},
            {"sequence": 2, "amount": "600000", "cycle_count": 2},
        ],
    }


@pytest.mark.django_db
def test_admin_crud_and_activation(option, admin_client):
    base = collection(option)
    created = admin_client.post(base, installment_plan(), format="json")
    assert created.status_code == 201
    plan_id = created.json()["data"]["id"]
    detail = f"{base}/{plan_id}"
    assert created.json()["data"]["is_active"] is False
    assert created.json()["data"]["initial_payment_amount"] == "600000"
    assert len(admin_client.get(base).json()["data"]) == 1
    assert admin_client.get(detail).json()["data"]["name"] == "Pay twice"

    changed = admin_client.patch(detail, {"name": "Two payments"}, format="json")
    assert changed.status_code == 200
    assert changed.json()["data"]["name"] == "Two payments"
    assert admin_client.post(f"{detail}/activate", {}, format="json").status_code == 200
    assert admin_client.post(f"{detail}/activate", {}, format="json").status_code == 200
    assert (
        admin_client.patch(detail, {"name": "No"}, format="json").json()["errors"][0]["code"]
        == "payment_plan_deactivate_before_edit"
    )
    assert admin_client.post(f"{detail}/deactivate", {}, format="json").status_code == 200
    assert admin_client.delete(detail).status_code == 204
    assert not MembershipPaymentPlan.objects.filter(pk=plan_id).exists()


@pytest.mark.django_db
def test_admin_rejects_one_month_installments_with_stable_code(option, admin_client):
    option.duration_months = 1
    option.save(update_fields=["duration_months"])
    full = full_plan()
    full["installments"][0]["cycle_count"] = 1
    assert admin_client.post(collection(option), full, format="json").status_code == 201

    rejected = admin_client.post(collection(option), installment_plan(), format="json")
    assert rejected.status_code == 400
    assert rejected.json()["errors"][0]["code"] == "payment_plan_installment_not_allowed"


@pytest.mark.django_db
def test_admin_cannot_activate_legacy_one_month_installment(option, admin_client):
    created = admin_client.post(collection(option), installment_plan(), format="json")
    assert created.status_code == 201
    option.duration_months = 1
    option.save(update_fields=["duration_months"])

    response = admin_client.post(
        f"{collection(option)}/{created.json()['data']['id']}/activate", {}, format="json"
    )
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "payment_plan_installment_not_allowed"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"payment_type": "other"}, "payment_plan_invalid"),
        ({"installment_count": 2}, "payment_plan_installment_count_invalid"),
        (
            {"installments": [{"sequence": 2, "amount": "1100000", "cycle_count": 3}]},
            "payment_plan_sequence_gap",
        ),
        (
            {"installments": [{"sequence": 1, "amount": "1000000", "cycle_count": 3}]},
            "payment_plan_amount_total_mismatch",
        ),
        (
            {"installments": [{"sequence": 1, "amount": "1100000", "cycle_count": 2}]},
            "payment_plan_cycle_total_mismatch",
        ),
        (
            {"installments": [{"sequence": 1, "amount": "0", "cycle_count": 3}]},
            "payment_plan_invalid",
        ),
    ],
)
def test_admin_rejects_invalid_plan(option, admin_client, change, code):
    response = admin_client.post(collection(option), {**full_plan(), **change}, format="json")
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == code
    assert not MembershipPaymentPlan.objects.exists()


@pytest.mark.django_db
def test_admin_permission_and_wrong_option(option, admin_client):
    base = collection(option)
    assert APIClient().get(base).status_code in {401, 403}
    created = admin_client.post(base, full_plan(), format="json").json()["data"]
    other = MembershipPackagePriceOption.objects.create(
        package=option.package, duration_months=6, monthly_price_vnd=400_000
    )
    assert admin_client.get(f"{collection(other)}/{created['id']}").status_code == 404
    assert (
        admin_client.post(f"{base}/{created['id']}/unknown", {}, format="json").status_code == 404
    )


@pytest.mark.django_db
def test_invalid_stored_plan_cannot_be_activated(option, admin_client):
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Incomplete",
        payment_type="installment",
        total_amount=Decimal("1200000"),
        installment_count=2,
        is_active=False,
    )
    response = admin_client.post(f"{collection(option)}/{plan.pk}/activate", {}, format="json")
    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "payment_plan_installment_count_mismatch"
    plan.refresh_from_db()
    assert not plan.is_active


@pytest.mark.django_db
def test_used_plan_cannot_be_edited_or_deleted(
    option, admin_client, monkeypatch, default_supported_shipping_area
):
    base = collection(option)
    plan_id = admin_client.post(base, full_plan(), format="json").json()["data"]["id"]
    admin_client.post(f"{base}/{plan_id}/activate", {}, format="json")
    cache.clear()
    monkeypatch.setattr(
        "memberships.banks.get_transfer_banks",
        lambda: [{"bin": "970436", "name": "Vietcombank"}],
    )
    update_membership_payment_settings(
        bank_bin="970436",
        bank_name="Vietcombank",
        account_number="123456789",
        account_name="NFV",
        direct_transfer_enabled=True,
    )
    public = APIClient().post(
        "/api/v1/public/csa-purchase-requests",
        {
            "package_id": str(option.package_id),
            "price_option_id": str(option.pk),
            "payment_plan_id": plan_id,
            "name": "Buyer",
            "phone": "0912345678",
            "province_code": "66",
            "ward_code": "22015",
            "address": "12 Main Street",
            "terms_accepted": True,
        },
        format="json",
    )
    assert public.status_code == 201
    request_id = public.json()["data"]["id"]
    original = CSAPurchaseRequest.objects.get(pk=request_id).payment_installments_snapshot
    detail = f"{base}/{plan_id}"
    assert admin_client.post(f"{detail}/deactivate", {}, format="json").status_code == 200
    assert (
        admin_client.patch(detail, {"total_amount": "1000000"}, format="json").json()["errors"][0][
            "code"
        ]
        == "payment_plan_in_use"
    )
    assert admin_client.delete(detail).status_code == 409
    assert CSAPurchaseRequest.objects.get(pk=request_id).payment_installments_snapshot == original

    replacement = admin_client.post(base, full_plan(), format="json")
    assert replacement.status_code == 201
    replacement_id = replacement.json()["data"]["id"]
    assert replacement_id != plan_id
    assert (
        MembershipPaymentPlan.objects.filter(
            price_option=option,
            name="Pay in full",
            payment_type="full",
            installment_count=1,
            is_active=False,
        ).count()
        == 2
    )
    plan = MembershipPaymentPlan.objects.get(pk=plan_id)
    assert not plan.is_active
    assert CSAPurchaseRequest.objects.get(pk=request_id).payment_plan_id == plan.pk


@pytest.mark.django_db
def test_inactive_historical_plans_can_be_replaced_but_only_one_duplicate_can_be_active(
    option, admin_client
):
    base = collection(option)
    original = admin_client.post(base, full_plan(), format="json")
    assert original.status_code == 201
    original_id = original.json()["data"]["id"]
    assert admin_client.post(f"{base}/{original_id}/activate", {}, format="json").status_code == 200
    assert (
        admin_client.post(f"{base}/{original_id}/deactivate", {}, format="json").status_code == 200
    )

    replacement = admin_client.post(base, full_plan(), format="json")
    assert replacement.status_code == 201
    replacement_id = replacement.json()["data"]["id"]
    assert replacement_id != original_id

    assert admin_client.post(f"{base}/{original_id}/activate", {}, format="json").status_code == 200
    duplicate = admin_client.post(f"{base}/{replacement_id}/activate", {}, format="json")
    assert duplicate.status_code == 400
    assert duplicate.json()["errors"][0]["code"] == "payment_plan_active_duplicate"
    assert (
        admin_client.post(f"{base}/{original_id}/deactivate", {}, format="json").status_code == 200
    )
    assert (
        admin_client.post(f"{base}/{replacement_id}/activate", {}, format="json").status_code == 200
    )


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_activation_keeps_only_one_active_plan(option):
    first = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Pay in full",
        payment_type="full",
        total_amount=Decimal("1100000"),
        installment_count=1,
    )
    second = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Pay in full",
        payment_type="full",
        total_amount=Decimal("1100000"),
        installment_count=1,
    )
    for plan in (first, second):
        plan.installments.create(sequence=1, amount=Decimal("1100000"), cycle_count=3)

    start = threading.Barrier(2)
    outcomes: list[str] = []

    def activate(plan_id):
        close_old_connections()
        try:
            start.wait(timeout=10)
            try:
                set_admin_payment_plan_active(option_id=option.pk, plan_id=plan_id, active=True)
                outcomes.append("active")
            except PaymentPlanValidationError as error:
                outcomes.append(error.code)
        finally:
            close_old_connections()

    workers = [threading.Thread(target=activate, args=(plan.pk,)) for plan in (first, second)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert all(not worker.is_alive() for worker in workers)
    assert sorted(outcomes) == ["active", "payment_plan_active_duplicate"]
    assert (
        MembershipPaymentPlan.objects.filter(
            price_option=option,
            payment_type="full",
            installment_count=1,
            is_active=True,
        ).count()
        == 1
    )
