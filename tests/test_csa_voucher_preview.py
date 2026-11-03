from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from memberships.models import (
    CSACoupon,
    CSAPurchaseRequest,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
)

URL = "/api/v1/csa-voucher-preview"


@pytest.fixture
def preview_data():
    package = MembershipPackage.objects.create(name="CSA", quota_policy="expire")
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=400_000
    )
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Twice",
        payment_type="installment",
        total_amount=1_200_000,
        installment_count=2,
        is_active=True,
    )
    for sequence, amount, cycles in [(1, 400_000, 1), (2, 800_000, 2)]:
        MembershipPaymentPlanInstallment.objects.create(
            payment_plan=plan, sequence=sequence, amount=amount, cycle_count=cycles
        )
    coupon = CSACoupon.objects.create(
        code="SAVE",
        application_scope="both",
        discount_type="fixed",
        discount_value=100_000,
        starts_at=timezone.now() - timedelta(days=1),
        expires_at=timezone.now() + timedelta(days=1),
        usage_limit=10,
    )
    return coupon, {
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "payment_plan_id": str(plan.pk),
        "coupon_code": "save",
    }


@pytest.fixture
def client(access_token):
    access_token.scope = "profile email"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


@pytest.mark.django_db
def test_preview_has_no_identity_terms_payment_or_purchase_side_effects(client, preview_data):
    coupon, data = preview_data
    for _ in range(2):
        response = client.post(URL, data, format="json")
        assert response.status_code == 200, response.data
        result = response.data["data"]
        assert set(result) == {"coupon", "payment_summary"}
        assert result["coupon"]["code"] == "SAVE"
        summary = result["payment_summary"]
        assert summary["initial_payment_amount"] == "300000"
        assert summary["customer_payable_total"] == "1100000"
        assert [row["amount"] for row in summary["installments"]] == ["300000", "800000"]
        assert "no-store" in response["Cache-Control"]
    coupon.refresh_from_db()
    assert coupon.used_count == 0
    assert not coupon.usages.exists()
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("scope", ["", "profile"])
def test_preview_requires_login_and_both_self_scopes(access_token, preview_data, scope):
    _, data = preview_data
    client = APIClient()
    if scope:
        access_token.scope = scope
        access_token.save(update_fields=["scope"])
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert client.post(URL, data, format="json").status_code in {401, 403}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field,value,code",
    [
        ("application_scope", "store", "coupon_scope_mismatch"),
        ("is_active", False, "coupon_inactive"),
        ("used_count", 10, "coupon_usage_exhausted"),
    ],
)
def test_preview_reuses_coupon_policy(client, preview_data, field, value, code):
    coupon, data = preview_data
    before_count = value if field == "used_count" else coupon.used_count
    setattr(coupon, field, value)
    coupon.save(update_fields=[field])
    response = client.post(URL, data, format="json")
    assert response.status_code == 400
    assert response.data["errors"][0]["code"] == code
    coupon.refresh_from_db()
    assert coupon.used_count == before_count


@pytest.mark.django_db
def test_preview_rejects_foreign_plan_and_client_terms_claim(client, preview_data):
    _, data = preview_data
    response = client.post(URL, {**data, "price_option_id": data["package_id"]}, format="json")
    assert response.status_code == 409
    response = client.post(URL, {**data, "terms_accepted": True}, format="json")
    assert response.status_code == 400


@pytest.mark.django_db
def test_preview_rejects_percentage_rounded_to_zero(client, preview_data):
    from decimal import Decimal

    coupon, _ = preview_data
    coupon.discount_type = "percent"
    coupon.discount_value = Decimal("0.01")
    coupon.save()
    package = MembershipPackage.objects.create(name="Small", quota_policy="expire")
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=1000
    )
    response = client.post(
        URL,
        {
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "coupon_code": coupon.code,
        },
        format="json",
    )
    assert response.status_code == 400
    assert response.data["errors"][0]["code"] == "coupon_invalid_value"
    coupon.refresh_from_db()
    assert coupon.used_count == 0
