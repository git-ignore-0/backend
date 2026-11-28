"""Forced-order checks for the Product row-lock contract on a transactional database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Event
from uuid import uuid4

import pytest
from django.db import connection, connections
from django.utils import timezone

from commerce.models import (
    CatalogTreeLock,
    Product,
    StoreOrder,
    StoreOrderLine,
    StoreStockReservation,
)
from commerce.serializers import StoreCheckoutSerializer
from commerce.services import CatalogConflict, delete_product, transition_product, update_product
from commerce.store_orders import StoreOrderError, create_store_checkout
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from farms.services import FarmConflict, create_stock_batch, link_farm_products, update_stock_batch
from memberships import csa_rounds
from memberships.models import CSASelection, MembershipCycleQuota
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        not connection.features.has_select_for_update,
        reason="requires actual SELECT FOR UPDATE row locks (SQLite does not provide them)",
    ),
]

LOCK_TIMEOUT = 15


def run_in_lock_order(left, right, *, left_first, lock_model=Product):
    """Pause at the service's real row lock, before either request completes."""

    winner_has_lock = Event()
    winner_ready = Event()
    contender_attempted_lock = Event()
    table = lock_model._meta.db_table

    def run(operation, *, first):
        db = connections["default"]
        try:
            db.ensure_connection()
            if db.vendor == "mysql":
                with db.cursor() as cursor:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout = %s", [LOCK_TIMEOUT])

            table_clause = f"FROM {db.ops.quote_name(table)}".upper()

            def coordinate(execute, sql, params, many, context):
                statement = sql.upper()
                if table_clause not in statement or "FOR UPDATE" not in statement:
                    return execute(sql, params, many, context)
                if not first:
                    contender_attempted_lock.set()
                    return execute(sql, params, many, context)
                result = execute(sql, params, many, context)
                if not winner_has_lock.is_set():
                    winner_has_lock.set()
                    winner_ready.set()
                    assert contender_attempted_lock.wait(LOCK_TIMEOUT), (
                        f"contender never reached SELECT FOR UPDATE on {table}"
                    )
                return result

            with db.execute_wrapper(coordinate):
                return operation()
        finally:
            if first:
                winner_ready.set()
            db.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first, second = (left, right) if left_first else (right, left)
        winner = workers.submit(run, first, first=True)
        assert winner_ready.wait(LOCK_TIMEOUT), f"winner never reached SELECT FOR UPDATE on {table}"
        if not winner_has_lock.is_set():
            winner.result(timeout=LOCK_TIMEOUT)
            pytest.fail(f"winner returned without SELECT FOR UPDATE on {table}")
        contender = workers.submit(run, second, first=False)
    assert contender_attempted_lock.is_set()
    return (winner, contender) if left_first else (contender, winner)


def discontinue(product, actor):
    return transition_product(
        actor=actor,
        product_id=product.pk,
        expected_status="active",
        target_status="discontinued",
    )


@pytest.fixture
def checkout_operation(store_product, payment_settings):
    serializer = StoreCheckoutSerializer(data=guest_payload(store_product))
    serializer.is_valid(raise_exception=True)

    def checkout():
        return create_store_checkout(user=None, payload=serializer.validated_data)

    return checkout


@pytest.fixture(autouse=True)
def isolate_side_effects(settings, tmp_path):
    # Transactional tests flush migration seed rows between cases.
    CatalogTreeLock.objects.get_or_create(pk=1)
    settings.CONTENT_FRONTEND_REVALIDATE_URL = ""
    settings.MEDIA_ROOT = tmp_path


@pytest.mark.parametrize("checkout_first", [True, False], ids=["checkout-first", "status-first"])
def test_discontinue_and_checkout_serialize(
    store_product, active_user, checkout_operation, checkout_first
):
    checkout, status = run_in_lock_order(
        checkout_operation,
        lambda: discontinue(store_product, active_user),
        left_first=checkout_first,
        lock_model=CatalogTreeLock,
    )
    status.result()
    store_product.refresh_from_db()
    assert store_product.status == "discontinued"
    if not checkout_first:
        with pytest.raises(StoreOrderError, match="^store_product_changed$"):
            checkout.result()
        assert not StoreOrder.objects.exists()
        assert not StoreOrderLine.objects.exists()
        assert not StoreStockReservation.objects.exists()
        return

    order, _, created = checkout.result()
    assert created
    replay, _, created = checkout_operation()
    assert not created and replay.pk == order.pk
    assert StoreOrder.objects.count() == 1
    line = order.lines.get(product=store_product)
    reservation = StoreStockReservation.objects.get(line=line, product=store_product)
    assert (line.quantity, reservation.quantity, reservation.released_at) == (2, 2, None)


@pytest.mark.parametrize("inventory_first", [True, False], ids=["stock-first", "status-first"])
@pytest.mark.parametrize("operation", ["add", "create", "extend"])
def test_discontinue_and_inventory_writes_serialize(
    store_product, active_user, operation, inventory_first
):
    offer = store_product.farm_offers.get()
    batch = offer.stock_batches.get()
    today = timezone.localdate()
    new_batch = {"start_date": today, "end_date": today + timedelta(days=2), "quantity_units": 6}
    if operation == "add":
        farm = Farm.objects.create(
            name_vi="Nông trại mới",
            name_en="New farm",
            location_vi="Đà Lạt",
            location_en="Da Lat",
        )

        def write_stock():
            return link_farm_products(
                farm_id=farm.pk,
                product_ids=[store_product.pk],
                actor=active_user,
                idempotency_key=uuid4(),
            )

        error_code = "product_inactive"
    elif operation == "create":

        def write_stock():
            return create_stock_batch(
                farm_id=offer.farm_id,
                product_id=store_product.pk,
                expected_link_id=offer.pk,
                expected_version=offer.version,
                actor=active_user,
                data=new_batch,
            )

        error_code = "product_not_selling"
    else:
        extended_end = batch.end_date + timedelta(days=1)

        def write_stock():
            # A quantity reduction must not conceal the longer availability window.
            return update_stock_batch(
                farm_id=offer.farm_id,
                product_id=store_product.pk,
                batch_id=batch.pk,
                expected_version=batch.version,
                actor=active_user,
                data={"quantity_units": 9, "end_date": extended_end},
            )

        error_code = "product_not_selling"

    stock, status = run_in_lock_order(
        write_stock, lambda: discontinue(store_product, active_user), left_first=inventory_first
    )
    status.result()
    store_product.refresh_from_db()
    assert store_product.status == "discontinued"

    if inventory_first:
        stock.result()
    else:
        with pytest.raises(FarmConflict, match=f"^{error_code}$"):
            stock.result()

    expected_batches = 2 if inventory_first and operation == "create" else 1
    assert FarmProductStockBatch.objects.filter(farm_product__product=store_product).count() == (
        expected_batches
    )
    if operation == "add":
        if inventory_first:
            source = FarmProduct.objects.get(farm=farm, product=store_product)
            assert not source.stock_batches.exists()
        else:
            assert not FarmProduct.objects.filter(farm=farm, product=store_product).exists()
    elif operation == "create" and inventory_first:
        assert offer.stock_batches.exclude(pk=batch.pk).get().quantity_units == 6
    elif operation == "extend":
        original_end = batch.end_date
        batch.refresh_from_db()
        if inventory_first:
            assert (batch.quantity_units, batch.end_date, batch.version) == (9, extended_end, 2)
        else:
            assert (batch.quantity_units, batch.end_date, batch.version) == (10, original_end, 1)


def assert_csa_holds(selection, product, *, cost):
    line = selection.lines.get(product=product)
    assert line.quantity == 2
    assert Decimal(line.snapshot["quota_cost"]) == cost
    assert selection.stock_holds.get(product=product).quantity == 2
    assert selection.quota_holds.get().units == 2 * cost
    assert (
        MembershipCycleQuota.objects.get(
            cycle__membership_id=selection.membership_id
        ).remaining_units
        == 4
    )
    assert selection.current_usage_id is None
    return line.snapshot


@pytest.mark.parametrize("selection_first", [True, False], ids=["hold-first", "status-first"])
def test_discontinue_and_first_csa_save_serialize(context, selection_first):
    user, product, membership, round_, _ = context
    selection, status = run_in_lock_order(
        lambda: save_selection(context),
        lambda: discontinue(product, user),
        left_first=selection_first,
    )
    status.result()
    product.refresh_from_db()
    assert product.status == "discontinued"
    if selection_first:
        committed = selection.result()
        assert_csa_holds(committed, product, cost=Decimal("1"))
        assert save_selection(context).pk == committed.pk
        assert_csa_holds(committed, product, cost=Decimal("1"))
    else:
        with pytest.raises(csa_rounds.CSARoundError, match="^csa_product_unavailable$"):
            selection.result()
        assert not CSASelection.objects.filter(round=round_, membership=membership).exists()
        assert not csa_rounds.reserved_stock([product.pk])


@pytest.mark.parametrize("edit_first", [True, False], ids=["edit-first", "batch-first"])
def test_identity_edit_and_first_stock_batch_serialize(store_product, active_user, edit_first):
    offer = store_product.farm_offers.get()
    offer.stock_batches.all().delete()
    today = timezone.localdate()

    def edit():
        return update_product(
            actor=active_user,
            product=store_product,
            payload={"sku": "TOMATO-2KG", "unit_size": Decimal("2.000")},
        )

    def create():
        return create_stock_batch(
            farm_id=offer.farm_id,
            product_id=store_product.pk,
            expected_link_id=offer.pk,
            expected_version=offer.version,
            actor=active_user,
            data={"start_date": today, "end_date": today, "quantity_units": 5},
        )

    editing, stock = run_in_lock_order(edit, create, left_first=edit_first)
    stock.result()
    if edit_first:
        editing.result()
    else:
        with pytest.raises(CatalogConflict, match="^product_unit_in_use$"):
            editing.result()
    store_product.refresh_from_db()
    expected_identity = (
        ("TOMATO-2KG", Decimal("2.000")) if edit_first else ("TOMATO", Decimal("1.000"))
    )
    assert (store_product.sku, store_product.unit_size) == expected_identity
    assert offer.stock_batches.get().quantity_units == 5


@pytest.mark.parametrize("edit_first", [True, False], ids=["edit-first", "checkout-first"])
def test_identity_edit_and_order_line_use_stock_locked_identity(
    store_product, active_user, checkout_operation, edit_first
):
    # Checkout needs an existing batch. The batch already locks identity in
    # both orders, and the new order line must carry the same identity.
    editing, checkout = run_in_lock_order(
        lambda: update_product(
            actor=active_user,
            product=store_product,
            payload={"sku": "TOMATO-2KG", "unit_size": Decimal("2.000")},
        ),
        checkout_operation,
        left_first=edit_first,
        lock_model=CatalogTreeLock,
    )
    with pytest.raises(CatalogConflict, match="^product_unit_in_use$"):
        editing.result()
    order, _, created = checkout.result()
    assert created
    line = order.lines.get(product=store_product)
    assert (line.sku_snapshot, line.unit_size_snapshot) == ("TOMATO", Decimal("1.000"))
    assert StoreStockReservation.objects.get(line=line, product=store_product).quantity == 2
    assert StoreOrder.objects.count() == 1
    store_product.refresh_from_db()
    assert (store_product.sku, store_product.unit_size) == ("TOMATO", Decimal("1.000"))


@pytest.mark.parametrize("edit_first", [True, False], ids=["edit-first", "hold-first"])
@pytest.mark.parametrize("field", ["identity", "quota"])
def test_product_edit_and_csa_save_serialize(context, field, edit_first):
    user, product, *_ = context
    identity = field == "identity"
    payload = (
        {"sku": "TOMATO-2KG", "unit_size": Decimal("2.000")}
        if identity
        else {"csa_quota_cost": Decimal("2")}
    )
    editing, saving = run_in_lock_order(
        lambda: update_product(actor=user, product=product, payload=payload),
        lambda: save_selection(context),
        left_first=edit_first,
    )
    if identity or not edit_first:
        error = "product_unit_in_use" if identity and edit_first else "product_csa_hold_in_use"
        with pytest.raises(CatalogConflict, match=f"^{error}$"):
            editing.result()
    else:
        editing.result()

    product.refresh_from_db()
    expected_cost = Decimal("1") if identity or not edit_first else Decimal("2")
    assert product.csa_quota_cost == expected_cost
    assert (product.sku, product.unit_size) == ("TOMATO", Decimal("1.000"))
    snapshot = assert_csa_holds(saving.result(), product, cost=expected_cost)
    assert (snapshot["sku"], Decimal(snapshot["unit_size"])) == ("TOMATO", Decimal("1.000"))


@pytest.mark.parametrize("delete_first", [True, False], ids=["delete-first", "reference-first"])
def test_delete_and_creating_farm_reference_serialize(store_product, active_user, delete_first):
    offer = store_product.farm_offers.get()
    farm_id = offer.farm_id
    offer.stock_batches.all().delete()
    offer.delete()
    product_id = store_product.pk

    def add_reference():
        return link_farm_products(
            farm_id=farm_id,
            product_ids=[product_id],
            actor=active_user,
            idempotency_key=uuid4(),
        )

    deleting, adding = run_in_lock_order(
        lambda: delete_product(actor=active_user, product=store_product),
        add_reference,
        left_first=delete_first,
    )
    if delete_first:
        deleting.result()
        with pytest.raises(FarmConflict, match="^not_found$"):
            adding.result()
        assert not Product.objects.filter(pk=product_id).exists()
        assert not FarmProduct.objects.filter(farm_id=farm_id, product_id=product_id).exists()
        assert not FarmProductStockBatch.objects.filter(farm_product__farm_id=farm_id).exists()
    else:
        receipt, created = adding.result()
        assert created is True
        assert receipt.resource_id == farm_id
        assert receipt.created_count == 1
        with pytest.raises(CatalogConflict, match="^product_in_use$"):
            deleting.result()
        assert Product.objects.filter(pk=product_id).exists()
        assert not FarmProduct.objects.get(
            farm_id=farm_id, product_id=product_id
        ).stock_batches.exists()
