"""Bulk writes against existing Product mutex consumers; run on isolated MySQL."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import connection, connections

from commerce.models import StoreOrder
from commerce.services import transition_product
from commerce.store_orders import StoreOrderError, approve_store_order
from content.creation_receipts import CreationIdempotencyConflict
from content.models import AdminCreationReceipt, ContentAuditLog
from farms import services
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from farms.serializers import BulkStockBatchWriteSerializer
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_bulk_supply import bulk_payload
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_product_lifecycle_locking import run_in_lock_order
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "mysql", reason="Requires isolated MySQL row locks"),
]


def command(sources, actor, key=None):
    serializer = BulkStockBatchWriteSerializer(data=bulk_payload(sources))
    serializer.is_valid(raise_exception=True)
    data = dict(serializer.validated_data, farm_id=sources[0].farm_id, actor=actor)
    if key:
        data["idempotency_key"] = key
    return data


@pytest.mark.parametrize("same_key", [True, False])
@pytest.mark.parametrize("bulk_first", [True, False])
def test_overlapping_bulk_reversed_input(store_product, active_user, same_key, bulk_first):
    source = store_product.farm_offers.get()
    second = FarmProduct.objects.create(farm=source.farm, product=make_product("BULK-SECOND"))
    left = command([source, second], active_user)
    right = dict(left, items=list(reversed(left["items"])))
    if not same_key:
        from identity.models import User

        right["actor"] = User.objects.create_user(
            email="bulk-other@example.com", name="Other", status="active"
        )
        right["idempotency_key"] = uuid4()
    before = FarmProductStockBatch.objects.count()
    results = run_in_lock_order(
        lambda: services.create_stock_batches_bulk(**left),
        lambda: services.create_stock_batches_bulk(**right),
        left_first=bulk_first,
    )
    winner, loser = results if bulk_first else reversed(results)
    assert winner.result()[1] is True
    if same_key:
        assert loser.result()[1] is False
    else:
        with pytest.raises(services.FarmConflict, match="farm_product_version_conflict"):
            loser.result()
    assert FarmProductStockBatch.objects.count() == before + 2
    assert AdminCreationReceipt.objects.count() == 1


@pytest.mark.parametrize("bulk_first", [True, False])
@pytest.mark.parametrize("operation", ["single", "remove", "readd", "product", "farm", "delete"])
def test_bulk_against_supply_and_farm_writers(store_product, active_user, bulk_first, operation):
    source = store_product.farm_offers.get()
    if operation == "readd":
        services.remove_farm_product(
            farm_id=source.farm_id,
            product_id=source.product_id,
            expected_link_id=source.pk,
            expected_version=1,
            actor=active_user,
        )
        source.refresh_from_db()
    bulk = command([source], active_user)
    revision = dict(
        farm_id=source.farm_id,
        product_id=source.product_id,
        expected_link_id=source.pk,
        expected_version=source.version,
        actor=active_user,
    )
    values = {"start_date": bulk["start_date"], "end_date": bulk["end_date"], "quantity_units": 2}
    writers = {
        "single": lambda: services.create_stock_batch(**revision, data=values),
        "remove": lambda: services.remove_farm_product(**revision),
        "readd": lambda: services.readd_farm_product(**revision, data=values),
        "product": lambda: transition_product(
            actor=active_user,
            product_id=source.product_id,
            expected_status="active",
            target_status="discontinued",
        ),
        "farm": lambda: services.update_farm(
            farm_id=source.farm_id,
            actor=active_user,
            expected_version=source.farm.version,
            data={"is_active": False},
        ),
        "delete": lambda: services.delete_farm(farm_id=source.farm_id, actor=active_user),
    }
    bulk_result, other = run_in_lock_order(
        lambda: services.create_stock_batches_bulk(**bulk),
        writers[operation],
        left_first=bulk_first,
    )
    if operation == "delete":
        with pytest.raises(services.FarmConflict, match="farm_in_use"):
            other.result()
        assert bulk_result.result()[1] is True
    elif operation == "farm":
        other.result()
        assert bulk_result.result()[1] is True
        assert Farm.objects.get(pk=source.farm_id).is_active is False
    elif operation == "readd":
        other.result()
        with pytest.raises(
            services.FarmConflict,
            match="farm_product_removed" if bulk_first else "farm_product_version_conflict",
        ):
            bulk_result.result()
    elif bulk_first:
        assert bulk_result.result()[1] is True
        if operation == "product":
            other.result()
        else:
            with pytest.raises(services.FarmConflict, match="farm_product_version_conflict"):
                other.result()
    else:
        other.result()
        with pytest.raises(
            services.FarmConflict,
            match="product_not_selling"
            if operation == "product"
            else "farm_product_version_conflict",
        ):
            bulk_result.result()
    source.refresh_from_db()
    expected_bulk_success = operation in {"farm", "delete"} or (bulk_first and operation != "readd")
    assert AdminCreationReceipt.objects.count() == int(expected_bulk_success)


@pytest.mark.parametrize("bulk_first", [True, False])
@pytest.mark.parametrize("operation", ["checkout", "approval"])
def test_bulk_with_store(store_product, active_user, checkout_operation, bulk_first, operation):
    source = store_product.farm_offers.get()
    bulk = command([source], active_user)
    if operation == "approval":
        order, _, _ = checkout_operation()

        def other():
            return approve_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid4(),
                allocations=[
                    {
                        "line_id": order.lines.get().pk,
                        "batch_id": source.stock_batches.order_by("created_at").first().pk,
                        "quantity": 2,
                    }
                ],
            )
    else:
        other = checkout_operation
    created, consumer = run_in_lock_order(
        lambda: services.create_stock_batches_bulk(**bulk), other, left_first=bulk_first
    )
    assert created.result()[1] is True
    if operation == "checkout" and bulk_first:
        with pytest.raises(StoreOrderError, match="store_requote_required") as error:
            consumer.result()
        assert error.value.context["reason"] == "stock_changed"
        assert error.value.context["available_quantity"] == 20
        assert not StoreOrder.objects.exists()
    else:
        consumer.result()
    assert source.stock_batches.count() == 2
    assert AdminCreationReceipt.objects.count() == 1


@pytest.mark.parametrize("bulk_first", [True, False])
def test_bulk_with_csa_selection(context, bulk_first):
    actor, product, _, _, _ = context
    source = product.farm_offers.get()
    bulk = command([source], actor)
    created, selected = run_in_lock_order(
        lambda: services.create_stock_batches_bulk(**bulk),
        lambda: save_selection(context, quantity=1),
        left_first=bulk_first,
    )
    assert created.result()[1] is True
    selection = selected.result()
    assert selection.stock_holds.get().quantity == 1
    assert AdminCreationReceipt.objects.count() == 1


def test_same_key_disjoint_products_race_at_receipt(active_user):
    farm = make_farm()
    sources = [
        FarmProduct.objects.create(farm=farm, product=make_product(f"DISJOINT-{i}"))
        for i in range(2)
    ]
    key = uuid4()
    commands = [command([source], active_user, key) for source in sources]
    barrier = Barrier(2)
    table = AdminCreationReceipt._meta.db_table.upper()

    def run(data):
        db = connections["default"]

        def coordinate(execute, sql, params, many, context):
            if sql.upper().startswith("INSERT") and table in sql.upper():
                barrier.wait(timeout=15)
            return execute(sql, params, many, context)

        try:
            db.ensure_connection()
            with db.cursor() as cursor:
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 15")
            with db.execute_wrapper(coordinate):
                return services.create_stock_batches_bulk(**data)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run, data) for data in commands]
    successful = 0
    for future in futures:
        try:
            assert future.result()[1] is True
            successful += 1
        except CreationIdempotencyConflict:
            pass
    assert successful == 1
    assert FarmProductStockBatch.objects.count() == 1
    assert AdminCreationReceipt.objects.count() == 1
    assert ContentAuditLog.objects.filter(action="farm.stock_batch.create").count() == 1
