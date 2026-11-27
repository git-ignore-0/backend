"""Single-Farm inventory races at real database locks."""

import uuid
from datetime import timedelta
from functools import partial

import pytest
from django.db import connection
from django.utils import timezone

from commerce.models import CatalogTreeLock, StoreOrder, StoreOrderStatus, StoreStockMovement
from commerce.serializers import StoreCheckoutSerializer
from commerce.store_orders import (
    StoreOrderError,
    approve_store_order,
    create_store_checkout,
    restock_store_order,
)
from farms import services
from memberships import csa_rounds
from memberships.models import CSASelection, CSAStockHold
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import overlapping_reservation_case as overlapping_reservation_case
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_lifecycle_locking import allocated_selection as allocated_selection
from tests.test_farm_lifecycle_locking import cancel_native_order
from tests.test_farm_source_lifecycle_mysql import source_command
from tests.test_farmer_supply_requests import (
    decide,
    farmer_client,
    link_url,
    submit,
    supply_payload,
)
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_product_lifecycle_locking import run_in_lock_order
from tests.test_single_farm_lifecycle import add_backup_farm, batch_command
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "mysql" or not connection.features.has_select_for_update,
        reason="requires MySQL with SELECT FOR UPDATE",
    ),
]


def checkout(product, *, quantity, expected):
    payload = guest_payload(product, quantity=quantity, expected_available=expected)
    payload["delivery_date"] = timezone.localdate() + timedelta(days=1)
    serializer = StoreCheckoutSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    order, _, created = create_store_checkout(user=None, payload=serializer.validated_data)
    assert created
    return order


def approve(order, actor, farm, key=None):
    return approve_store_order(
        order_id=order.pk,
        actor=actor,
        idempotency_key=key or uuid.uuid4(),
        sources=[{"line_id": order.lines.get().pk, "farm_id": farm.pk}],
    )


@pytest.mark.parametrize("has_backup", [False, True], ids=["one-farm", "two-farms"])
def test_checkouts_serialize_and_cannot_combine_leftover_stock(
    store_product, payment_settings, has_backup
):
    source = store_product.farm_offers.get()
    source.stock_batches.update(quantity_units=6)
    farm_ids = {source.farm_id}
    if has_backup:
        farm_ids.add(add_backup_farm(store_product, 6).pk)
    operation = partial(checkout, store_product, quantity=4, expected=6)

    first, second = run_in_lock_order(
        operation, operation, left_first=True, lock_model=CatalogTreeLock
    )
    reservation = first.result().lines.get().stock_reservations.get()
    assert (reservation.quantity, reservation.released_at) == (4, None)
    if has_backup:
        second_reservation = second.result().lines.get().stock_reservations.get()
        assert (second_reservation.quantity, second_reservation.released_at) == (4, None)
        assert {reservation.farm_id, second_reservation.farm_id} == farm_ids
        with pytest.raises(StoreOrderError, match="^store_stock_changed$"):
            checkout(store_product, quantity=4, expected=2)
        assert StoreOrder.objects.count() == 2
    else:
        with pytest.raises(StoreOrderError, match="^store_requote_required$") as exc:
            second.result()
        assert exc.value.context["available_quantity"] == 2
        assert reservation.farm_id == source.farm_id
        assert StoreOrder.objects.count() == 1
    assert not StoreStockMovement.objects.exists()


@pytest.mark.parametrize("checkout_first", [True, False], ids=["checkout-first", "selection-first"])
def test_checkout_and_csa_save_compete_for_one_farm(context, payment_settings, checkout_first):
    _, product, *_ = context
    source = product.farm_offers.get()
    source.stock_batches.update(quantity_units=6)

    guest, membership = run_in_lock_order(
        partial(checkout, product, quantity=4, expected=6),
        partial(save_selection, context, quantity=4),
        left_first=checkout_first,
    )
    if checkout_first:
        order = guest.result()
        assert order.lines.get().stock_reservations.get().farm_id == source.farm_id
        with pytest.raises(csa_rounds.CSARoundError, match="^csa_stock_insufficient$"):
            membership.result()
        assert not CSASelection.objects.exists()
        assert not CSAStockHold.objects.exists()
    else:
        selection = membership.result()
        assert selection.stock_holds.get().farm_id == source.farm_id
        with pytest.raises(StoreOrderError, match="^store_requote_required$") as exc:
            guest.result()
        assert exc.value.context["available_quantity"] == 2
        assert not StoreOrder.objects.exists()


def test_two_csa_selections_serialize_stock_and_quota(context, overlapping_reservation_case):
    actor, product, _, _, now = context
    first, second, other, error_code = overlapping_reservation_case
    line = [{"product_id": product.pk, "quantity": 3}]
    result, rejected = run_in_lock_order(
        partial(csa_rounds.save_selection, round_id=first.pk, user=actor, lines=line, now=now),
        partial(csa_rounds.save_selection, round_id=second.pk, user=other, lines=line, now=now),
        left_first=True,
    )

    selection = result.result()
    assert selection.stock_holds.get().farm_id == product.farm_offers.get().farm_id
    assert selection.quota_holds.get().units == 3
    with pytest.raises(csa_rounds.CSARoundError, match=f"^{error_code}$"):
        rejected.result()
    assert CSASelection.objects.count() == 1


@pytest.mark.parametrize("checkout_first", [True, False], ids=["checkout-first", "approval-first"])
def test_checkout_and_farm_switch_serialize_without_stealing_a_hold(
    store_product, active_user, payment_settings, checkout_first
):
    original = store_product.farm_offers.get().farm
    order = checkout(store_product, quantity=6, expected=10)
    target = add_backup_farm(store_product, 8)
    guest, approval = run_in_lock_order(
        partial(checkout, store_product, quantity=5, expected=8),
        partial(approve, order, active_user, target),
        left_first=checkout_first,
    )

    order.refresh_from_db()
    reservation = order.lines.get().stock_reservations.get()
    if checkout_first:
        assert guest.result().lines.get().stock_reservations.get().farm_id == target.pk
        with pytest.raises(StoreOrderError, match="^store_allocation_stale$") as exc:
            approval.result()
        assert exc.value.context["available_quantity"] == 3
        assert order.status == StoreOrderStatus.PENDING_REVIEW
        assert (reservation.farm_id, reservation.released_at) == (original.pk, None)
        assert not StoreStockMovement.objects.exists()
        assert not order.actions.filter(action="approve").exists()
    else:
        assert approval.result().pk == order.pk
        with pytest.raises(StoreOrderError, match="^store_requote_required$") as exc:
            guest.result()
        assert exc.value.context["available_quantity"] == 10
        assert reservation.released_at is not None
        allocation = order.lines.get().stock_allocations.get()
        assert (allocation.batch.farm_product.farm_id, allocation.quantity) == (target.pk, 6)
        assert order.actions.filter(action="approve").count() == 1


@pytest.mark.parametrize("same_key", [True, False], ids=["same-key", "different-key"])
def test_two_approvals_create_one_receipt_and_one_farm_allocation(
    store_product, active_user, payment_settings, same_key
):
    order = checkout(store_product, quantity=4, expected=10)
    farm = store_product.farm_offers.get().farm
    key = uuid.uuid4()
    first, second = run_in_lock_order(
        partial(approve, order, active_user, farm, key),
        partial(approve, order, active_user, farm, key if same_key else uuid.uuid4()),
        left_first=True,
        lock_model=StoreOrder,
    )

    assert first.result().pk == order.pk
    if same_key:
        assert second.result().pk == order.pk
    else:
        with pytest.raises(StoreOrderError, match="^store_order_state_conflict$"):
            second.result()
    assert order.actions.filter(action="approve").count() == 1
    allocation = order.lines.get().stock_allocations.get()
    assert (allocation.quantity, allocation.batch.farm_product.farm_id) == (4, farm.pk)
    assert list(allocation.movements.values_list("kind", "quantity")) == [("sale", 4)]


@pytest.mark.parametrize(
    "lifecycle_first", [True, False], ids=["lifecycle-first", "approval-first"]
)
@pytest.mark.parametrize("operation", ["deactivate", "remove", "reduce", "cancel"])
def test_approval_switch_and_target_source_lifecycle_are_atomic(
    store_product, active_user, payment_settings, lifecycle_first, operation
):
    original = store_product.farm_offers.get().farm
    order = checkout(store_product, quantity=4, expected=10)
    target = add_backup_farm(store_product, 4)
    source = target.products.get()
    batch = source.stock_batches.get()

    def change_supply():
        if operation == "deactivate":
            return services.update_farm(
                farm_id=target.pk, actor=active_user, expected_version=1, data={"is_active": False}
            )
        if operation == "remove":
            return services.remove_farm_product(**source_command(source, active_user))
        if operation == "cancel":
            return services.cancel_stock_batch(**batch_command(source, batch, active_user))
        return services.update_stock_batch(
            **batch_command(source, batch, active_user), data={"quantity_units": 3}
        )

    change, approval = run_in_lock_order(
        change_supply, partial(approve, order, active_user, target), left_first=lifecycle_first
    )

    reservation = order.lines.get().stock_reservations.get()
    if lifecycle_first:
        change.result()
        with pytest.raises(StoreOrderError, match="^store_allocation_stale$"):
            approval.result()
        assert (reservation.farm_id, reservation.released_at) == (original.pk, None)
        assert not StoreStockMovement.objects.exists()
        assert not order.actions.filter(action="approve").exists()
    else:
        assert approval.result().pk == order.pk
        conflict = {"remove": "farm_product_has_open_allocations", "reduce": "stock_sold"}.get(
            operation
        )
        if conflict:
            with pytest.raises(services.FarmConflict, match=f"^{conflict}$"):
                change.result()
        else:
            change.result()
        assert reservation.released_at is not None
        allocation = order.lines.get().stock_allocations.get()
        assert (allocation.batch_id, allocation.quantity) == (batch.pk, 4)
        assert list(allocation.movements.values_list("kind", "quantity")) == [("sale", 4)]
        assert order.actions.filter(action="approve").count() == 1


@pytest.mark.parametrize("restock_first", [True, False], ids=["restock-first", "checkout-first"])
def test_checkout_and_historical_restock_share_farm_capacity(
    context, allocated_selection, restock_first
):
    actor, product, *_ = context
    cancelled = cancel_native_order(allocated_selection, actor)
    allocation = cancelled.lines.get().stock_allocations.get()
    farm = allocation.batch.farm_product.farm
    baseline = allocation.batch.quantity_units - allocation.quantity
    guest, restock = run_in_lock_order(
        partial(checkout, product, quantity=1, expected=baseline),
        partial(
            restock_store_order,
            order_id=cancelled.pk,
            actor=actor,
            idempotency_key=uuid.uuid4(),
            allocations=[{"allocation_id": allocation.pk, "quantity": 1}],
        ),
        left_first=not restock_first,
    )

    assert restock.result().pk == cancelled.pk
    assert allocation.movements.get(kind="restock").quantity == 1
    if restock_first:
        with pytest.raises(StoreOrderError, match="^store_requote_required$") as caught:
            guest.result()
        assert caught.value.context["available_quantity"] == baseline + 1
        assert StoreOrder.objects.count() == 1
    else:
        reservation = guest.result().lines.get().stock_reservations.get()
        assert (reservation.farm_id, reservation.quantity) == (farm.pk, 1)


@pytest.mark.parametrize("reopen_first", [True, False], ids=["reopen-first", "approve-first"])
def test_approval_and_csa_reopen_preserve_single_farm_ownership(context, reopen_first):
    actor, product, _, round_, _ = context
    selection = save_selection(context, quantity=2)
    csa_rounds.process_round(round_.pk, now=round_.closes_at)
    selection.refresh_from_db()
    farm = product.farm_offers.get().farm
    decision_at = round_.closes_at
    reopening, approval = run_in_lock_order(
        partial(
            csa_rounds.reopen_selection,
            selection_id=selection.pk,
            actor=actor,
            deadline=decision_at + timedelta(minutes=30),
            now=decision_at,
            expected_revision=selection.revision,
        ),
        partial(approve, selection.order, actor, farm),
        left_first=reopen_first,
        lock_model=StoreOrder,
    )

    selection.refresh_from_db()
    if reopen_first:
        assert reopening.result().state == "reselecting"
        with pytest.raises(StoreOrderError, match="^store_order_state_conflict$"):
            approval.result()
        assert not StoreStockMovement.objects.exists()
        assert not selection.stock_holds.exists()
    else:
        assert approval.result().pk == selection.order_id
        with pytest.raises(csa_rounds.CSARoundError, match="^csa_selection_locked$"):
            reopening.result()
        allocation = selection.order.lines.get().stock_allocations.get()
        assert (allocation.batch.farm_product.farm_id, allocation.quantity) == (farm.pk, 2)
        assert allocation.movements.filter(kind="sale").count() == 1
        assert not selection.stock_holds.exists()


@pytest.mark.parametrize("supply_first", [True, False], ids=["supply-first", "checkout-first"])
def test_farmer_approval_and_checkout_share_the_farm_capacity(
    farm_admin_api, store_product, payment_settings, supply_first
):
    source = store_product.farm_offers.get()
    link = farm_admin_api.post(link_url(source), {}, format="json")
    assert link.status_code == 201
    submitted = submit(farmer_client(link), supply_payload([source]))
    assert submitted.status_code == 201
    entry_id = submitted.json()["data"]["id"]

    approval, guest = run_in_lock_order(
        partial(decide, farm_admin_api, entry_id),
        partial(checkout, store_product, quantity=2, expected=10),
        left_first=supply_first,
    )

    assert approval.result().status_code == 200
    assert source.stock_batches.count() == 2
    if supply_first:
        with pytest.raises(StoreOrderError, match="^store_requote_required$") as caught:
            guest.result()
        assert caught.value.context["available_quantity"] == 20
        assert not StoreOrder.objects.exists()
    else:
        reserve = guest.result().lines.get().stock_reservations.get()
        assert (reserve.farm_id, reserve.quantity) == (source.farm_id, 2)
