"""Delivery calendar and companion shipping are authoritative backend decisions."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import StoreOrder, StoreStockReservation
from commerce.store_delivery import VIETNAM, delivery_calendar
from memberships import csa_rounds
from memberships.models import CSACoupon, CSARound, CSASelection
from tests.test_csa_round_api_fixes import admin_client as admin_client
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def test_checkout_requires_delivery_date(store_product, payment_settings, supported_area):
    payload = guest_payload(store_product)
    payload.pop("delivery_date", None)
    response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 400
    assert "delivery_date" in response.data


@pytest.mark.parametrize("offset,expected", [(-1, 400), (0, 201), (7, 201), (8, 400)])
def test_delivery_date_window(store_product, payment_settings, supported_area, offset, expected):
    payload = guest_payload(store_product)
    payload["delivery_date"] = (timezone.localdate() + timedelta(days=offset)).isoformat()
    response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == expected, response.data
    if expected == 201:
        assert response.data["data"]["delivery_date"] == payload["delivery_date"]
    else:
        assert response.data["errors"][0]["code"] == "store_delivery_date_invalid"


def test_guest_calendar_uses_vietnam_midnight(monkeypatch):
    now = datetime(2026, 11, 13, 17, 0, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    with timezone.override("UTC"):
        response = APIClient().get("/api/v1/public/store-delivery-dates")
    assert response.status_code == 200
    days = response.data["data"]["dates"]
    assert len(days) == 8
    assert days[0] == {"delivery_date": "2026-11-14", "free_shipping": False}
    assert days[-1]["delivery_date"] == "2026-11-21"


def test_companion_configuration_and_committed_csa_only(context, monkeypatch):
    from commerce.store_delivery import delivery_calendar

    user, _, _, round_, now = context
    selected = save(context, 1)
    csa_rounds.configure_companion_delivery(
        round_id=round_.pk,
        actor=user,
        companion_delivery_enabled=True,
        companion_delivery_cutoff=round_.delivery_at - timedelta(minutes=1),
    )
    date = timezone.localdate(round_.delivery_at).isoformat()

    def free():
        return {
            d["delivery_date"]: d["free_shipping"] for d in delivery_calendar(user=user)["dates"]
        }[date]

    assert not free()
    csa_rounds.process_round(round_.pk, now=round_.closes_at)
    assert free()
    selected.refresh_from_db()
    StoreOrder.objects.filter(pk=selected.order_id).update(status="cancelled")
    assert not free()


def member_client(user):
    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    return client


@pytest.fixture
def companion(context, monkeypatch):
    user, product, membership, round_, now = context
    selected = save(context, 1)
    csa_rounds.process_round(round_.pk, now=round_.closes_at)
    csa_rounds.configure_companion_delivery(
        round_id=round_.pk,
        actor=user,
        companion_delivery_enabled=True,
        companion_delivery_cutoff=round_.delivery_at - timedelta(minutes=30),
    )
    selected.refresh_from_db()
    return context, selected


def preview_body(context, *, day=None, quota=False):
    return {
        "delivery_date": (day or context[3].delivery_at.astimezone(VIETNAM).date()).isoformat(),
        "lines": [{"product_id": str(context[1].pk), "quantity": 1, "use_quota": quota}],
    }


def checkout_body(quote):
    return {
        "delivery_date": quote["delivery_date"],
        "idempotency_key": str(uuid4()),
        "lines": quote["lines"],
        "shipping_address_updated_at": quote["shipping_address_updated_at"],
        "expected_shipping_fee_vnd": quote["shipping_fee_vnd"],
        "expected_total_vnd": quote["total_vnd"],
    }


@pytest.mark.parametrize("delta,free", [(-1, True), (0, False), (1, False)])
def test_companion_cutoff_is_strict(companion, monkeypatch, delta, free):
    context, _ = companion
    row = CSARound.objects.get(pk=context[3].pk)
    monkeypatch.setattr(
        timezone, "now", lambda: row.companion_delivery_cutoff + timedelta(seconds=delta)
    )
    data = member_client(context[0]).post(
        "/api/v1/public/store-quotes", preview_body(context), format="json"
    )
    assert data.status_code == 200, data.data
    assert data.data["data"]["shipping_fee_vnd"] == ("0" if free else "30000")


@pytest.mark.parametrize("invalid", ["yesterday", "now", "delivery", "later", "missing"])
def test_admin_rejects_invalid_cutoff(context, admin_client, invalid):
    round_, now = context[3:]
    value = {
        "yesterday": now - timedelta(days=1),
        "now": now,
        "delivery": round_.delivery_at,
        "later": round_.delivery_at + timedelta(seconds=1),
    }
    body = {"companion_delivery_enabled": True}
    if invalid != "missing":
        body["companion_delivery_cutoff"] = value[invalid].isoformat()
    response = admin_client.patch(
        f"/api/v1/admin/csa-rounds/{round_.pk}/companion-delivery", body, format="json"
    )
    assert response.status_code == 409
    assert response.data["errors"] == [{"code": "csa_companion_deadline_invalid"}]


@pytest.mark.parametrize("zone", ["naive", "offset", "utc"])
def test_admin_cutoff_requires_explicit_timezone(context, admin_client, zone):
    round_, now = context[3:]
    cutoff = now + timedelta(hours=2)
    value = cutoff.astimezone(VIETNAM).isoformat()
    if zone == "naive":
        value = cutoff.astimezone(VIETNAM).replace(tzinfo=None).isoformat()
    elif zone == "utc":
        value = cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z")
    response = admin_client.patch(
        f"/api/v1/admin/csa-rounds/{round_.pk}/companion-delivery",
        {"companion_delivery_enabled": True, "companion_delivery_cutoff": value},
        format="json",
    )
    assert response.status_code == (400 if zone == "naive" else 200), response.data
    round_.refresh_from_db()
    if zone == "naive":
        assert "companion_delivery_cutoff" in response.data
        assert not round_.companion_delivery_enabled
        assert round_.companion_delivery_cutoff is None
    else:
        assert round_.companion_delivery_cutoff == cutoff


def test_admin_close_reopen_independent_of_selection_and_private(companion, admin_client):
    context, selected = companion
    path = f"/api/v1/admin/csa-rounds/{context[3].pk}/companion-delivery"
    assert APIClient().get(path).status_code == 401
    assert (
        member_client(context[0])
        .patch(path, {"companion_delivery_enabled": False}, format="json")
        .status_code
        == 403
    )
    before = (selected.state, selected.revision, selected.order_id)
    response = admin_client.patch(path, {"companion_delivery_enabled": False}, format="json")
    assert response.status_code == 200
    assert not any(d["free_shipping"] for d in delivery_calendar(user=context[0])["dates"])
    response = admin_client.patch(path, {"companion_delivery_enabled": True}, format="json")
    assert response.status_code == 200
    selected.refresh_from_db()
    assert (selected.state, selected.revision, selected.order_id) == before
    public = member_client(context[0]).get("/api/v1/csa-rounds").json()
    assert "companion_delivery_cutoff" not in str(public)
    assert "companion_delivery_enabled" not in str(public)


@pytest.mark.parametrize(
    "source",
    ["other_user", "cancelled_order", "cancelled_round", "expired_reselection", "disabled"],
)
def test_ineligible_source_does_not_waive_shipping(companion, active_user, source):
    context, selected = companion
    user = context[0]
    if source == "other_user":
        from identity.models import User

        user = User.objects.create_user(email="other@nfv.test", name="Other", password="test")
    elif source == "cancelled_order":
        StoreOrder.objects.filter(pk=selected.order_id).update(status="cancelled")
    elif source == "cancelled_round":
        CSARound.objects.filter(pk=context[3].pk).update(cancelled_at=context[4])
    elif source == "expired_reselection":
        CSASelection.objects.filter(pk=selected.pk).update(
            state="reselecting", reopen_until=context[4]
        )
    else:
        CSARound.objects.filter(pk=context[3].pk).update(companion_delivery_enabled=False)
    assert not any(d["free_shipping"] for d in delivery_calendar(user=user)["dates"])


def test_multiple_rounds_same_day_checks_all_sources(companion):
    from copy import copy

    context, selected = companion
    round_ = context[3]
    other = CSARound.objects.create(
        sequence=round_.sequence + 1,
        name="Second",
        opens_at=round_.opens_at,
        closes_at=round_.closes_at,
        delivery_at=round_.delivery_at,
        published_at=context[4],
        companion_delivery_enabled=True,
        companion_delivery_cutoff=round_.delivery_at - timedelta(minutes=1),
    )
    order = copy(selected.order)
    order.pk = uuid4()
    order.create_idempotency_key = uuid4()
    order.reference_code = str(uuid4())[:20]
    order.save(force_insert=True)
    CSASelection.objects.create(
        round=other, membership_id=selected.membership_id, order=order, state="locked"
    )
    # An expired first source must not hide the eligible second source.
    CSARound.objects.filter(pk=round_.pk).update(companion_delivery_cutoff=context[4])
    assert any(d["free_shipping"] for d in delivery_calendar(user=context[0])["dates"])


@pytest.mark.parametrize("quota", [False, True])
@pytest.mark.parametrize("voucher", [False, True])
def test_preview_create_change_day_requote_and_retry(companion, payment_settings, quota, voucher):
    context, selected = companion
    client = member_client(context[0])
    body = preview_body(context, quota=quota)
    if quota:
        body["lines"][0]["quantity"] = 4
    if voucher:
        coupon = CSACoupon.objects.create(
            code="SHIP",
            application_scope="store",
            discount_type="fixed",
            discount_value=1,
            starts_at=context[4] - timedelta(days=1),
            expires_at=context[4] + timedelta(days=3),
        )
        body["coupon_code"] = coupon.code
    first = client.post("/api/v1/public/store-quotes", body, format="json")
    assert first.status_code == 200, first.data
    quote = first.data["data"]
    assert quote["shipping_fee_vnd"] == "0"
    payload = checkout_body(quote)
    if voucher:
        payload.update(
            coupon_code=body["coupon_code"], expected_voucher_quote=quote["voucher_quote"]
        )
    # A different date must recalculate, even with unchanged cart/quota.
    other_day = context[4].astimezone(VIETNAM).date()
    changed = client.post(
        "/api/v1/public/store-quotes",
        {**body, "delivery_date": other_day.isoformat()},
        format="json",
    )
    assert changed.status_code == 200, changed.data
    assert changed.data["data"]["shipping_fee_vnd"] == "30000"
    assert int(changed.data["data"]["total_vnd"]) == int(quote["total_vnd"]) + 30000
    response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.data
    order_id = response.data["data"]["id"]
    snapshot = response.data["data"]["shipping_benefit"]
    # Closing/changing/cancelling the source never changes the placed order.
    CSARound.objects.filter(pk=context[3].pk).update(
        companion_delivery_enabled=False, cancelled_at=context[4]
    )
    StoreOrder.objects.filter(pk=selected.order_id).update(status="cancelled")
    replay = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert replay.status_code == 200, replay.data
    assert replay.data["data"]["id"] == order_id
    assert replay.data["data"]["shipping_fee_vnd"] == "0"
    assert replay.data["data"]["shipping_benefit"] == snapshot
    assert StoreStockReservation.objects.filter(line__order_id=order_id).count() == 1
    changed_payload = {**payload, "delivery_date": other_day.isoformat()}
    assert (
        client.post("/api/v1/public/store-checkouts", changed_payload, format="json").status_code
        == 409
    )


def test_free_shipping_without_csa_product_and_cutoff_requote(
    companion, payment_settings, monkeypatch
):
    context, _ = companion
    product = context[1]
    product.csa_quota_group = None
    product.csa_quota_cost = 0
    product.save()
    client = member_client(context[0])
    quote = client.post("/api/v1/public/store-quotes", preview_body(context), format="json").data[
        "data"
    ]
    assert quote["shipping_fee_vnd"] == "0"
    cutoff = CSARound.objects.get(pk=context[3].pk).companion_delivery_cutoff
    monkeypatch.setattr(timezone, "now", lambda: cutoff)
    response = client.post("/api/v1/public/store-checkouts", checkout_body(quote), format="json")
    assert response.status_code == 409, response.data
    assert response.data["errors"][0]["code"] == "store_requote_required"
    assert response.data["errors"][0]["context"]["shipping_fee_vnd"] == 30000
    assert StoreOrder.objects.count() == 1  # Only the CSA source.
    assert not StoreStockReservation.objects.exists()


def test_guest_quote_and_create_share_calculation(store_product, payment_settings, supported_area):
    client = APIClient()
    payload = guest_payload(store_product)
    quote_input = {
        "delivery_date": payload["delivery_date"],
        "guest": payload["guest"],
        "lines": [{"product_id": str(store_product.pk), "quantity": 2, "use_quota": False}],
    }
    preview = client.post("/api/v1/public/store-quotes", quote_input, format="json")
    assert preview.status_code == 200, preview.data
    quote = preview.data["data"]
    assert quote["shipping_fee_vnd"] == "30000"
    body = checkout_body(quote)
    body.pop("shipping_address_updated_at")
    body["guest"] = payload["guest"]
    created = client.post("/api/v1/public/store-checkouts", body, format="json")
    assert created.status_code == 201, created.data
    assert created.data["data"]["total_vnd"] == quote["total_vnd"]


def test_voucher_date_binding_even_when_fee_is_unchanged(companion, payment_settings):
    context, _ = companion
    coupon = CSACoupon.objects.create(
        code="DATE",
        application_scope="store",
        discount_type="fixed",
        discount_value=1,
        starts_at=context[4] - timedelta(days=1),
        expires_at=context[4] + timedelta(days=3),
    )
    client = member_client(context[0])
    today = context[4].astimezone(VIETNAM).date()
    body = {**preview_body(context, day=today), "coupon_code": coupon.code}
    first = client.post("/api/v1/public/store-quotes", body, format="json").data["data"]
    body["delivery_date"] = (today + timedelta(days=2)).isoformat()
    second = client.post("/api/v1/public/store-quotes", body, format="json").data["data"]
    assert first["total_vnd"] == second["total_vnd"]
    assert first["voucher_quote"] != second["voucher_quote"]
    create = checkout_body(second)
    create.update(coupon_code=coupon.code, expected_voucher_quote=first["voucher_quote"])
    response = client.post("/api/v1/public/store-checkouts", create, format="json")
    assert response.status_code == 409
    assert response.data["errors"][0]["context"]["reason"] == "voucher_changed"


@pytest.mark.parametrize("blocked", ["round", "product", "payment"])
def test_checkout_rechecks_cutoff_after_lock_wait(
    companion, payment_settings, monkeypatch, blocked
):
    from django.db.models.query import QuerySet

    from commerce.models import Product
    from memberships.models import MembershipPaymentSettings

    context, _ = companion
    client = member_client(context[0])
    quote = client.post("/api/v1/public/store-quotes", preview_body(context), format="json").data[
        "data"
    ]
    row = CSARound.objects.get(pk=context[3].pk)
    clock = [context[4]]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    original = QuerySet._fetch_all
    model = {"round": CSARound, "product": Product, "payment": MembershipPaymentSettings}[blocked]

    def fetch(rows):
        result = original(rows)
        if rows.model is model and rows.query.select_for_update:
            clock[0] = row.companion_delivery_cutoff
        return result

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    response = client.post("/api/v1/public/store-checkouts", checkout_body(quote), format="json")
    assert response.status_code == 409, response.data
    assert response.data["errors"][0]["code"] == "store_requote_required"
    assert StoreOrder.objects.count() == 1
    assert not StoreStockReservation.objects.exists()


def test_vietnam_delivery_day_differs_from_utc(companion):
    context, _ = companion
    today = context[4].astimezone(VIETNAM).date()
    delivery = datetime.combine(
        today + timedelta(days=1), datetime.min.time().replace(minute=30), tzinfo=VIETNAM
    )
    # Keep the synthetic round valid even when the test runs near VN midnight.
    CSARound.objects.filter(pk=context[3].pk).update(
        opens_at=context[4] - timedelta(hours=2),
        closes_at=context[4] - timedelta(minutes=1),
        delivery_at=delivery,
        companion_delivery_cutoff=delivery - timedelta(minutes=1),
    )
    days = {
        d["delivery_date"]: d["free_shipping"] for d in delivery_calendar(user=context[0])["dates"]
    }
    assert days[(today + timedelta(days=1)).isoformat()]
    assert not days[today.isoformat()]


def test_old_order_is_readable_with_null_delivery_date(companion):
    context, selection = companion
    client = member_client(context[0])
    response = client.get(f"/api/v1/store-orders/{selection.order_id}")
    assert response.status_code == 200
    assert response.data["data"]["delivery_date"] is None
    assert response.data["data"]["shipping_benefit"] is None


def test_batched_calendar_query_count_does_not_scale_with_rounds(companion):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    context, _ = companion
    with CaptureQueriesContext(connection) as captured:
        delivery_calendar(user=context[0])
    assert len(captured) <= 3


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("winner", ["admin", "checkout"])
def test_admin_checkout_serialization_on_row_lock_database(
    companion, payment_settings, monkeypatch, winner
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, local

    from django.db import close_old_connections, connection, transaction
    from django.db.models.query import QuerySet

    from commerce.models import Product
    from commerce.serializers import StoreCheckoutSerializer
    from commerce.store_orders import StoreOrderError, create_store_checkout

    if not connection.features.has_select_for_update:
        pytest.skip(
            "Requires a database with real SELECT FOR UPDATE; SQLite cannot prove concurrency"
        )
    context, _ = companion
    user, _, _, round_, _ = context
    quote = (
        member_client(user)
        .post("/api/v1/public/store-quotes", preview_body(context), format="json")
        .data["data"]
    )
    serializer = StoreCheckoutSerializer(data=checkout_body(quote))
    serializer.is_valid(raise_exception=True)
    locked, attempted = Event(), Event()
    state = local()
    original = QuerySet._fetch_all

    def fetch(rows):
        if (
            winner == "checkout"
            and getattr(state, "admin", False)
            and rows.model is CSARound
            and rows.query.select_for_update
        ):
            attempted.set()
        if (
            winner == "admin"
            and getattr(state, "checkout", False)
            and rows.model is CSARound
            and rows.query.select_for_update
        ):
            attempted.set()
        result = original(rows)
        if (
            winner == "checkout"
            and getattr(state, "checkout", False)
            and rows.model is Product
            and rows.query.select_for_update
            and not locked.is_set()
        ):
            locked.set()
            assert attempted.wait(10)
        return result

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)

    def admin():
        close_old_connections()
        state.admin = True
        try:
            if winner == "checkout":
                assert locked.wait(10)
            with transaction.atomic():
                if winner == "admin":
                    CSARound.objects.select_for_update().get(pk=round_.pk)
                    locked.set()
                    assert attempted.wait(10)
                csa_rounds.configure_companion_delivery(
                    round_id=round_.pk, actor=user, companion_delivery_enabled=False
                )
            return "closed"
        finally:
            close_old_connections()

    def checkout():
        close_old_connections()
        state.checkout = True
        try:
            if winner == "admin":
                assert locked.wait(10)
            try:
                order, _, _ = create_store_checkout(user=user, payload=serializer.validated_data)
                return str(order.pk)
            except StoreOrderError as error:
                return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        admin_result = pool.submit(admin)
        checkout_result = pool.submit(checkout)
        assert admin_result.result(timeout=20) == "closed"
        result = checkout_result.result(timeout=20)
    if winner == "admin":
        assert result == "store_requote_required"
        assert StoreOrder.objects.count() == 1
    else:
        order = StoreOrder.objects.get(pk=result)
        assert order.shipping_fee_vnd == 0
        assert order.shipping_benefit_snapshot["round_id"] == str(round_.pk)
        assert StoreStockReservation.objects.filter(line__order=order).count() == 1


def test_admin_can_close_after_expiry_and_reopen_with_new_deadline(
    companion, admin_client, monkeypatch
):
    context, _ = companion
    row = CSARound.objects.get(pk=context[3].pk)
    monkeypatch.setattr(timezone, "now", lambda: row.companion_delivery_cutoff)
    path = f"/api/v1/admin/csa-rounds/{row.pk}/companion-delivery"
    closed = admin_client.patch(path, {"companion_delivery_enabled": False}, format="json")
    assert closed.status_code == 200
    invalid = admin_client.patch(path, {"companion_delivery_enabled": True}, format="json")
    assert invalid.status_code == 409
    reopened = admin_client.patch(
        path,
        {
            "companion_delivery_enabled": True,
            "companion_delivery_cutoff": (row.delivery_at - timedelta(seconds=1)).isoformat(),
        },
        format="json",
    )
    assert reopened.status_code == 200
    assert any(d["free_shipping"] for d in delivery_calendar(user=context[0])["dates"])
    CSARound.objects.filter(pk=row.pk).update(cancelled_at=row.companion_delivery_cutoff)
    assert (
        admin_client.patch(path, {"companion_delivery_enabled": True}, format="json").status_code
        == 409
    )


def test_midnight_crossed_while_waiting_rejects_yesterday(companion, payment_settings, monkeypatch):
    from django.db.models.query import QuerySet

    from commerce.models import Product

    context, _ = companion
    client = member_client(context[0])
    day = context[4].astimezone(VIETNAM).date()
    quote = client.post(
        "/api/v1/public/store-quotes", preview_body(context, day=day), format="json"
    ).data["data"]
    original = QuerySet._fetch_all

    def fetch(rows):
        result = original(rows)
        if rows.model is Product and rows.query.select_for_update:
            after = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=VIETNAM)
            monkeypatch.setattr(timezone, "now", lambda: after)
        return result

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    response = client.post("/api/v1/public/store-checkouts", checkout_body(quote), format="json")
    assert response.status_code == 400, response.data
    assert response.data["errors"][0]["code"] == "store_delivery_date_invalid"
    assert not StoreStockReservation.objects.exists()


def test_placed_retail_order_survives_real_csa_cancellation(companion, payment_settings):
    context, selection = companion
    client = member_client(context[0])
    quote = client.post("/api/v1/public/store-quotes", preview_body(context), format="json").data[
        "data"
    ]
    created = client.post("/api/v1/public/store-checkouts", checkout_body(quote), format="json")
    assert created.status_code == 201
    snapshot = created.data["data"]["shipping_benefit"]
    csa_rounds.cancel_round(round_id=context[3].pk, actor=context[0], reason="cancel source")
    selection.refresh_from_db()
    assert selection.state == "cancelled"
    order = StoreOrder.objects.get(pk=created.data["data"]["id"])
    assert order.status == "payment_pending"
    assert order.shipping_fee_vnd == 0
    assert order.shipping_benefit_snapshot == snapshot


def test_retry_after_date_window_still_replays_original(companion, payment_settings, monkeypatch):
    context, _ = companion
    client = member_client(context[0])
    quote = client.post("/api/v1/public/store-quotes", preview_body(context), format="json").data[
        "data"
    ]
    body = checkout_body(quote)
    first = client.post("/api/v1/public/store-checkouts", body, format="json")
    assert first.status_code == 201
    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=9))
    replay = client.post("/api/v1/public/store-checkouts", body, format="json")
    assert replay.status_code == 200, replay.data
    assert replay.data["data"]["id"] == first.data["data"]["id"]
    assert replay.data["data"]["shipping_benefit"] == first.data["data"]["shipping_benefit"]
    assert StoreOrder.objects.count() == 2


def test_admin_rechecks_time_after_waiting_for_round_lock(context, admin_client, monkeypatch):
    from django.db.models.query import QuerySet

    row, now = context[3:]
    cutoff = now + timedelta(seconds=1)
    original = QuerySet._fetch_all

    def fetch(rows):
        result = original(rows)
        if rows.model is CSARound and rows.query.select_for_update:
            monkeypatch.setattr(timezone, "now", lambda: cutoff)
        return result

    monkeypatch.setattr(QuerySet, "_fetch_all", fetch)
    response = admin_client.patch(
        f"/api/v1/admin/csa-rounds/{row.pk}/companion-delivery",
        {
            "companion_delivery_enabled": True,
            "companion_delivery_cutoff": cutoff.isoformat(),
        },
        format="json",
    )
    assert response.status_code == 409
    row.refresh_from_db()
    assert not row.companion_delivery_enabled


def test_frozen_only_blocked_but_renewal_can_use_original_csa_trip(context, monkeypatch):
    from copy import copy

    from tests.test_csa_store_flow_fixes import frozen_context

    old = frozen_context(context, monkeypatch)
    user, _, _, round_, _ = context
    # Preserve the original participant and create its CSA order while frozen.
    csa_rounds.process_round(round_.pk, now=round_.closes_at)
    csa_rounds.configure_companion_delivery(
        round_id=round_.pk,
        actor=user,
        companion_delivery_enabled=True,
        companion_delivery_cutoff=round_.delivery_at - timedelta(minutes=1),
    )
    assert member_client(user).get("/api/v1/public/store-delivery-dates").status_code == 409
    fresh = copy(old)
    fresh.pk = uuid4()
    fresh.start_date = old.end_date
    fresh.end_date = fresh.start_date + timedelta(days=30)
    fresh.extension_end_date = None
    fresh.status = "active"
    fresh.current_slot = True
    fresh.farmbrite_order_reference = str(uuid4())
    fresh.farmbrite_order_reference_key = str(uuid4())
    fresh.save(force_insert=True)
    response = member_client(user).get("/api/v1/public/store-delivery-dates")
    assert response.status_code == 200
    assert any(d["free_shipping"] for d in response.data["data"]["dates"])
    assert CSASelection.objects.get(round=round_).membership_id == old.pk


@pytest.mark.django_db(transaction=True)
def test_additive_migrations_keep_legacy_orders_and_rounds_unconfigured(companion):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    context, selected = companion
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old = [
        ("commerce", "0008_product_translation_description_document"),
        ("memberships", "0036_csa_round_cancellation_audit"),
    ]
    try:
        executor.migrate(old)
        executor = MigrationExecutor(connection)
        historical = executor.loader.project_state(old).apps
        assert (
            historical.get_model("commerce", "StoreOrder")
            .objects.filter(pk=selected.order_id)
            .exists()
        )
        assert (
            historical.get_model("memberships", "CSARound")
            .objects.filter(pk=context[3].pk)
            .exists()
        )
        executor.migrate(latest)
        restored = StoreOrder.objects.get(pk=selected.order_id)
        assert restored.delivery_date is None
        assert restored.shipping_benefit_snapshot is None
        round_ = CSARound.objects.get(pk=context[3].pk)
        assert not round_.companion_delivery_enabled
        assert round_.companion_delivery_cutoff is None
    finally:
        MigrationExecutor(connection).migrate(latest)
