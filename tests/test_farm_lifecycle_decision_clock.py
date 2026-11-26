from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.db.models import QuerySet
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import StoreOrder, StoreStockReservation
from farms.models import FarmProductStockBatch
from memberships import csa_rounds
from memberships.models import CSARound, CSAStockHold, MembershipPaymentSettings
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def test_checkout_rechecks_stock_when_payment_lock_crosses_vietnam_midnight(
    store_product, payment_settings, monkeypatch
):
    today = timezone.localdate()
    clock = [datetime.combine(today, time(23, 59, 59), ZoneInfo("Asia/Ho_Chi_Minh"))]
    FarmProductStockBatch.objects.filter(farm_product__product=store_product).update(end_date=today)
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    real_fetch = QuerySet._fetch_all

    def wait_for_payment(queryset):
        if queryset.model is MembershipPaymentSettings and queryset.query.select_for_update:
            clock[0] += timedelta(seconds=2)
        return real_fetch(queryset)

    monkeypatch.setattr(QuerySet, "_fetch_all", wait_for_payment)
    payload = guest_payload(store_product)
    payload["delivery_date"] = today + timedelta(days=1)
    response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")

    assert response.status_code == 409, response.json()
    assert response.json()["errors"][0]["code"] == "store_requote_required"
    assert not StoreOrder.objects.exists()
    assert not StoreStockReservation.objects.exists()


def test_csa_rechecks_stock_when_quota_lock_crosses_vietnam_midnight(context, monkeypatch):
    _, product, _, round_, initial = context
    today = timezone.localdate(initial)
    clock = [datetime.combine(today, time(23, 59, 59), ZoneInfo("Asia/Ho_Chi_Minh"))]
    CSARound.objects.filter(pk=round_.pk).update(
        closes_at=clock[0] + timedelta(hours=1), delivery_date=today + timedelta(days=2)
    )
    FarmProductStockBatch.objects.filter(farm_product__product=product).update(end_date=today)
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    real_candidates = csa_rounds._quota_candidates

    def wait_for_quota(*args, **kwargs):
        result = real_candidates(*args, **kwargs)
        clock[0] += timedelta(seconds=2)
        return result

    monkeypatch.setattr(csa_rounds, "_quota_candidates", wait_for_quota)
    with pytest.raises(csa_rounds.CSARoundError, match="csa_stock_insufficient"):
        save(context)
    assert not CSAStockHold.objects.exists()
