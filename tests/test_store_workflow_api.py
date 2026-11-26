"""Public/admin contract for review-before-payment; exercises real domain writes."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import StoreOrder, StoreOrderAction
from commerce.store_orders import StoreOrderError
from tests.test_store_orders import assert_admin_action_receipt, guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(active_user):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    client = APIClient()
    client.force_authenticate(
        active_user, token=SimpleNamespace(scope="profile email content:manage")
    )
    return client


@pytest.fixture
def checkout(store_product, payment_settings, supported_area):
    response = APIClient().post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )
    assert response.status_code == 201, response.data
    return response.data["data"]


def act(client, order, action, **body):
    return client.post(
        f"/api/v1/admin/store-orders/{order['id']}/{action}",
        {"idempotency_key": str(uuid4()), **body},
        format="json",
    )


def approve(client, order):
    line = StoreOrder.objects.get(pk=order["id"]).lines.get()
    batch = line.product.farm_offers.get().stock_batches.get()
    return act(
        client,
        order,
        "approve",
        allocations=[
            {
                "line_id": str(line.pk),
                "batch_id": str(batch.pk),
                "quantity": line.quantity,
            }
        ],
    )


def test_checkout_hides_payment_until_goods_review(checkout):
    assert checkout["status"] == "pending_review"
    assert checkout["workflow_version"] == 2
    assert checkout["payment"] is None
    assert checkout["refund_status"] is None
    for _ in range(2):
        response = APIClient().get(
            f"/api/v1/public/store-orders/{checkout['id']}",
            HTTP_X_STORE_ACCESS_TOKEN=checkout["guest_access_token"],
        )
        assert response.data["data"]["payment"] is None
        assert response.data["data"]["expires_at"] == checkout["expires_at"]
    response = APIClient().post(
        f"/api/v1/public/store-orders/{checkout['id']}/report-transfer",
        {"access_token": checkout["guest_access_token"]},
        format="json",
    )
    assert response.status_code == 409
    assert response.data["errors"][0]["code"] == "store_order_state_conflict"


def test_buyer_cannot_request_refund_when_cancelled_before_goods_approval(checkout):
    client = APIClient()
    body = {
        "access_token": checkout["guest_access_token"],
        "idempotency_key": str(uuid4()),
        "request_refund": False,
    }
    response = client.post(
        f"/api/v1/public/store-orders/{checkout['id']}/cancel", body, format="json"
    )
    assert response.status_code == 200, response.data
    assert response.data["data"]["approved_at"] is None
    assert response.data["data"]["refund_status"] is None
    response = client.post(
        f"/api/v1/public/store-orders/{checkout['id']}/request-refund",
        {"access_token": checkout["guest_access_token"], "idempotency_key": str(uuid4())},
        format="json",
    )
    assert response.status_code == 409, response.data
    assert response.data["errors"][0]["code"] == "store_refund_state_conflict"
    order = StoreOrder.objects.get(pk=checkout["id"])
    assert order.refund_requested_at is None
    assert order.refund_status is None
    assert not order.actions.filter(action="request_refund").exists()


def test_goods_approval_and_receipt_are_separate_actions(checkout, admin):
    response = approve(admin, checkout)
    assert response.status_code == 200, response.data
    data = response.data["data"]
    assert data["status"] == "payment_pending"
    assert data["payment_status"] == "unconfirmed"
    assert data["payment"]["qr_payload"]["amount"] == 120000
    assert data["approved_by"] is not None
    deadline = data["expires_at"]
    response = APIClient().get(
        f"/api/v1/public/store-orders/{checkout['id']}",
        HTTP_X_STORE_ACCESS_TOKEN=checkout["guest_access_token"],
    )
    assert response.data["data"]["expires_at"] == deadline
    response = act(admin, checkout, "record-payment")
    assert response.status_code == 200, response.data
    assert response.data["data"]["status"] == "pending_delivery"
    assert response.data["data"]["payment_received_by"] is not None


def test_cancel_reconciliation_close_is_final_and_cancel_retry_is_safe(checkout, admin):
    assert approve(admin, checkout).status_code == 200
    body = {
        "access_token": checkout["guest_access_token"],
        "idempotency_key": str(uuid4()),
        "request_refund": True,
    }
    url = f"/api/v1/public/store-orders/{checkout['id']}/cancel"
    response = APIClient().post(url, body, format="json")
    assert response.status_code == 200, response.data
    assert response.data["data"]["status"] == "cancelled"
    assert response.data["data"]["refund_status"] == "pending"
    assert response.data["data"]["payment_status"] == "unconfirmed"
    assert APIClient().post(url, body, format="json").status_code == 200
    response = act(admin, checkout, "close-refund-request", reason="Bank verified no funds")
    assert response.status_code == 200, response.data
    assert response.data["data"]["refund_status"] is None
    replay = APIClient().post(url, body, format="json")
    assert replay.status_code == 200
    assert replay.data["data"]["refund_status"] is None
    assert replay.data["data"]["refund_request_closed_at"] is not None
    assert (
        APIClient()
        .post(
            f"/api/v1/public/store-orders/{checkout['id']}/request-refund",
            {"access_token": checkout["guest_access_token"], "idempotency_key": str(uuid4())},
            format="json",
        )
        .status_code
        == 409
    )
    assert act(admin, checkout, "record-payment", note="Must not reopen").status_code == 409
    assert act(admin, checkout, "refund", amount_vnd=120000, note="No receipt").status_code == 409


@pytest.mark.parametrize("action", ["cancel", "request-refund", "report-transfer"])
def test_wrong_guest_cannot_mutate_order(checkout, action):
    response = APIClient().post(
        f"/api/v1/public/store-orders/{checkout['id']}/{action}",
        {
            "access_token": "wrong",
            **({"idempotency_key": str(uuid4())} if action != "report-transfer" else {}),
        },
        format="json",
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "action", ["approve", "record-payment", "cancel", "close-refund-request", "refund", "deliver"]
)
def test_guest_cannot_call_admin_actions(checkout, action):
    response = act(APIClient(), checkout, action)
    assert response.status_code in {401, 403}


def test_order_list_reconciles_expired_store_page(checkout, admin):
    StoreOrder.objects.filter(pk=checkout["id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    response = admin.get("/api/v1/admin/store-orders?status=cancelled")
    assert response.status_code == 200
    assert response.data["meta"]["total"] == 1
    assert response.data["data"][0]["status"] == "cancelled"
    assert StoreOrder.objects.get(pk=checkout["id"]).status == "cancelled"


@pytest.mark.parametrize("action", ["cancel", "request-refund", "report-transfer"])
def test_authenticated_missing_scope_never_falls_back_to_valid_guest(checkout, active_user, action):
    client = APIClient()
    client.force_authenticate(active_user, token=SimpleNamespace(scope="profile"))
    response = client.post(
        f"/api/v1/public/store-orders/{checkout['id']}/{action}",
        {
            "access_token": checkout["guest_access_token"],
            **({"idempotency_key": str(uuid4())} if action != "report-transfer" else {}),
        },
        format="json",
    )
    assert response.status_code == 403
    assert response.data["errors"][0]["code"] == "insufficient_scope"
    assert StoreOrder.objects.get(pk=checkout["id"]).status == "pending_review"


def test_cancel_and_close_require_strict_bodies(checkout, admin):
    for body in ({"request_refund": "false"}, {"request_refund": True, "note": "unknown"}):
        response = APIClient().post(
            f"/api/v1/public/store-orders/{checkout['id']}/cancel",
            {
                "access_token": checkout["guest_access_token"],
                "idempotency_key": str(uuid4()),
                **body,
            },
            format="json",
        )
        assert response.status_code == 400
    for action in ("cancel", "close-refund-request"):
        assert act(admin, checkout, action, reason="   ").status_code == 400
    assert StoreOrder.objects.get(pk=checkout["id"]).status == "pending_review"


def test_payment_review_filter_returns_new_state(checkout, admin):
    assert approve(admin, checkout).status_code == 200
    url = f"/api/v1/public/store-orders/{checkout['id']}/report-transfer"
    body = {"access_token": checkout["guest_access_token"]}
    first = APIClient().post(url, body, format="json")
    assert first.status_code == 200
    assert first.data["data"]["status"] == "payment_review"
    repeat = APIClient().post(url, body, format="json")
    assert repeat.status_code == 200
    assert repeat.data["data"]["expires_at"] == first.data["data"]["expires_at"]
    response = admin.get("/api/v1/admin/store-orders?status=payment_review")
    assert response.status_code == 200
    assert response.data["meta"]["total"] == 1
    assert response.data["data"][0]["id"] == checkout["id"]


def test_cancel_retry_changed_refund_choice_is_conflict(checkout):
    body = {"access_token": checkout["guest_access_token"], "idempotency_key": str(uuid4())}
    url = f"/api/v1/public/store-orders/{checkout['id']}/cancel"
    assert APIClient().post(url, body, format="json").status_code == 200
    response = APIClient().post(url, {**body, "request_refund": True}, format="json")
    assert response.status_code == 409
    assert response.data["errors"][0]["code"] == "store_idempotency_conflict"


@pytest.mark.parametrize("body", [[], 1, "checkout"])
def test_checkout_rejects_non_object_body_without_server_error(body):
    response = APIClient().post("/api/v1/public/store-checkouts", body, format="json")
    assert response.status_code == 400


@pytest.mark.parametrize("payment_filter,expected_total", [("received", 0), ("refund_required", 1)])
@pytest.mark.parametrize("status_filter", ["", "&status=cancelled"])
def test_admin_payment_filter_uses_effective_expiry_state(
    checkout, admin, active_user, payment_filter, expected_total, status_filter
):
    from commerce.store_orders import record_store_payment
    from tests.test_store_orders import legacy_checkout_snapshot

    legacy_checkout_snapshot(checkout)
    record_store_payment(order_id=checkout["id"], actor=active_user, idempotency_key=uuid4())
    StoreOrder.objects.filter(pk=checkout["id"]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    response = admin.get(
        f"/api/v1/admin/store-orders?payment_status={payment_filter}{status_filter}"
    )
    assert response.status_code == 200
    assert response.data["meta"]["total"] == expected_total
    assert len(response.data["data"]) == expected_total
    if expected_total:
        item = response.data["data"][0]
        assert item["id"] == checkout["id"]
        assert item["status"] == "cancelled"
        assert item["payment_status"] == "refund_required"


def test_admin_refund_filter_counts_expired_orders_outside_current_page(
    checkout, admin, active_user, store_product
):
    from commerce.store_orders import record_store_payment
    from tests.test_store_orders import legacy_checkout_snapshot

    created = APIClient().post(
        "/api/v1/public/store-checkouts",
        guest_payload(store_product, expected_available=8),
        format="json",
    )
    assert created.status_code == 201
    orders = [checkout, created.data["data"]]
    for data in orders:
        legacy_checkout_snapshot(data)
        record_store_payment(order_id=data["id"], actor=active_user, idempotency_key=uuid4())
        StoreOrder.objects.filter(pk=data["id"]).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    seen = set()
    for page in [1, 2]:
        response = admin.get(
            "/api/v1/admin/store-orders?status=cancelled&payment_status=refund_required"
            f"&page_size=1&page={page}"
        )
        assert response.status_code == 200
        assert response.data["meta"]["total"] == 2
        assert len(response.data["data"]) == 1
        item = response.data["data"][0]
        seen.add(item["id"])
        assert item["status"] == "cancelled"
        assert item["payment_status"] == "refund_required"
        if page == 1:
            assert StoreOrder.objects.filter(payment_status="received").count() == 1
    assert seen == {data["id"] for data in orders}


@pytest.mark.parametrize("receipt", ["new", "historical", "known"])
def test_legacy_approval_records_only_new_receipt_actor(checkout, admin, active_user, receipt):
    from identity.models import User
    from tests.test_store_orders import legacy_checkout_snapshot

    legacy_checkout_snapshot(checkout)
    response = APIClient().post(
        f"/api/v1/public/store-orders/{checkout['id']}/report-transfer",
        {"access_token": checkout["guest_access_token"]},
        format="json",
    )
    assert response.status_code == 200
    original_time = timezone.now() - timedelta(minutes=1)
    original_actor = None
    if receipt == "known":
        original_actor = User.objects.create_user(
            email="receipt@example.com", name="Receipt admin", password="StrongPassword12"
        )
    if receipt != "new":
        StoreOrder.objects.filter(pk=checkout["id"]).update(
            payment_status="received",
            payment_received_at=original_time,
            payment_received_by=original_actor,
        )
    approved = approve(admin, checkout)
    assert approved.status_code == 200
    order = StoreOrder.objects.get(pk=checkout["id"])
    assert order.status == "pending_delivery"
    assert order.payment_status == "received"
    assert order.approved_by == active_user
    assert order.payment_received_by == (active_user if receipt == "new" else original_actor)
    assert approved.data["data"]["payment_received_by"] == (
        str(order.payment_received_by_id) if order.payment_received_by_id else None
    )
    if receipt != "new":
        assert order.payment_received_at == original_time


@pytest.mark.parametrize("reported", [False, True])
@pytest.mark.parametrize("source", ["buyer", "admin", "expiry"])
def test_v2_payment_cancellation_opens_one_reconciliation_and_close_is_final(
    checkout, admin, reported, source, monkeypatch
):
    from commerce.models import StoreOrderAction, StoreStockMovement

    approved = approve(admin, checkout)
    assert approved.status_code == 200
    token = checkout["guest_access_token"]
    buyer = APIClient()
    public = f"/api/v1/public/store-orders/{checkout['id']}"
    if reported:
        assert (
            buyer.post(
                f"{public}/report-transfer", {"access_token": token}, format="json"
            ).status_code
            == 200
        )
    if source == "buyer":
        # Old callers sending false must not bypass reconciliation at either payment step.
        result = buyer.post(
            f"{public}/cancel",
            {"access_token": token, "idempotency_key": str(uuid4()), "request_refund": False},
            format="json",
        )
    elif source == "admin":
        result = act(admin, checkout, "cancel", reason="Unavailable harvest")
    else:
        row = StoreOrder.objects.get(pk=checkout["id"])
        monkeypatch.setattr(timezone, "now", lambda: row.expires_at)
        result = buyer.get(public, HTTP_X_STORE_ACCESS_TOKEN=token)
    assert result.status_code == 200, result.data
    cancelled = result.data["data"]
    assert cancelled["status"] == "cancelled"
    assert cancelled["refund_status"] == "pending"
    assert cancelled["payment_status"] == "unconfirmed"
    assert cancelled["payment_received_at"] is None
    assert cancelled["refund_requested_at"] is not None
    assert bool(cancelled["transfer_reported_at"]) is reported
    assert (
        StoreStockMovement.objects.filter(line__order_id=checkout["id"], kind="restock").count()
        == 1
    )
    closed_key = str(uuid4())
    close_body = {"idempotency_key": closed_key, "reason": "Bank checked: no funds received"}
    close_url = f"/api/v1/admin/store-orders/{checkout['id']}/close-refund-request"
    closed = admin.post(close_url, close_body, format="json")
    assert_admin_action_receipt(closed, checkout["id"], "close_refund_request", closed_key)
    assert closed.data["data"]["refund_status"] is None
    assert closed.data["data"]["refund_request_closed_at"] is not None
    replay = admin.post(close_url, close_body, format="json")
    assert_admin_action_receipt(replay, checkout["id"], "close_refund_request", closed_key)
    assert replay.data["data"]["refund_status"] is None
    before = StoreOrderAction.objects.filter(order_id=checkout["id"]).count()
    claim = buyer.post(
        f"{public}/request-refund",
        {"access_token": token, "idempotency_key": str(uuid4())},
        format="json",
    )
    assert claim.status_code == 409
    assert (
        act(
            admin, checkout, "record-payment", note="Must not reopen closed reconciliation"
        ).status_code
        == 409
    )
    assert StoreOrderAction.objects.filter(order_id=checkout["id"]).count() == before
    assert (
        StoreStockMovement.objects.filter(line__order_id=checkout["id"], kind="restock").count()
        == 1
    )


def test_v2_reconciled_receipt_can_only_be_refunded_and_cannot_be_closed_no_money(checkout, admin):
    assert approve(admin, checkout).status_code == 200
    cancelled = act(admin, checkout, "cancel", reason="Harvest shortage")
    assert cancelled.data["data"]["refund_status"] == "pending"
    received = act(admin, checkout, "record-payment", note="Bank verified")
    assert received.status_code == 200
    assert received.data["data"]["status"] == "cancelled"
    assert received.data["data"]["payment_status"] == "refund_required"
    assert received.data["data"]["refund_status"] == "pending"
    assert (
        act(admin, checkout, "close-refund-request", reason="Not allowed after receipt").status_code
        == 409
    )
    refund_key = str(uuid4())
    body = {
        "idempotency_key": refund_key,
        "amount_vnd": int(checkout["total_vnd"]),
        "note": "Full refund sent",
    }
    url = f"/api/v1/admin/store-orders/{checkout['id']}/refund"
    result = admin.post(url, body, format="json")
    assert result.status_code == 200
    assert result.data["data"]["refund_status"] == "refunded"
    assert (
        admin.post(url, body, format="json").data["data"]["refunded_at"]
        == result.data["data"]["refunded_at"]
    )


@pytest.mark.parametrize("source", ["buyer", "admin", "expiry"])
def test_v2_preapproval_cancel_never_opens_money_review(checkout, admin, source, monkeypatch):
    token = checkout["guest_access_token"]
    client = APIClient()
    public = f"/api/v1/public/store-orders/{checkout['id']}"
    if source == "buyer":
        result = client.post(
            f"{public}/cancel",
            {"access_token": token, "idempotency_key": str(uuid4()), "request_refund": False},
            format="json",
        )
    elif source == "admin":
        result = act(admin, checkout, "cancel", reason="No harvest available")
    else:
        row = StoreOrder.objects.get(pk=checkout["id"])
        monkeypatch.setattr(timezone, "now", lambda: row.expires_at)
        result = client.get(public, HTTP_X_STORE_ACCESS_TOKEN=token)
    assert result.status_code == 200
    assert result.data["data"]["status"] == "cancelled"
    assert result.data["data"]["refund_status"] is None
    assert result.data["data"]["refund_requested_at"] is None
    assert act(admin, checkout, "record-payment").status_code == 409


@pytest.mark.parametrize("explicit_note", [None, "", "Cancellation note"])
def test_v2_reason_only_cancel_preserves_receipt_note_and_exact_replay(
    checkout, admin, explicit_note
):
    from commerce.models import StoreOrderAction

    assert approve(admin, checkout).status_code == 200
    received = act(admin, checkout, "record-payment", note="Bank receipt VN12345")
    assert received.status_code == 200
    body = {"idempotency_key": str(uuid4()), "reason": "Cannot deliver"}
    if explicit_note is not None:
        body["reconciliation_note"] = explicit_note
    url = f"/api/v1/admin/store-orders/{checkout['id']}/cancel"
    cancelled = admin.post(url, body, format="json")
    assert cancelled.status_code == 200
    expected_note = "Bank receipt VN12345" if explicit_note is None else explicit_note
    assert cancelled.data["data"]["reconciliation_note"] == expected_note
    assert cancelled.data["data"]["payment_status"] == "refund_required"
    fresh = admin.get(f"/api/v1/admin/store-orders/{checkout['id']}")
    assert fresh.data["data"]["reconciliation_note"] == expected_note
    replay = admin.post(url, body, format="json")
    assert replay.status_code == 200
    assert replay.data["data"]["reconciliation_note"] == expected_note
    assert StoreOrderAction.objects.filter(order_id=checkout["id"], action="cancel").count() == 1
    assert (
        StoreOrderAction.objects.get(order_id=checkout["id"], action="record_payment").context[
            "note"
        ]
        == "Bank receipt VN12345"
    )


def test_legacy_reason_only_cancel_keeps_original_note_behavior(checkout, admin):
    StoreOrder.objects.filter(pk=checkout["id"]).update(
        workflow_version=1, reconciliation_note="Original legacy note"
    )
    result = act(admin, checkout, "cancel", reason="Legacy cancel")
    assert result.status_code == 200
    assert result.data["data"]["reconciliation_note"] == ""


def test_v2_cancel_replays_an_accepted_old_blank_note_key_without_rewriting_history(
    checkout, admin
):
    from commerce import store_orders as services
    from commerce.models import StoreOrderAction

    key = uuid4()
    row = StoreOrder.objects.get(pk=checkout["id"])
    row.status = "cancelled"
    row.reconciliation_note = "Existing historical summary"
    row.save(update_fields=["status", "reconciliation_note"])
    StoreOrderAction.objects.create(
        order=row,
        action="cancel",
        idempotency_key=key,
        request_fingerprint=services.request_fingerprint(
            {"reason": "Old cancel", "reconciliation_note": ""}
        ),
        context={"reason": "Old cancel", "reconciliation_note": ""},
    )
    result = admin.post(
        f"/api/v1/admin/store-orders/{row.pk}/cancel",
        {"idempotency_key": str(key), "reason": "Old cancel"},
        format="json",
    )
    assert result.status_code == 200
    assert result.data["data"]["reconciliation_note"] == "Existing historical summary"
    assert StoreOrderAction.objects.filter(order=row, action="cancel").count() == 1


def test_payment_receipt_confirms_already_received_and_rejects_another_payload(checkout, admin):
    assert approve(admin, checkout).status_code == 200
    original = act(admin, checkout, "record-payment", note="Funds verified")
    assert original.status_code == 200
    received_at = original.data["data"]["payment_received_at"]
    key = str(uuid4())

    for response in (
        act(admin, checkout, "record-payment", note="Second check", idempotency_key=key),
        act(admin, checkout, "record-payment", note="Second check", idempotency_key=key),
    ):
        assert_admin_action_receipt(response, checkout["id"], "record_payment", key)
        assert response.data["data"]["payment_received_at"] == received_at
    entry = StoreOrderAction.objects.get(
        order_id=checkout["id"], action="record_payment", idempotency_key=key
    )
    assert entry.context["already_received"] is True
    assert (
        StoreOrderAction.objects.filter(order_id=checkout["id"], action="record_payment").count()
        == 2
    )

    conflict = act(admin, checkout, "record-payment", note="Different note", idempotency_key=key)
    assert conflict.status_code == 409
    assert conflict.data["errors"][0]["code"] == "store_idempotency_conflict"
    assert "meta" not in conflict.data


@pytest.mark.parametrize("mismatch", ["order", "action", "key"])
def test_admin_mutation_requires_an_exact_persisted_receipt(
    checkout, admin, store_product, monkeypatch, mismatch
):
    order = StoreOrder.objects.get(pk=checkout["id"])
    key = uuid4()
    record_order = order
    if mismatch == "order":
        other = APIClient().post(
            "/api/v1/public/store-checkouts",
            guest_payload(store_product, expected_available=8),
            format="json",
        )
        assert other.status_code == 201
        record_order = StoreOrder.objects.get(pk=other.data["data"]["id"])
    record = StoreOrderAction.objects.create(
        order=record_order,
        action="record_payment" if mismatch == "action" else "cancel",
        idempotency_key=uuid4() if mismatch == "key" else key,
        request_fingerprint="fixture",
    )
    # Model an invariant failure after the service returns; the view cannot
    # trust the request fields or a neighboring action as confirmation.
    monkeypatch.setattr("commerce.store_views.cancel_store_order", lambda **_kwargs: order)
    response = act(admin, checkout, "cancel", reason="Harvest shortage", idempotency_key=str(key))
    assert response.status_code == 500
    assert "meta" not in response.data
    assert StoreOrderAction.objects.filter(pk=record.pk).exists()
    assert not StoreOrderAction.objects.filter(
        order=order, action="cancel", idempotency_key=key
    ).exists()


def test_rejected_and_rolled_back_actions_do_not_return_a_receipt(checkout, admin, monkeypatch):
    invalid = act(admin, checkout, "cancel", reason="")
    assert invalid.status_code == 400
    assert "meta" not in invalid.data

    def fail_after_action_write(**_kwargs):
        raise StoreOrderError("store_requote_required")

    monkeypatch.setattr("commerce.store_orders.release_coupon", fail_after_action_write)
    rolled_back = act(admin, checkout, "cancel", reason="Harvest shortage")
    assert rolled_back.status_code == 409
    assert "meta" not in rolled_back.data

    order = StoreOrder.objects.get(pk=checkout["id"])
    assert order.status == "pending_review"
    assert order.lines.get().stock_reservations.filter(released_at__isnull=True).exists()
    assert not StoreOrderAction.objects.filter(order=order).exists()


def test_store_receipt_is_not_available_to_an_authenticated_non_admin(checkout, active_user):
    client = APIClient()
    client.force_authenticate(
        active_user, token=SimpleNamespace(scope="profile email content:manage")
    )
    denied = act(client, checkout, "cancel", reason="Harvest shortage")
    assert denied.status_code == 403
    assert "meta" not in denied.data
    assert not StoreOrderAction.objects.filter(order_id=checkout["id"]).exists()
