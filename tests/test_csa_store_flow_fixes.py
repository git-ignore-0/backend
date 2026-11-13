"""Regression tests for frozen checkout and CSA order-list deadline consistency."""

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import ShippingAddress, StoreOrder
from memberships import csa_rounds as service
from memberships.models import MembershipCycleQuota
from memberships.services import sync_membership_status
from tests.test_csa_round_api_fixes import admin_client as admin_client
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area
from tests.test_store_orders import zero_shipping_fee as zero_shipping_fee

pytestmark = pytest.mark.django_db


def authenticated_client(user):
    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    return client


def frozen_context(context, monkeypatch):
    _, _, member, round_, now = context
    member.end_date = timezone.localdate(now) + timedelta(days=1)
    member.save(update_fields=["end_date"])
    round_.closes_at = now + timedelta(days=1, hours=1)
    round_.delivery_at = now + timedelta(days=3)
    round_.save()
    save(context, 1)
    monkeypatch.setattr(timezone, "now", lambda: now + timedelta(days=1))
    assert sync_membership_status(member).status == "frozen"
    return member


def cash_payload(context):
    payload = guest_payload(context[1], quantity=1, expected_available=9)
    payload.pop("guest")
    payload["shipping_address_updated_at"] = ShippingAddress.objects.get(
        user=context[0]
    ).updated_at.isoformat()
    return payload


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("stale_status", [False, True])
def test_frozen_member_cannot_cash_checkout_or_preview(
    context, monkeypatch, payment_settings, preview, stale_status
):
    member = frozen_context(context, monkeypatch)
    if stale_status:
        type(member).objects.filter(pk=member.pk).update(status="active")
    client = authenticated_client(context[0])
    payload = cash_payload(context)
    if preview:
        from commerce.store_orders import StoreOrderError, preview_store_voucher

        payload["lines"][0]["product_id"] = context[1].pk
        with pytest.raises(StoreOrderError, match="store_membership_frozen"):
            preview_store_voucher(user=context[0], payload=payload)
    else:
        response = client.post("/api/v1/public/store-checkouts", payload, format="json")
        assert response.status_code == 409, response.json()
        assert response.json()["errors"][0]["code"] == "store_membership_frozen"
    assert not StoreOrder.objects.exists()


def test_new_active_plan_restores_cash_checkout_while_old_plan_is_frozen(
    context, monkeypatch, payment_settings
):
    member = frozen_context(context, monkeypatch)
    fresh = copy.copy(member)
    fresh.pk = uuid.uuid4()
    fresh.start_date = member.end_date
    fresh.end_date = fresh.start_date + timedelta(days=30)
    fresh.extension_end_date = None
    fresh.status = "active"
    fresh.current_slot = True
    fresh.farmbrite_order_reference = str(uuid.uuid4())
    fresh.farmbrite_order_reference_key = str(uuid.uuid4())
    fresh.save(force_insert=True)
    response = authenticated_client(context[0]).post(
        "/api/v1/public/store-checkouts", cash_payload(context), format="json"
    )
    assert response.status_code == 201, response.json()


def expire_reselection(context, monkeypatch):
    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    deadline = context[3].delivery_at - timedelta(hours=1)
    service.reopen_selection(
        selection_id=selected.pk,
        actor=context[0],
        deadline=deadline,
        now=context[3].closes_at,
    )
    monkeypatch.setattr(timezone, "now", lambda: deadline)
    selected.refresh_from_db()
    return selected


@pytest.mark.parametrize("admin", [False, True])
@pytest.mark.parametrize("requested_status", [None, "cancelled", "pending_review"])
def test_order_list_uses_expired_csa_status_for_rows_and_count(
    context, monkeypatch, admin_client, admin, requested_status
):
    selected = expire_reselection(context, monkeypatch)
    client = admin_client if admin else authenticated_client(context[0])
    path = "/api/v1/admin/store-orders" if admin else "/api/v1/store-orders"
    if requested_status:
        path += f"?status={requested_status}"
    response = client.get(path)
    assert response.status_code == 200, response.json()
    body = response.json()
    if requested_status == "pending_review":
        assert body["data"] == []
        assert body["meta"]["total"] == 0
    else:
        assert body["meta"]["total"] == 1
        assert body["data"][0]["status"] == "cancelled"
        assert body["data"][0]["csa"]["selection"]["state"] == "cancelled"
        selected.refresh_from_db()
        assert selected.current_usage_id is None
        quota = MembershipCycleQuota.objects.get(cycle__membership=context[2])
        assert quota.remaining_units == 4
        client.get(path)
        quota.refresh_from_db()
        assert quota.remaining_units == 4


def test_empty_order_page_does_not_process_expired_csa_backlog(context, monkeypatch, admin_client):
    selected = expire_reselection(context, monkeypatch)
    response = admin_client.get("/api/v1/admin/store-orders?page=9999&page_size=1")
    assert response.status_code == 200
    assert response.json()["data"] == []
    selected.refresh_from_db()
    assert selected.state == "reselecting"
