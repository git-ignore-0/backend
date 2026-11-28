import json
import uuid
from dataclasses import replace
from datetime import timedelta

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.inventory import inventory_capacity
from commerce.models import (
    StoreOrder,
    StoreOrderAction,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockReservation,
)
from commerce.store_orders import request_fingerprint
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from tests.test_single_farm_inventory import strand_supply, supply
from tests.test_store_orders import assert_admin_action_receipt, guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


@pytest.fixture
def batches(store_product):
    original = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    other_farm = Farm.objects.create(
        name_vi=original.farm_product.farm.name_vi,
        name_en=original.farm_product.farm.name_en,
        location_vi="Huế",
        location_en="Hue",
    )
    other = FarmProduct.objects.create(farm=other_farm, product=store_product)
    early = other.stock_batches.create(
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=1),
        quantity_units=2,
    )
    late = other.stock_batches.create(
        start_date=early.start_date,
        end_date=early.end_date + timedelta(days=1),
        quantity_units=3,
    )
    return original, early, late


def _checkout(product, *, quantity=4, expected_available=10):
    response = APIClient().post(
        "/api/v1/public/store-checkouts",
        guest_payload(product, quantity=quantity, expected_available=expected_available),
        format="json",
    )
    assert response.status_code == 201, response.json()
    return StoreOrder.objects.get(pk=response.json()["data"]["id"])


@pytest.fixture
def order(store_product, payment_settings):
    return _checkout(store_product)


def _path(order):
    return f"/api/v1/admin/store-orders/{order.pk}"


def _source(line, batch):
    return {"line_id": str(line.pk), "farm_id": str(batch.farm_product.farm_id)}


def _allocation(line, batch, quantity):
    return {"line_id": str(line.pk), "batch_id": str(batch.pk), "quantity": quantity}


def _assert_uncommitted(order):
    order.refresh_from_db()
    assert order.status == StoreOrderStatus.PENDING_REVIEW
    assert not StoreStockAllocation.objects.filter(line__order=order).exists()
    assert not StoreStockMovement.objects.filter(line__order=order).exists()
    assert not StoreOrderAction.objects.filter(order=order, action="approve").exists()
    assert StoreStockReservation.objects.filter(
        line__order=order, released_at__isnull=True
    ).exists()


def _public_quantity_and_quote(client, product, payload, expected):
    catalog = "/api/v1/public/store-products"
    listing = client.get(catalog, {"locale": "vi"})
    detail = client.get(f"{catalog}/{product.slug}", {"locale": "vi"})
    assert listing.status_code == detail.status_code == 200
    assert listing.json()["data"][0]["available_quantity"] == expected
    assert detail.json()["data"]["available_quantity"] == expected
    quote = client.post(
        "/api/v1/public/store-quotes",
        {
            "locale": payload["locale"],
            "delivery_date": payload["delivery_date"],
            "guest": payload["guest"],
            "lines": [
                {key: line[key] for key in ("product_id", "quantity", "use_quota")}
                for line in payload["lines"]
            ],
        },
        format="json",
    )
    if not expected:
        assert quote.status_code == 409
        assert quote.json()["errors"][0]["code"] == "store_stock_changed"
    else:
        assert quote.status_code == 200, quote.json()
        assert quote.json()["data"]["lines"] == payload["lines"]


@pytest.mark.parametrize("stock", [0, 1, 999, 1000, 2_147_483_647, 2_147_483_648, 3_000_000_000])
def test_public_availability_matches_checkout_and_replay(
    admin, store_product, payment_settings, stock
):
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    if stock == 0:
        FarmProductStockBatch.objects.filter(pk=batch.pk).update(
            start_date=timezone.localdate() - timedelta(days=2),
            end_date=timezone.localdate() - timedelta(days=1),
        )
    else:
        FarmProductStockBatch.objects.filter(pk=batch.pk).update(
            quantity_units=min(stock, 1_500_000_000)
        )
        if stock > 1_500_000_000:
            batch.farm_product.stock_batches.create(
                start_date=batch.start_date,
                end_date=batch.end_date,
                quantity_units=stock - 1_500_000_000,
            )
    if stock == 3_000_000_000:
        supply(store_product, "Smaller Farm", 1_000_000_000)
    client = APIClient()
    payload = guest_payload(store_product, quantity=1, expected_available=stock)
    _public_quantity_and_quote(client, store_product, payload, stock)
    url = "/api/v1/public/store-checkouts"
    response = client.post(url, payload, format="json")
    if not stock:
        assert response.status_code == 409
        assert response.json()["errors"][0]["code"] == "store_stock_changed"
        return
    assert response.status_code == 201, response.json()
    order = StoreOrder.objects.get(pk=response.json()["data"]["id"])
    hold = StoreStockReservation.objects.get(line__order=order)
    assert (hold.farm_id, hold.quantity) == (batch.farm_product.farm_id, 1)
    replay = client.post(url, payload, format="json")
    assert replay.status_code == 200
    assert replay.json()["data"]["id"] == str(order.pk)
    assert StoreStockReservation.objects.filter(line__order=order).count() == 1
    if stock == 2_147_483_647:
        for legacy_value in (str(stock), float(stock)):
            prior_shape = {
                **payload,
                "lines": [{**payload["lines"][0], "expected_available_quantity": legacy_value}],
            }
            replay = client.post(url, prior_shape, format="json")
            assert replay.status_code == 200
            assert replay.json()["data"]["id"] == str(order.pk)
    if stock == 3_000_000_000:
        line = order.lines.get()
        inventory = admin.get(_path(order)).json()["data"]["source_inventory"]["lines"][0]
        selected = next(farm for farm in inventory["farms"] if farm["farm_id"] == str(hold.farm_id))
        assert selected["available_quantity"] == stock
        approved = admin.post(
            f"{_path(order)}/approve",
            {"idempotency_key": str(uuid.uuid4()), "sources": [_source(line, batch)]},
            format="json",
        )
        assert approved.status_code == 200, approved.json()


@pytest.mark.parametrize("physical", [9_007_199_254_740_991, 9_007_199_254_740_999])
def test_public_api_saturates_only_the_wire_availability(
    store_product, payment_settings, monkeypatch, physical
):
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    key = (store_product.pk, batch.farm_product.farm_id)

    def with_large_stock(*args, **kwargs):
        capacity = inventory_capacity(*args, **kwargs)
        return replace(
            capacity,
            physical={**capacity.physical, key: physical},
            free={**capacity.free, key: physical},
        )

    monkeypatch.setattr("commerce.public_catalog.inventory_capacity", with_large_stock)
    monkeypatch.setattr("commerce.store_orders.inventory_capacity", with_large_stock)
    client = APIClient()
    expected = 9_007_199_254_740_991
    payload = guest_payload(store_product, quantity=1, expected_available=expected)
    _public_quantity_and_quote(client, store_product, payload, expected)
    url = "/api/v1/public/store-checkouts"
    for invalid in [-1, 0.25, True, "invalid", str(expected), float(expected), expected + 1]:
        wrong = guest_payload(store_product, quantity=1, expected_available=invalid)
        assert client.post(url, wrong, format="json").status_code == 400
    fractional = json.dumps(payload, default=str).replace(str(expected), "9007199254740990.6")
    assert client.post(url, fractional, content_type="application/json").status_code == 400
    wrong = guest_payload(store_product, quantity=1000, expected_available=expected)
    assert client.post(url, wrong, format="json").status_code == 400
    stale = guest_payload(store_product, quantity=1, expected_available=expected - 1)
    conflict = client.post(url, stale, format="json")
    assert conflict.status_code == 409
    assert conflict.json()["errors"][0]["context"]["available_quantity"] == expected
    assert client.post(url, payload, format="json").status_code == 201
    assert FarmProductStockBatch.objects.get(pk=batch.pk).quantity_units == batch.quantity_units


def test_public_and_admin_share_six_from_two_batches_at_one_farm(
    admin, store_product, payment_settings, batches
):
    original, early, late = batches
    FarmProductStockBatch.objects.filter(pk=original.pk).update(quantity_units=4)
    FarmProductStockBatch.objects.filter(pk__in=(early.pk, late.pk)).update(quantity_units=3)
    client = APIClient()
    payload = guest_payload(store_product, quantity=6, expected_available=6)
    _public_quantity_and_quote(client, store_product, payload, 6)
    url = "/api/v1/public/store-checkouts"
    rejected = client.post(
        url, guest_payload(store_product, quantity=7, expected_available=6), format="json"
    )
    assert rejected.status_code == 409
    assert rejected.json()["errors"][0]["code"] == "store_stock_changed"
    assert not StoreStockReservation.objects.exists()

    order = _checkout(store_product, quantity=6, expected_available=6)
    line = order.lines.get()
    farm_id = str(early.farm_product.farm_id)
    detail = admin.get(_path(order)).json()["data"]
    projection = detail["source_inventory"]["lines"][0]
    assert projection["assigned_farm_id"] == projection["suggested_farm_id"] == farm_id
    assert [farm["farm_id"] for farm in projection["farms"] if farm["eligible"]] == [farm_id]
    assert detail["allocation_suggestion"] == [
        _allocation(line, early, 3),
        _allocation(line, late, 3),
    ]
    result = admin.post(
        f"{_path(order)}/approve",
        {"idempotency_key": str(uuid.uuid4()), "sources": [_source(line, early)]},
        format="json",
    )
    assert result.status_code == 200, result.json()
    assert list(
        line.stock_allocations.order_by("batch__end_date").values_list("batch_id", "quantity")
    ) == [(early.pk, 3), (late.pk, 3)]


def test_projection_prefers_eligible_assignment_before_inventory_order(
    admin, store_product, payment_settings, batches
):
    original, early, _ = batches
    FarmProductStockBatch.objects.filter(pk=original.pk).update(end_date=timezone.localdate())
    order = _checkout(store_product, quantity=4)
    line = order.lines.get()
    original_farm = original.farm_product.farm_id
    backup_farm = early.farm_product.farm_id
    FarmProductStockBatch.objects.filter(pk=original.pk).update(
        end_date=timezone.localdate() + timedelta(days=3)
    )
    assert (
        inventory_capacity([store_product.pk], now=timezone.now(), exclude_order=order).choose_farm(
            store_product.pk, line.quantity
        )
        == backup_farm
    )
    for assigned, expected in [(original_farm, original_farm), (None, backup_farm)]:
        StoreStockReservation.objects.filter(line=line).update(farm_id=assigned)
        response = admin.get(_path(order))
        assert response.status_code == 200
        projected = response.json()["data"]["source_inventory"]["lines"][0]
        assert all(farm["eligible"] for farm in projected["farms"])
        assert projected["suggested_farm_id"] == str(expected)


@pytest.mark.parametrize(
    ("change", "assigned", "has_backup"),
    [
        ("farm", True, True),
        ("farm", False, True),
        ("farm", True, False),
        ("batch", True, True),
    ],
)
def test_source_projection_uses_only_eligible_options_after_stock_reads(
    admin, store_product, payment_settings, batches, monkeypatch, change, assigned, has_backup
):
    original, early, late = batches
    FarmProductStockBatch.objects.filter(pk=original.pk).update(end_date=timezone.localdate())
    order = _checkout(store_product, quantity=4)
    line = order.lines.get()
    original_farm = original.farm_product.farm_id
    backup_farm = early.farm_product.farm_id
    if not assigned:
        StoreStockReservation.objects.filter(line=line).update(farm=None)
    if not has_backup:
        Farm.objects.filter(pk=backup_farm).update(is_active=False)

    read_count = 0

    def read_then_change(*args, **kwargs):
        nonlocal read_count
        read_count += 1
        capacity = inventory_capacity(*args, **kwargs)
        assert capacity.choose_farm(store_product.pk, line.quantity) == original_farm
        if change == "farm":
            Farm.objects.filter(pk=original_farm).update(is_active=False)
        else:
            FarmProductStockBatch.objects.filter(pk=original.pk).update(quantity_units=1)
        return capacity

    monkeypatch.setattr("commerce.store_orders.inventory_capacity", read_then_change)
    response = admin.get(_path(order))
    assert response.status_code == 200, response.json()
    assert read_count == 1
    data = response.json()["data"]
    inventory = data["source_inventory"]
    projected = inventory["lines"][0]
    assert projected["line_id"] == str(line.pk)
    assert projected["product_id"] == str(store_product.pk)
    assert projected["required_quantity"] == 4
    assert projected["assigned_farm_id"] == (str(original_farm) if assigned else None)
    farms = {farm["farm_id"]: farm for farm in projected["farms"]}
    assert len(farms) == len(projected["farms"])
    if assigned or change == "batch":
        assert not farms[str(original_farm)]["eligible"]
        assert farms[str(original_farm)]["available_quantity"] == (1 if change == "batch" else 0)
    else:
        assert str(original_farm) not in farms
    if has_backup:
        suggested = projected["suggested_farm_id"]
        assert suggested == str(backup_farm)
        assert farms[suggested]["eligible"] and farms[suggested]["available_quantity"] >= 4
        assert data["allocation_suggestion"] == [
            _allocation(line, early, 2),
            _allocation(line, late, 2),
        ]
    else:
        assert projected["suggested_farm_id"] is None
        assert data["allocation_suggestion"] == []
    assert all(row["farm_id"] in farms for row in inventory["candidates"])


def test_approval_switches_farm_and_allocates_current_batches_in_expiry_order(
    admin, store_product, payment_settings, batches
):
    original, early, late = batches
    FarmProductStockBatch.objects.filter(pk=original.pk).update(
        quantity_units=11, end_date=timezone.localdate()
    )
    order = _checkout(store_product, quantity=5, expected_available=11)
    other = _checkout(store_product, quantity=6, expected_available=6)
    line = order.lines.get()
    farm_a = str(original.farm_product.farm_id)
    farm_b = str(early.farm_product.farm_id)
    # Farm A has enough only after removing the order's own reservation.
    detail = admin.get(_path(order)).json()["data"]
    projected = detail["source_inventory"]["lines"][0]
    assert projected == {
        "line_id": str(line.pk),
        "product_id": str(store_product.pk),
        "required_quantity": 5,
        "assigned_farm_id": farm_a,
        "suggested_farm_id": farm_a,
        "farms": sorted(
            [
                {
                    "farm_id": farm,
                    "farm_name": "Nông trại A",
                    "available_quantity": 5,
                    "eligible": True,
                }
                for farm in (farm_a, farm_b)
            ],
            key=lambda farm: farm["farm_id"],
        ),
    }
    assert detail["allocation_suggestion"] == [_allocation(line, original, 5)]

    # A faster-expiring historical row may not win the allocation plan.
    FarmProduct.objects.filter(pk=early.farm_product_id).update(supply_generation=2)
    FarmProductStockBatch.objects.filter(pk__in=[early.pk, late.pk]).update(supply_generation=2)
    early.farm_product.stock_batches.create(
        start_date=timezone.localdate(), end_date=timezone.localdate(), quantity_units=20
    )
    key = uuid.uuid4()
    payload = {"idempotency_key": str(key), "sources": [_source(line, early)]}
    result = admin.post(f"{_path(order)}/approve", payload, format="json")
    assert_admin_action_receipt(result, order.pk, "approve", key)
    assert result.json()["data"]["status"] == StoreOrderStatus.PAYMENT_PENDING
    assert set(StoreStockAllocation.objects.values_list("batch_id", "quantity")) == {
        (early.pk, 2),
        (late.pk, 3),
    }
    assert StoreStockMovement.objects.filter(idempotency_key=key, kind="sale").count() == 2
    assert StoreStockReservation.objects.get(line=line).release_reason == "approved"
    assert StoreStockReservation.objects.get(line__order=other).released_at is None

    replay = admin.post(f"{_path(order)}/approve", payload, format="json")
    assert_admin_action_receipt(replay, order.pk, "approve", key)
    conflicting = {**payload, "sources": [_source(line, original)]}
    result = admin.post(f"{_path(order)}/approve", conflicting, format="json")
    assert result.status_code == 409
    assert result.json()["errors"] == [{"code": "store_idempotency_conflict"}]
    assert StoreStockMovement.objects.filter(idempotency_key=key).count() == 2


@pytest.mark.parametrize("problem", ["short", "inactive", "missing", "no_offer"])
def test_approval_rechecks_requested_farm_and_keeps_reservation_on_failure(
    admin, order, batches, problem
):
    original, early, late = batches
    line = order.lines.get()
    source = _source(line, early)
    if problem == "short":
        FarmProductStockBatch.objects.filter(pk=late.pk).update(quantity_units=1)
    elif problem == "inactive":
        Farm.objects.filter(pk=source["farm_id"]).update(is_active=False)
    elif problem == "missing":
        source["farm_id"] = str(uuid.uuid4())
    else:
        source["farm_id"] = str(Farm.objects.create(name_vi="Không có hàng").pk)
    response = admin.post(
        f"{_path(order)}/approve",
        {"idempotency_key": str(uuid.uuid4()), "sources": [source]},
        format="json",
    )

    assert response.status_code == 409
    assert response["Cache-Control"] == "private, no-store"
    assert response.json()["errors"] == [
        {
            "code": "store_allocation_stale",
            "context": {
                **source,
                "product_id": str(line.product_id),
                "available_quantity": 3 if problem == "short" else 0,
            },
        }
    ]
    _assert_uncommitted(order)
    assert StoreStockReservation.objects.get(line=line).farm_id == original.farm_product.farm_id


@pytest.mark.parametrize("problem", ["empty", "duplicate", "invalid_uuid", "extra_field", "mixed"])
def test_approval_serializer_rejects_malformed_source_payloads(admin, order, batches, problem):
    batch, *_ = batches
    line = order.lines.get()
    source = _source(line, batch)
    payload = {"idempotency_key": str(uuid.uuid4()), "sources": [source]}
    if problem == "empty":
        payload["sources"] = []
    elif problem == "duplicate":
        payload["sources"].append(source)
    elif problem == "invalid_uuid":
        source["farm_id"] = "farm"
    elif problem == "extra_field":
        source["quantity"] = line.quantity
    else:
        payload["allocations"] = [_allocation(line, batch, line.quantity)]

    response = admin.post(f"{_path(order)}/approve", payload, format="json")

    assert response.status_code == 400
    _assert_uncommitted(order)


def test_uncommitted_legacy_split_is_rejected_but_historical_fingerprint_replays(
    admin, active_user, order, batches
):
    original, early, _ = batches
    line = order.lines.get()
    key = uuid.uuid4()
    legacy = [_allocation(line, original, 2), _allocation(line, early, 2)]
    payload = {"idempotency_key": str(key), "allocations": legacy}
    response = admin.post(f"{_path(order)}/approve", payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "store_allocation_mixed_farm"
    _assert_uncommitted(order)

    # Write the rows an old version would have committed, without today's service.
    for batch in (original, early):
        allocation = StoreStockAllocation.objects.create(line=line, batch=batch, quantity=2)
        StoreStockMovement.objects.create(
            allocation=allocation,
            line=line,
            batch=batch,
            kind="sale",
            quantity=2,
            idempotency_key=key,
            actor=active_user,
        )
    StoreOrderAction.objects.create(
        order=order,
        action="approve",
        idempotency_key=key,
        request_fingerprint=request_fingerprint({"allocations": legacy}),
        actor=active_user,
        context={"allocations": legacy},
    )
    StoreStockReservation.objects.filter(line=line).update(released_at=timezone.now())
    StoreOrder.objects.filter(pk=order.pk).update(
        status=StoreOrderStatus.PAYMENT_PENDING, approved_at=timezone.now()
    )
    response = admin.post(f"{_path(order)}/approve", payload, format="json")
    assert_admin_action_receipt(response, order.pk, "approve", key)
    assert {row["farm_id"] for row in response.json()["data"]["allocations"]} == {
        str(original.farm_product.farm_id),
        str(early.farm_product.farm_id),
    }

    payload["allocations"] = list(reversed(legacy))
    response = admin.post(f"{_path(order)}/approve", payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "store_idempotency_conflict"}]
    assert StoreStockMovement.objects.filter(idempotency_key=key).count() == 2


def test_sources_cover_every_line_and_replay_independent_of_source_order(
    admin, store_product, payment_settings
):
    from tests.test_public_store_catalog_api import make_product

    second = make_product("other-vegetable", store_product.category)
    farm = Farm.objects.create(name_vi="Nguồn thứ hai", name_en="Second source")
    second_source = FarmProduct.objects.create(farm=farm, product=second)
    second_batch = FarmProductStockBatch.objects.create(
        farm_product=second_source,
        quantity_units=3,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=2),
    )
    first_batch = store_product.farm_offers.get().stock_batches.get()
    payload = guest_payload(store_product)
    payload["lines"].append(
        {
            "product_id": str(second.pk),
            "quantity": 1,
            "use_quota": False,
            "expected_unit_price_vnd": second.retail_price_vnd,
            "expected_available_quantity": 3,
            "expected_quota_quantity": 0,
        }
    )
    payload["expected_total_vnd"] += second.retail_price_vnd
    created = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
    assert created.status_code == 201, created.json()
    order = StoreOrder.objects.get(pk=created.json()["data"]["id"])
    first, last = order.lines.order_by("position")
    assert first.stock_reservations.get().farm_id == first_batch.farm_product.farm_id
    assert last.stock_reservations.get().farm_id == second_source.farm_id
    sources = [_source(first, first_batch), _source(last, second_batch)]
    key = uuid.uuid4()
    missing = admin.post(
        f"{_path(order)}/approve",
        {"idempotency_key": str(key), "sources": sources[:1]},
        format="json",
    )
    assert missing.status_code == 409
    assert missing.json()["errors"][0]["code"] == "store_allocation_incomplete"
    foreign = {**sources[0], "line_id": str(uuid.uuid4())}
    stale = admin.post(
        f"{_path(order)}/approve",
        {"idempotency_key": str(key), "sources": [*sources, foreign]},
        format="json",
    )
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "store_allocation_stale"
    _assert_uncommitted(order)

    for submitted in (sources, list(reversed(sources))):
        approved = admin.post(
            f"{_path(order)}/approve",
            {"idempotency_key": str(key), "sources": submitted},
            format="json",
        )
        assert_admin_action_receipt(approved, order.pk, "approve", key)
    assert list(first.stock_allocations.values_list("batch_id", "quantity")) == [
        (first_batch.pk, 2)
    ]
    assert list(last.stock_allocations.values_list("batch_id", "quantity")) == [
        (second_batch.pk, 1)
    ]
    assert StoreStockMovement.objects.filter(idempotency_key=key).count() == 2


def test_admin_can_recover_each_live_hold_from_an_expired_farm(
    admin, store_product, payment_settings
):
    original = store_product.farm_offers.get()
    first = _checkout(store_product, quantity=4)
    second = _checkout(store_product, quantity=4, expected_available=6)
    replacement = Farm.objects.create(name_vi="Nguồn thay thế")
    new_offer = FarmProduct.objects.create(farm=replacement, product=store_product)
    batch = FarmProductStockBatch.objects.create(
        farm_product=new_offer,
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=1),
        quantity_units=8,
    )
    original.stock_batches.update(end_date=timezone.localdate() - timedelta(days=1))
    assert StoreStockReservation.objects.filter(farm_id=original.farm_id).count() == 2
    for order in (first, second):
        line = order.lines.get()
        detail = admin.get(_path(order)).json()["data"]["source_inventory"]["lines"][0]
        assert detail["assigned_farm_id"] == str(original.farm_id)
        assert detail["suggested_farm_id"] == str(replacement.pk)

        key = uuid.uuid4()
        result = admin.post(
            f"{_path(order)}/approve",
            {"idempotency_key": str(key), "sources": [_source(line, batch)]},
            format="json",
        )
        assert_admin_action_receipt(result, order.pk, "approve", key)
        assert line.stock_reservations.get().released_at is not None
    assert list(batch.store_stock_movements.values_list("kind", "quantity")) == [
        ("sale", 4),
        ("sale", 4),
    ]


@pytest.mark.parametrize("backup_sizes", [(4,), (2, 2)])
def test_partial_recovery_is_explicit_and_never_combines_farms(
    admin, store_product, payment_settings, backup_sizes
):
    from commerce.inventory import inventory_capacity
    from tests.test_single_farm_inventory import supply

    original = store_product.farm_offers.get()
    first = _checkout(store_product, quantity=4)
    second = _checkout(store_product, quantity=4, expected_available=6)
    backups = [supply(store_product, f"Backup {i}", size) for i, size in enumerate(backup_sizes)]
    original.stock_batches.update(end_date=timezone.localdate() - timedelta(days=1))
    capacity = inventory_capacity([store_product.pk], now=timezone.now())
    assert capacity.maximum(store_product.pk) == 0
    assert capacity.stranded_hold_counts[store_product.pk] == 2

    selected = second.lines.get()
    batch = backups[0].products.get().stock_batches.get()
    projection = admin.get(_path(second)).json()["data"]["source_inventory"]["lines"][0]
    response = admin.post(
        f"{_path(second)}/approve",
        {"idempotency_key": str(uuid.uuid4()), "sources": [_source(selected, batch)]},
        format="json",
    )
    if backup_sizes == (4,):
        assert projection["suggested_farm_id"] == str(backups[0].pk)
        assert response.status_code == 200
        assert selected.stock_allocations.get().quantity == 4
    else:
        assert projection["suggested_farm_id"] is None
        assert response.status_code == 409
        _assert_uncommitted(second)
    _assert_uncommitted(first)
    hold = first.lines.get().stock_reservations.get()
    assert hold.farm_id == original.farm_id
    assert hold.quantity == 4


def test_partial_recovery_preserves_healthy_and_legacy_holds(
    admin, store_product, payment_settings
):
    from tests.test_single_farm_inventory import supply

    original = store_product.farm_offers.get()
    stranded = _checkout(store_product, quantity=4)
    _checkout(store_product, quantity=4, expected_available=6)
    backup = supply(store_product, "Backup", 10)
    healthy = _checkout(store_product, quantity=6, expected_available=10)
    legacy = _checkout(store_product, quantity=1, expected_available=4)
    legacy_hold = legacy.lines.get().stock_reservations.get()
    StoreStockReservation.objects.filter(pk=legacy_hold.pk).update(farm_id=None)
    original.stock_batches.update(end_date=timezone.localdate() - timedelta(days=1))
    batch = backup.products.get().stock_batches.get()

    def approve_from_backup(order):
        return admin.post(
            f"{_path(order)}/approve",
            {
                "idempotency_key": str(uuid.uuid4()),
                "sources": [_source(order.lines.get(), batch)],
            },
            format="json",
        )

    assert approve_from_backup(stranded).status_code == 409
    _assert_uncommitted(stranded)
    assert approve_from_backup(healthy).status_code == 200
    assert healthy.lines.get().stock_allocations.get().quantity == 6
    assert approve_from_backup(stranded).status_code == 409
    _assert_uncommitted(stranded)
    legacy_hold.refresh_from_db()
    assert (legacy_hold.farm_id, legacy_hold.quantity, legacy_hold.released_at) == (None, 1, None)


def test_partial_recovery_keeps_legacy_stock_when_removing_own_hold_restores_a_farm(
    admin, store_product, payment_settings
):
    from tests.test_single_farm_inventory import supply

    original = store_product.farm_offers.get()
    original.stock_batches.update(quantity_units=13)
    first = _checkout(store_product, quantity=4, expected_available=13)
    other = _checkout(store_product, quantity=4, expected_available=9)
    legacy = _checkout(store_product, quantity=5, expected_available=5)
    legacy_hold = legacy.lines.get().stock_reservations.get()
    StoreStockReservation.objects.filter(pk=legacy_hold.pk).update(farm_id=None)
    today = timezone.localdate()
    original.stock_batches.update(end_date=today - timedelta(days=1))
    original.stock_batches.create(start_date=today, end_date=today, quantity_units=5)
    backup = supply(store_product, "Backup", 4)
    batch = backup.products.get().stock_batches.get()

    response = admin.post(
        f"{_path(first)}/approve",
        {
            "idempotency_key": str(uuid.uuid4()),
            "sources": [_source(first.lines.get(), batch)],
        },
        format="json",
    )
    assert response.status_code == 409
    _assert_uncommitted(first)
    _assert_uncommitted(other)
    _assert_uncommitted(legacy)


@pytest.mark.parametrize(
    ("assignment", "legacy_shape"),
    [
        ("healthy", False),
        ("stranded", False),
        ("healthy", True),
        ("stranded", True),
        ("unassigned", True),
    ],
    ids=["healthy", "stranded", "healthy-with-legacy", "stranded-with-legacy", "unassigned"],
)
def test_approval_preserves_own_capacity_from_a_verified_plan(
    admin, access_token, store_product, payment_settings, monkeypatch, assignment, legacy_shape
):
    product = store_product
    original = product.farm_offers.get().stock_batches.get()
    stranded = assignment != "healthy"
    if stranded:
        first = _checkout(product)
    else:
        FarmProductStockBatch.objects.filter(pk=original.pk).update(quantity_units=6)
    second = _checkout(product, quantity=3, expected_available=6)
    third = _checkout(product, quantity=3, expected_available=3)
    farm_a = supply(product, "Farm A", 4, farm_id=uuid.UUID(int=1))
    batch_a = farm_a.products.get().stock_batches.get()
    farm_b = supply(product, "Farm B", 6, farm_id=uuid.UUID(int=2))
    if not stranded:
        FarmProductStockBatch.objects.filter(pk=original.pk).update(end_date=timezone.localdate())
        batch_a.end_date = timezone.localdate() + timedelta(days=1)
        batch_a.save(update_fields=["end_date"])
        first = _checkout(product, expected_available=6)
    if legacy_shape:
        third.lines.get().stock_reservations.update(farm=None)
    if assignment == "unassigned":
        first.lines.get().stock_reservations.update(farm=None)

    own = first.lines.get().stock_reservations.get()
    assert (
        own.farm_id
        == {
            "healthy": farm_a.pk,
            "stranded": original.farm_product.farm_id,
            "unassigned": None,
        }[assignment]
    )
    orders = [first, second, third]
    others = StoreStockReservation.objects.filter(line__order__in=[second, third])
    other_holds = dict(others.values_list("pk", "farm_id"))
    strand_supply(original, monkeypatch)
    StoreOrder.objects.filter(pk__in=[order.pk for order in orders]).update(
        expires_at=timezone.now() + timedelta(hours=12)
    )
    access_token.expires = timezone.now() + timedelta(hours=12)
    access_token.save(update_fields=["expires"])
    before = inventory_capacity([product.pk], now=timezone.now())
    assert before.recovery_shortfalls == {}
    assert before.maximum(product.pk) == 0

    detail = admin.get(_path(first))
    assert detail.status_code == 200, detail.json()
    projection = detail.json()["data"]["source_inventory"]["lines"][0]
    options = {row["farm_id"]: row for row in projection["farms"]}
    key = uuid.uuid4()
    line = own.line
    payload = {"idempotency_key": str(key)}
    if legacy_shape:
        payload["allocations"] = [_allocation(line, batch_a, 4)]
    else:
        payload["sources"] = [_source(line, batch_a)]
    response = admin.post(f"{_path(first)}/approve", payload, format="json")
    assert (
        options[str(farm_a.pk)]["eligible"],
        projection["suggested_farm_id"],
        response.status_code,
    ) == (
        True,
        str(farm_a.pk),
        200,
    )
    assert options[str(farm_b.pk)]["eligible"] is False
    assert_admin_action_receipt(response, first.pk, "approve", key)
    assert_admin_action_receipt(
        admin.post(f"{_path(first)}/approve", payload, format="json"), first.pk, "approve", key
    )
    assert first.actions.filter(action="approve").count() == 1
    assert list(line.stock_allocations.values_list("batch_id", "quantity")) == [(batch_a.pk, 4)]
    assert list(StoreStockMovement.objects.filter(line=line).values_list("kind", "quantity")) == [
        ("sale", 4)
    ]
    assert dict(others.filter(released_at__isnull=True).values_list("pk", "farm_id")) == other_holds
    assert list(others.order_by("pk").values_list("quantity", flat=True)) == [3, 3]
    after = inventory_capacity([product.pk], now=timezone.now())
    assert after.recovery_shortfalls == {}
    assert after.maximum(product.pk) == 0


@pytest.mark.parametrize("legacy_shape", [False, True], ids=["sources", "allocations"])
def test_approval_rechecks_stock_after_consuming_a_partially_deficient_farm(
    admin, access_token, store_product, payment_settings, monkeypatch, legacy_shape
):
    now = timezone.now()
    monkeypatch.setattr(timezone, "now", lambda: now)
    product = store_product
    product.farm_offers.get().stock_batches.all().delete()
    farm_a = supply(product, "Farm A", 4, farm_id=uuid.UUID(int=1))
    source = farm_a.products.get()
    batch_a = source.stock_batches.get()
    today = timezone.localdate()
    expiring = source.stock_batches.create(start_date=today, end_date=today, quantity_units=2)
    first = _checkout(product, quantity=4, expected_available=6)
    other = _checkout(product, quantity=2, expected_available=2)
    own = first.lines.get().stock_reservations.get()
    other_hold = other.lines.get().stock_reservations.get()
    assert own.farm_id == other_hold.farm_id == farm_a.pk
    farm_b = supply(product, "Farm B", 2, farm_id=uuid.UUID(int=2))
    strand_supply(expiring, monkeypatch)
    expiry = timezone.now() + timedelta(hours=12)
    StoreOrder.objects.filter(pk__in=[first.pk, other.pk]).update(expires_at=expiry)
    access_token.expires = expiry
    access_token.save(update_fields=["expires"])

    before = inventory_capacity([product.pk], now=timezone.now())
    assert before.physical == {(product.pk, farm_a.pk): 4, (product.pk, farm_b.pk): 2}
    assert before.assigned == {(product.pk, farm_a.pk): 6}
    assert before.stranded_hold_counts == {product.pk: 2}
    assert not before.recovery_shortfalls
    assert before.maximum(product.pk) == 0

    with CaptureQueriesContext(connection) as queries:
        inventory_capacity(
            [product.pk], now=timezone.now(), exclude_order=first, recover_stranded=True
        )
    assert len(queries) == 1

    detail = admin.get(_path(first))
    assert detail.status_code == 200, detail.json()
    projected = detail.json()["data"]["source_inventory"]["lines"][0]
    options = {row["farm_id"]: row for row in projected["farms"]}
    key = uuid.uuid4()
    payload = {"idempotency_key": str(key)}
    if legacy_shape:
        payload["allocations"] = [_allocation(own.line, batch_a, 4)]
    else:
        payload["sources"] = [_source(own.line, batch_a)]
    response = admin.post(f"{_path(first)}/approve", payload, format="json")
    assert (
        options[str(farm_a.pk)]["eligible"],
        projected["suggested_farm_id"],
        response.status_code,
    ) == (
        True,
        str(farm_a.pk),
        200,
    )
    assert options[str(farm_b.pk)]["eligible"] is False
    assert_admin_action_receipt(response, first.pk, "approve", key)
    replay = admin.post(f"{_path(first)}/approve", payload, format="json")
    assert_admin_action_receipt(replay, first.pk, "approve", key)
    assert first.actions.filter(action="approve").count() == 1
    assert list(own.line.stock_allocations.values_list("batch_id", "quantity")) == [(batch_a.pk, 4)]
    assert list(
        StoreStockMovement.objects.filter(line=own.line).values_list("kind", "quantity")
    ) == [("sale", 4)]

    other_hold.refresh_from_db()
    assert (other_hold.farm_id, other_hold.quantity, other_hold.released_at) == (farm_a.pk, 2, None)
    after = inventory_capacity([product.pk], now=timezone.now())
    assert after.total_demand == {product.pk: 2}
    assert after.stranded_hold_counts == {product.pk: 1}
    assert not after.recovery_shortfalls
    assert after.maximum(product.pk) == 0
    next_key = uuid.uuid4()
    recovered = admin.post(
        f"{_path(other)}/approve",
        {
            "idempotency_key": str(next_key),
            "sources": [{"line_id": str(other_hold.line_id), "farm_id": str(farm_b.pk)}],
        },
        format="json",
    )
    assert_admin_action_receipt(recovered, other.pk, "approve", next_key)
    assert other_hold.line.stock_allocations.get().batch.farm_product.farm_id == farm_b.pk
