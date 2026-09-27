from datetime import timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient

from memberships.models import (
    CSACoupon,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    QuotaPolicy,
)

BASE = "/api/v1/admin/csa-coupons"


@pytest.fixture
def admin_client(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


@pytest.fixture
def catalog():
    package = MembershipPackage.objects.create(name="CSA", quota_policy=QuotaPolicy.EXPIRE)
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=400_000
    )
    plan = MembershipPaymentPlan.objects.create(
        price_option=option,
        name="Pay twice",
        payment_type="installment",
        total_amount=Decimal("1200000"),
        installment_count=2,
        is_active=True,
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=plan, sequence=1, amount=Decimal("600000"), cycle_count=1
    )
    MembershipPaymentPlanInstallment.objects.create(
        payment_plan=plan, sequence=2, amount=Decimal("600000"), cycle_count=2
    )
    return package, option, plan


def payload(*, package_ids=None, price_option_ids=None, **changes):
    now = timezone.now()
    values = {
        "code": " tettrungthu2026 ",
        "discount_type": "fixed",
        "discount_value": "100000",
        "starts_at": (now - timedelta(days=1)).isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "package_ids": package_ids or [],
        "price_option_ids": price_option_ids or [],
        "payment_types": ["installment"],
        "installment_counts": [2],
    }
    values.update(changes)
    return values


@pytest.mark.django_db
def test_admin_permission_and_crud(admin_client, catalog):
    package, option, _plan = catalog
    assert APIClient().get(BASE).status_code == 401
    body = payload(package_ids=[str(package.pk)], price_option_ids=[str(option.pk)])
    created = admin_client.post(BASE, body, format="json")
    assert created.status_code == 201, created.json()
    data = created.json()["data"]
    assert data["code"] == "TETTRUNGTHU2026"
    assert data["usage_limit"] == 1
    assert data["maximum_discount_amount"] is None
    assert data["status"] == "active"
    assert data["scope"]["packages"][0]["name"] == "CSA"
    assert set(data) == {
        "id",
        "code",
        "discount_type",
        "discount_value",
        "usage_limit",
        "used_count",
        "starts_at",
        "expires_at",
        "is_active",
        "status",
        "package_ids",
        "price_option_ids",
        "payment_types",
        "installment_counts",
        "minimum_amount",
        "maximum_discount_amount",
        "scope",
        "created_at",
        "updated_at",
    }
    url = f"{BASE}/{data['id']}"
    assert admin_client.get(url).json()["data"]["code"] == data["code"]
    assert admin_client.get(BASE).json()["data"][0]["id"] == data["id"]
    changed = admin_client.patch(url, {"discount_value": "200000", "usage_limit": 3}, format="json")
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["discount_value"] == "200000.00"
    assert (
        admin_client.post(f"{url}/deactivate", {}, format="json").json()["data"]["status"]
        == "inactive"
    )
    assert admin_client.post(f"{url}/deactivate", {}, format="json").status_code == 200
    assert (
        admin_client.post(f"{url}/activate", {}, format="json").json()["data"]["status"] == "active"
    )
    assert admin_client.post(f"{url}/activate", {}, format="json").json()["errors"] == [
        {"code": "coupon_already_active"}
    ]
    assert admin_client.delete(url).status_code == 405


@pytest.mark.django_db
def test_coupon_admin_requires_website_admin(active_user, access_token):
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    for path, method in (
        (BASE, "get"),
        (BASE, "post"),
        (f"{BASE}/options", "get"),
        (f"{BASE}/preview", "post"),
        (f"{BASE}/00000000-0000-0000-0000-000000000001", "get"),
        (f"{BASE}/00000000-0000-0000-0000-000000000001", "patch"),
        (f"{BASE}/00000000-0000-0000-0000-000000000001/activate", "post"),
        (f"{BASE}/00000000-0000-0000-0000-000000000001/deactivate", "post"),
    ):
        assert getattr(client, method)(path).status_code == 403


@pytest.mark.django_db
def test_coupon_options_only_include_active_catalog(admin_client, catalog):
    package, option, plan = catalog
    response = admin_client.get(f"{BASE}/options")
    assert response.status_code == 200
    assert response.json()["data"] == [
        {
            "id": str(package.pk),
            "name": package.name,
            "price_options": [
                {
                    "id": str(option.pk),
                    "duration_months": option.duration_months,
                    "payment_plans": [
                        {
                            "id": str(plan.pk),
                            "name": plan.name,
                            "payment_type": "installment",
                            "installment_count": 2,
                        }
                    ],
                }
            ],
        }
    ]
    MembershipPaymentPlan.objects.filter(pk=plan.pk).update(is_active=False)
    assert (
        admin_client.get(f"{BASE}/options").json()["data"][0]["price_options"][0]["payment_plans"]
        == []
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"code": " "}, "coupon_invalid"),
        ({"discount_type": "other"}, "coupon_invalid_type"),
        ({"discount_value": "0"}, "coupon_invalid_value"),
        ({"discount_value": "100000.5"}, "coupon_invalid_value"),
        ({"discount_type": "percent", "discount_value": "101"}, "coupon_invalid_value"),
        ({"usage_limit": 0}, "coupon_invalid_usage_limit"),
        ({"payment_types": ["other"]}, "coupon_not_applicable"),
        ({"installment_counts": [0]}, "coupon_not_applicable"),
        ({"package_ids": ["00000000-0000-0000-0000-000000000001"]}, "coupon_not_applicable"),
        ({"price_option_ids": ["00000000-0000-0000-0000-000000000001"]}, "coupon_not_applicable"),
        ({"qr_amount": "1"}, "coupon_not_applicable"),
    ],
)
def test_admin_validation_errors(admin_client, changes, code):
    response = admin_client.post(BASE, payload(**changes), format="json")
    assert response.status_code == 400
    assert response.json() == {"errors": [{"code": code}]}


@pytest.mark.django_db
def test_admin_date_scope_duplicate_and_filter(admin_client, catalog):
    package, option, _plan = catalog
    wrong_package = MembershipPackage.objects.create(name="Other", quota_policy=QuotaPolicy.EXPIRE)
    invalid_scope = admin_client.post(
        BASE,
        payload(package_ids=[str(wrong_package.pk)], price_option_ids=[str(option.pk)]),
        format="json",
    )
    assert invalid_scope.json()["errors"] == [{"code": "coupon_not_applicable"}]
    now = timezone.now()
    bad_date = admin_client.post(
        BASE,
        payload(starts_at=(now + timedelta(days=2)).isoformat(), expires_at=now.isoformat()),
        format="json",
    )
    assert bad_date.json()["errors"] == [{"code": "coupon_invalid_date_range"}]
    created = admin_client.post(BASE, payload(), format="json")
    assert created.status_code == 201
    duplicate = admin_client.post(BASE, payload(code="TETTRUNGTHU2026"), format="json")
    assert duplicate.status_code == 409
    assert duplicate.json()["errors"] == [{"code": "coupon_duplicate_code"}]
    assert len(admin_client.get(f"{BASE}?status=active&discount_type=fixed").json()["data"]) == 1
    assert admin_client.get(f"{BASE}?status=scheduled").json()["data"] == []
    assert admin_client.get(f"{BASE}?unknown=1").json()["errors"] == [{"code": "coupon_invalid"}]
    assert admin_client.get(f"{BASE}?status=active&status=inactive").json()["errors"] == [
        {"code": "coupon_invalid"}
    ]


@pytest.mark.django_db
def test_status_filters_and_activation_guards(admin_client):
    now = timezone.now()
    active = admin_client.post(BASE, payload(code="ACTIVE", usage_limit=2), format="json").json()[
        "data"
    ]
    inactive = admin_client.post(BASE, payload(code="INACTIVE"), format="json").json()["data"]
    scheduled = admin_client.post(
        BASE,
        payload(
            code="SCHEDULED",
            starts_at=(now + timedelta(days=1)).isoformat(),
            expires_at=(now + timedelta(days=2)).isoformat(),
        ),
        format="json",
    ).json()["data"]
    expired = admin_client.post(BASE, payload(code="EXPIRED"), format="json").json()["data"]
    exhausted = admin_client.post(BASE, payload(code="EXHAUSTED"), format="json").json()["data"]
    admin_client.post(f"{BASE}/{inactive['id']}/deactivate", {}, format="json")
    admin_client.post(f"{BASE}/{scheduled['id']}/deactivate", {}, format="json")
    CSACoupon.objects.filter(pk=scheduled["id"]).update(is_active=True)
    admin_client.post(f"{BASE}/{expired['id']}/deactivate", {}, format="json")
    CSACoupon.objects.filter(pk=expired["id"]).update(
        starts_at=now - timedelta(days=2), expires_at=now - timedelta(days=1)
    )
    CSACoupon.objects.filter(pk=expired["id"]).update(is_active=True)
    CSACoupon.objects.filter(pk=exhausted["id"]).update(used_count=1)
    expected = {
        "active": active["id"],
        "inactive": inactive["id"],
        "scheduled": scheduled["id"],
        "expired": expired["id"],
        "exhausted": exhausted["id"],
    }
    for state, identifier in expected.items():
        rows = admin_client.get(f"{BASE}?status={state}").json()["data"]
        assert [row["id"] for row in rows] == [identifier]
        assert rows[0]["status"] == state
    CSACoupon.objects.filter(pk=expired["id"]).update(is_active=False)
    assert admin_client.post(f"{BASE}/{expired['id']}/activate", {}, format="json").json() == {
        "errors": [{"code": "coupon_cannot_activate"}]
    }
    CSACoupon.objects.filter(pk=exhausted["id"]).update(is_active=False)
    assert admin_client.post(f"{BASE}/{exhausted['id']}/activate", {}, format="json").json() == {
        "errors": [{"code": "coupon_cannot_activate"}]
    }


@pytest.mark.django_db
def test_used_coupon_is_immutable_except_deactivation_and_limit_increase(admin_client):
    created = admin_client.post(BASE, payload(usage_limit=3), format="json").json()["data"]
    url = f"{BASE}/{created['id']}"
    CSACoupon.objects.filter(pk=created["id"]).update(used_count=2)
    assert admin_client.patch(url, {"code": "OTHER"}, format="json").json()["errors"] == [
        {"code": "coupon_cannot_edit_used"}
    ]
    assert admin_client.patch(url, {"discount_value": "200000"}, format="json").json()[
        "errors"
    ] == [{"code": "coupon_cannot_edit_used"}]
    assert admin_client.patch(url, {"package_ids": []}, format="json").status_code == 200
    assert admin_client.patch(url, {"usage_limit": 1}, format="json").json()["errors"] == [
        {"code": "coupon_invalid_usage_limit"}
    ]
    assert admin_client.patch(url, {"usage_limit": 4}, format="json").status_code == 200
    assert admin_client.post(f"{url}/deactivate", {}, format="json").status_code == 200
    assert admin_client.delete(url).status_code == 405


@pytest.mark.django_db
def test_backend_preview_uses_first_installment_only(admin_client, catalog):
    _package, _option, plan = catalog
    response = admin_client.post(
        f"{BASE}/preview", {**payload(), "payment_plan_id": str(plan.pk)}, format="json"
    )
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["payment_plan_total_before_discount"] == "1200000"
    assert data["customer_payable_total"] == "1100000"
    assert [row["amount"] for row in data["installments"]] == ["500000", "600000"]
    assert not CSACoupon.objects.exists()
    minimum_not_met = admin_client.post(
        f"{BASE}/preview",
        {**payload(minimum_amount="2000000"), "payment_plan_id": str(plan.pk)},
        format="json",
    )
    assert minimum_not_met.json() == {"errors": [{"code": "coupon_not_applicable"}]}


@pytest.mark.django_db
def test_percent_coupon_maximum_discount_cap_is_validated_and_previewed(admin_client, catalog):
    _package, _option, plan = catalog
    values = {
        **payload(
            discount_type="percent",
            discount_value="50",
            maximum_discount_amount="40000",
        ),
    }
    created = admin_client.post(BASE, values, format="json")
    assert created.status_code == 201, created.json()
    assert created.json()["data"]["maximum_discount_amount"] == "40000"

    preview = admin_client.post(
        f"{BASE}/preview", {**values, "payment_plan_id": str(plan.pk)}, format="json"
    )
    assert preview.status_code == 200, preview.json()
    assert preview.json()["data"]["discount_amount"] == "40000"
    assert preview.json()["data"]["initial_payment_after_discount"] == "560000"

    invalid_fixed = admin_client.post(
        BASE,
        payload(discount_type="fixed", maximum_discount_amount="40000", code="FIXEDCAP"),
        format="json",
    )
    assert invalid_fixed.status_code == 400
    assert invalid_fixed.json() == {"errors": [{"code": "coupon_invalid_value"}]}
