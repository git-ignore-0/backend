"""CSA holds share Store inventory while keeping one farm per product."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import Product, ProductTranslation, StoreOrder, StoreStockReservation
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships import csa_rounds as service
from memberships.models import CSASelection, MembershipCycleQuota
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def _batch(product, now, *, units, farm=None, days=1):
    farm = farm or Farm.objects.create(name_vi="Trang trại B", name_en="Farm B")
    source, _ = FarmProduct.objects.get_or_create(farm=farm, product=product)
    today = timezone.localdate(now)
    return FarmProductStockBatch.objects.create(
        farm_product=source,
        start_date=today,
        end_date=today + timedelta(days=days),
        quantity_units=units,
    )


def _initial_batch(product, now, *, units):
    batch = FarmProductStockBatch.objects.get(farm_product__product=product)
    batch.quantity_units = units
    batch.end_date = timezone.localdate(now)
    batch.save(update_fields=["quantity_units", "end_date"])
    return batch


def test_catalog_and_save_require_one_farm_with_the_entire_quantity(context):
    user, product, _, round_, now = context
    _initial_batch(product, now, units=3)
    _batch(product, now, units=3)

    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    response = client.get(f"/api/v1/csa-rounds/{round_.pk}/products")
    assert response.status_code == 200, response.data
    assert response.data["data"][0]["available_quantity"] == 3

    with pytest.raises(service.CSARoundError, match="^csa_stock_insufficient$"):
        save(context, quantity=4)
    assert not CSASelection.objects.exists()


def test_assignment_prefers_existing_farm_switches_and_survives_quota_failure(context):
    _, product, _, _, now = context
    first = _initial_batch(product, now, units=1)
    _batch(product, now, units=2, farm=first.farm_product.farm, days=0)
    second = _batch(product, now, units=5)

    selection = save(context, quantity=2)
    assert selection.stock_holds.get().farm_id == first.farm_product.farm_id
    kept = save(context, quantity=3, expected_revision=selection.revision)
    assert kept.stock_holds.get().farm_id == first.farm_product.farm_id

    with pytest.raises(service.CSARoundError, match="^csa_quota_insufficient$"):
        save(context, quantity=5, expected_revision=kept.revision)
    kept.refresh_from_db()
    assert kept.revision == 2
    assert kept.stock_holds.get().farm_id == first.farm_product.farm_id
    assert kept.stock_holds.get().quantity == 3
    assert kept.quota_holds.get().units == 3

    switched = save(context, quantity=4, expected_revision=kept.revision)
    assert switched.stock_holds.get().farm_id == second.farm_product.farm_id
    assert switched.stock_holds.get().quantity == 4
    reduced = save(context, quantity=2, expected_revision=switched.revision)
    assert reduced.stock_holds.get().farm_id == second.farm_product.farm_id


def test_protected_hold_can_shrink_after_natural_expiry_without_switching(context, monkeypatch):
    user, product, member, round_, now = context
    first = _initial_batch(product, now, units=5)
    _batch(product, now, units=2)
    round_.closes_at = now + timedelta(days=2)
    round_.delivery_date = timezone.localdate(now) + timedelta(days=3)
    round_.save(update_fields=["closes_at", "delivery_date"])
    MembershipCycleQuota.objects.filter(cycle__membership=member).update(
        granted_units=10, remaining_units=10
    )
    selection = save(context, quantity=4)
    now += timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: now)

    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    response = client.get(f"/api/v1/csa-rounds/{round_.pk}/products")
    assert response.status_code == 200, response.data
    assert response.data["data"][0]["available_quantity"] == 4
    assert response.data["data"][0]["selection_mode"] == "held_only"

    with pytest.raises(service.CSARoundError, match="^csa_stock_insufficient$"):
        save(context, quantity=5, expected_revision=selection.revision)
    kept = save(context, quantity=4, expected_revision=selection.revision)
    reduced = save(context, quantity=3, expected_revision=kept.revision)
    assert reduced.stock_holds.get().farm_id == first.farm_product.farm_id
    assert reduced.stock_holds.get().quantity == 3
    assert reduced.quota_holds.get().units == 3


def test_order_creation_and_reselection_preserve_exactly_one_assignment_per_product(
    context, monkeypatch
):
    from django.db.models import QuerySet

    user, product, _, round_, now = context
    first = _initial_batch(product, now, units=4)
    other = Product.objects.create(
        category=product.category,
        sku="CUCUMBER",
        slug="cucumber",
        csa_quota_group=product.csa_quota_group,
        csa_quota_cost=1,
        status="active",
        is_published=True,
        retail_price_vnd=45000,
        unit_size=1,
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    ProductTranslation.objects.create(product=other, locale="vi", name="Dưa leo")
    second = _batch(other, now, units=4)
    quantities = [
        {"product_id": product.pk, "quantity": 2},
        {"product_id": other.pk, "quantity": 2},
    ]
    assignments = {product.pk: first.farm_product.farm_id, other.pk: second.farm_product.farm_id}
    quantities.sort(key=lambda row: assignments[row["product_id"]], reverse=True)
    locked_farms = []
    original_fetch = QuerySet._fetch_all

    def capture_locks(queryset):
        capture = (
            queryset.model is Farm
            and queryset.query.select_for_update
            and queryset._result_cache is None
        )
        original_fetch(queryset)
        if capture:
            locked_farms.extend(farm.pk for farm in queryset._result_cache)

    monkeypatch.setattr(QuerySet, "_fetch_all", capture_locks)
    selected = service.save_selection(round_id=round_.pk, user=user, lines=quantities)
    assert locked_farms == sorted(assignments.values())
    assert dict(selected.stock_holds.values_list("product_id", "farm_id")) == assignments

    service.process_round(round_.pk, now=round_.closes_at)
    selected.refresh_from_db()
    assert selected.order.lines.count() == 2
    assert not StoreStockReservation.objects.filter(line__order=selected.order).exists()
    assert service.selection_stock_capacity(assignments, None) == {product.pk: 2, other.pk: 2}
    previous_lines = set(selected.order.lines.values_list("id", flat=True))

    cutoff = service.delivery_start(round_) - timedelta(hours=1)
    service.reopen_selection(
        selection_id=selected.pk, actor=user, deadline=cutoff, now=round_.closes_at
    )
    assert service.selection_stock_capacity(assignments, None) == {product.pk: 4, other.pk: 4}
    draft = service.save_selection(
        round_id=round_.pk, user=user, lines=quantities, now=round_.closes_at
    )
    confirmed = service.confirm_selection(
        selection_id=draft.pk, user=user, expected_revision=draft.revision, now=round_.closes_at
    )
    assert confirmed.order_id == selected.order_id
    assert not previous_lines.intersection(confirmed.order.lines.values_list("id", flat=True))
    assert dict(confirmed.stock_holds.values_list("product_id", "farm_id")) == assignments
    assert service.selection_stock_capacity(assignments, None) == {product.pk: 2, other.pk: 2}
    assert not StoreStockReservation.objects.filter(line__order=confirmed.order).exists()


@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires MySQL row-lock semantics")
@pytest.mark.django_db(transaction=True)
def test_checkout_and_csa_save_serialize_single_farm_demand(context, payment_settings):
    _, product, _, _, now = context
    source = _initial_batch(product, now, units=4).farm_product
    _batch(product, now, units=2)
    barrier = Barrier(2)

    def choose():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            save(context, quantity=3)
            return "csa_saved"
        except service.CSARoundError as error:
            return error.code
        finally:
            close_old_connections()

    def checkout():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            payload = guest_payload(product, quantity=3, expected_available=4)
            response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
            if response.status_code == 201:
                return "store_created"
            assert response.status_code == 409, response.data
            return response.data["errors"][0]["code"]
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        chosen = pool.submit(choose)
        checked_out = pool.submit(checkout)
        assert (chosen.result(timeout=15), checked_out.result(timeout=15)) in {
            ("csa_saved", "store_requote_required"),
            ("csa_stock_insufficient", "store_created"),
        }

    assert CSASelection.objects.count() + StoreOrder.objects.count() == 1
    assert service.selection_stock_capacity([product.pk], None) == {product.pk: 2}
    assigned = list(StoreStockReservation.objects.values_list("farm_id", "quantity"))
    assigned += list(service.effective_stock_hold_rows(now=now).values_list("farm_id", "quantity"))
    assert assigned == [(source.farm_id, 3)]


def test_save_rechecks_the_deadline_after_waiting_for_assignment_farm(context, monkeypatch):
    from django.db.models import QuerySet

    user, product, _, round_, now = context
    clock = [now]
    monkeypatch.setattr(timezone, "now", lambda: clock[0])
    original_fetch = QuerySet._fetch_all

    def move_clock_at_farm_lock(queryset):
        original_fetch(queryset)
        if queryset.model is Farm and queryset.query.select_for_update:
            clock[0] = round_.closes_at

    monkeypatch.setattr(QuerySet, "_fetch_all", move_clock_at_farm_lock)
    with pytest.raises(service.CSARoundError, match="^csa_selection_locked$"):
        service.save_selection(
            round_id=round_.pk, user=user, lines=[{"product_id": product.pk, "quantity": 1}]
        )
    assert not CSASelection.objects.exists()
