"""Exercise Farm linking against competing writers through real MySQL row locks."""

from functools import partial
from uuid import uuid4

import pytest
from django.db import connection

from commerce.models import Product, StoreStockMovement, StoreStockReservation
from commerce.store_orders import active_product_stock_totals
from content.creation_receipts import CreationIdempotencyConflict
from content.models import AdminCreationReceipt, ContentAuditLog
from farms import services, supply_requests
from farms.farmer_links import write_farmer_link
from farms.models import Farm, FarmProduct, FarmProductStockBatch, FarmSupplyRequest
from memberships.models import CSAStockHold
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_bulk_supply_mysql import command as supply_command
from tests.test_farm_bulk_supply_mysql import run_at_receipt_insert
from tests.test_farm_source_lifecycle_mysql import (
    assert_source_state,
    batch_data,
    source_command,
    source_events,
)
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_product_lifecycle_locking import discontinue, run_in_lock_order
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "mysql", reason="Requires isolated MySQL row locks"),
]


def link_operation(farm, actor, product_ids, *, key=None, removed=None):
    if removed is None:
        payload = {"product_ids": product_ids}
    else:
        payload = {
            "items": [
                {
                    "product_id": product_id,
                    "expected_source": (
                        {"id": removed.pk, "version": removed.version}
                        if removed.product_id == product_id
                        else None
                    ),
                }
                for product_id in product_ids
            ]
        }
    return partial(
        services.link_farm_products,
        farm_id=farm.pk,
        actor=actor,
        idempotency_key=key or uuid4(),
        **payload,
    )


def assert_links(farm, product_ids, *, restored=None):
    sources = FarmProduct.objects.filter(farm=farm)
    restored_id = restored.product_id if restored is not None else None
    assert set(sources.values_list("product_id", "version", "supply_generation")) == {
        (product_id, 3, 2) if product_id == restored_id else (product_id, 1, 1)
        for product_id in product_ids
    }
    assert sources.count() == len(product_ids)
    assert not FarmProductStockBatch.objects.filter(farm_product__farm=farm).exists()
    link_events = ContentAuditLog.objects.filter(
        action="farm.product.add", context__farm_id=str(farm.pk)
    )
    assert sorted(link_events.values_list("entity_id", flat=True)) == sorted(
        str(source_id)
        for source_id in sources.exclude(product_id=restored_id).values_list("pk", flat=True)
    )
    if restored is not None:
        assert_source_state(restored, removed=False, version=3, generation=2)
        assert source_events(restored, "readd") == 1


def removed_source(farm, product_id, actor):
    source = FarmProduct.objects.create(farm=farm, product_id=product_id)
    return services.remove_farm_product(**source_command(source, actor))


@pytest.mark.parametrize("left_first", [True, False], ids=["left-first", "right-first"])
@pytest.mark.parametrize("revision_aware", [False, True], ids=["legacy", "mixed"])
@pytest.mark.parametrize("race", ["replay", "overlap", "same-product"])
def test_concurrent_link_intents_commit_only_one_complete_set(
    active_user, left_first, revision_aware, race
):
    farm = make_farm()
    shared, first, second = [make_product(f"LINK-{i}").pk for i in range(3)]
    key = uuid4()
    removed = removed_source(farm, shared, active_user) if revision_aware else None
    if race == "same-product":
        left_ids = right_ids = [shared]
    else:
        left_ids = [first, shared]
        right_ids = [shared, first] if race == "replay" else [second, shared]
    left = link_operation(farm, active_user, left_ids, key=key, removed=removed)
    right = link_operation(
        farm, active_user, right_ids, key=key if race == "replay" else uuid4(), removed=removed
    )

    results = run_in_lock_order(left, right, left_first=left_first)

    winning, losing = results if left_first else reversed(results)
    receipt, created = winning.result()
    winner_ids = left_ids if left_first else right_ids
    assert created is True
    assert (receipt.resource_id, receipt.created_count) == (farm.pk, len(winner_ids))
    if race == "replay":
        replay, created = losing.result()
        assert created is False
        assert replay.pk == receipt.pk
    else:
        code = "farm_product_version_conflict" if revision_aware else "product_already_linked"
        with pytest.raises(services.FarmConflict, match=f"^{code}$") as error:
            losing.result()
        assert error.value.context["product_id"] == str(shared)
    assert_links(farm, winner_ids, restored=removed)
    assert AdminCreationReceipt.objects.get().pk == receipt.pk


def test_same_key_disjoint_links_compete_at_receipt_insert(active_user):
    farm = make_farm()
    products = [make_product(f"DISJOINT-LINK-{i}") for i in range(2)]
    key = uuid4()
    left, right = [link_operation(farm, active_user, [product.pk], key=key) for product in products]

    results = run_at_receipt_insert(left, right)

    committed = []
    for product, result in zip(products, results, strict=True):
        try:
            assert result.result()[1] is True
        except CreationIdempotencyConflict:
            continue
        committed.append(product.pk)
    assert len(committed) == 1
    assert_links(farm, committed)
    receipt = AdminCreationReceipt.objects.get()
    assert (receipt.resource_id, receipt.created_count) == (farm.pk, 1)


@pytest.mark.parametrize("link_first", [True, False], ids=["link-first", "farm-first"])
@pytest.mark.parametrize("revision_aware", [False, True], ids=["legacy", "mixed"])
@pytest.mark.parametrize("operation", ["delete", "deactivate"])
def test_bulk_linking_serializes_with_farm_writers(
    active_user, link_first, revision_aware, operation
):
    farm = make_farm()
    product_ids = [make_product(f"FARM-LINK-{i}").pk for i in range(2)]
    removed = removed_source(farm, product_ids[1], active_user) if revision_aware else None
    if operation == "delete":
        farm_write = partial(services.delete_farm, farm_id=farm.pk, actor=active_user)
    else:
        farm_write = partial(
            services.update_farm,
            farm_id=farm.pk,
            actor=active_user,
            expected_version=farm.version,
            data={"is_active": False},
        )

    linking, writing = run_in_lock_order(
        link_operation(farm, active_user, product_ids, removed=removed),
        farm_write,
        left_first=link_first,
        lock_model=Product if revision_aware else Farm,
    )

    if link_first:
        assert linking.result()[1] is True
        with pytest.raises(services.FarmConflict, match="^farm_product_changed$"):
            writing.result()
    elif operation == "delete":
        writing.result()
        with pytest.raises(Farm.DoesNotExist):
            linking.result()
        assert not Farm.objects.filter(pk=farm.pk).exists()
        assert_links(farm, [])
        assert not AdminCreationReceipt.objects.exists()
        return
    else:
        assert writing.result().is_active is False
        assert linking.result()[1] is True
    farm.refresh_from_db()
    assert farm.is_active is link_first
    assert farm.version == (1 if link_first else 2)
    assert_links(farm, product_ids, restored=removed)
    assert AdminCreationReceipt.objects.get().created_count == 2


@pytest.mark.parametrize("link_first", [True, False], ids=["link-first", "status-first"])
@pytest.mark.parametrize("revision_aware", [False, True], ids=["legacy", "mixed"])
def test_bulk_linking_rechecks_product_status_and_rolls_back(
    active_user, link_first, revision_aware
):
    farm = make_farm()
    eligible = make_product("ELIGIBLE")
    changed = make_product("DISCONTINUING")
    product_ids = [eligible.pk, changed.pk]
    removed = removed_source(farm, changed.pk, active_user) if revision_aware else None

    linking, discontinuing = run_in_lock_order(
        link_operation(farm, active_user, product_ids, removed=removed),
        partial(discontinue, changed, active_user),
        left_first=link_first,
        lock_model=Product,
    )

    discontinuing.result()
    changed.refresh_from_db()
    assert changed.status == "discontinued"
    if link_first:
        assert linking.result()[1] is True
        assert_links(farm, product_ids, restored=removed)
        assert AdminCreationReceipt.objects.get().created_count == 2
    else:
        with pytest.raises(services.FarmConflict, match="^product_inactive$") as error:
            linking.result()
        context = {"item_index": 1, "product_id": str(changed.pk)}
        if removed is not None:
            assert_source_state(removed, removed=True, version=2)
            assert source_events(removed, "readd") == 0
            context.update(source_id=str(removed.pk), version=2)
        assert error.value.context == context
        assert not FarmProduct.objects.filter(farm=farm, product=eligible).exists()
        assert not AdminCreationReceipt.objects.exists()


@pytest.mark.parametrize("link_first", [True, False], ids=["link-first", "supply-first"])
def test_bulk_linking_and_bulk_supply_only_add_stock_through_supply(
    store_product, active_user, link_first
):
    existing = store_product.farm_offers.get()
    linked_farm = make_farm()
    other = make_product("OTHER")
    product_ids = [other.pk, store_product.pk]

    linking, supplying = run_in_lock_order(
        link_operation(linked_farm, active_user, product_ids),
        partial(services.create_stock_batches_bulk, **supply_command([existing], active_user)),
        left_first=link_first,
    )

    assert linking.result()[1] is True
    assert supplying.result()[1] is True
    assert_links(linked_farm, product_ids)
    assert_source_state(existing, removed=False, version=2)
    assert existing.stock_batches.count() == FarmProductStockBatch.objects.count() == 2
    assert active_product_stock_totals(product_ids) == {store_product.pk: 20}
    assert ContentAuditLog.objects.filter(action="farm.stock_batch.create").count() == 1
    assert set(AdminCreationReceipt.objects.values_list("action", "created_count")) == {
        (AdminCreationReceipt.Action.FARM_PRODUCT_LINKS, 2),
        (AdminCreationReceipt.Action.FARM_STOCK_BATCHES, 1),
    }


def submit_for_empty_source(source, actor):
    link, _ = write_farmer_link(farm_id=source.farm_id, authorize_write=lambda: actor)
    values = batch_data(7)
    entry, _ = supply_requests.submit_supply_request(
        farm_id=source.farm_id,
        digest=link.token_digest,
        idempotency_key=uuid4(),
        start_date=values["start_date"],
        end_date=values["end_date"],
        items=[
            {
                "product_id": source.product_id,
                "expected_link_id": source.pk,
                "expected_generation": source.supply_generation,
                "expected_product_identity": supply_requests.product_identity_fingerprint(
                    source.product
                ),
                "quantity_units": values["quantity_units"],
            }
        ],
    )
    return entry


def test_concurrent_readd_with_the_same_key_replays_once(active_user):
    source = FarmProduct.objects.create(farm=make_farm(), product=make_product())
    source = services.remove_farm_product(**source_command(source, active_user))
    readd = partial(
        services.readd_farm_product, **source_command(source, active_user), idempotency_key=uuid4()
    )

    restoring, replaying = run_in_lock_order(readd, readd, left_first=True)

    receipt, created = restoring.result()
    replay, duplicate_created = replaying.result()
    assert (created, duplicate_created) == (True, False)
    assert receipt.pk == replay.pk == AdminCreationReceipt.objects.get().pk
    assert receipt.resource_id == source.pk
    assert_source_state(source, removed=False, version=3, generation=2)
    assert source_events(source, "readd") == 1
    assert not source.stock_batches.exists()


@pytest.mark.parametrize("readd_first", [True, False], ids=["readd-first", "other-first"])
@pytest.mark.parametrize("revision_aware", [False, True], ids=["single", "mixed"])
@pytest.mark.parametrize("operation", ["remove", "farmer-approval", "bulk-supply"])
def test_readd_serializes_with_stale_source_operations(
    active_user, readd_first, revision_aware, operation
):
    source = FarmProduct.objects.create(farm=make_farm(), product=make_product())
    if operation == "farmer-approval":
        entry = submit_for_empty_source(source, active_user)
    source = services.remove_farm_product(**source_command(source, active_user))
    revision = source_command(source, active_user)
    if operation == "remove":
        other = partial(services.remove_farm_product, **revision)
    elif operation == "farmer-approval":
        other = partial(
            supply_requests.decide_supply_request,
            entry=entry,
            authorize_write=lambda: active_user,
            idempotency_key=uuid4(),
            outcome=FarmSupplyRequest.Status.APPROVED,
        )
    else:
        other = partial(services.create_stock_batches_bulk, **supply_command([source], active_user))

    restoring_operation = partial(services.readd_farm_product, **revision, idempotency_key=uuid4())
    if revision_aware:
        new = make_product("NEW")
        restoring_operation = link_operation(
            source.farm, active_user, [new.pk, source.product_id], removed=source
        )
    restoring, competing = run_in_lock_order(
        restoring_operation,
        other,
        left_first=readd_first,
    )

    receipt, created = restoring.result()
    assert created is True
    if revision_aware:
        assert (receipt.resource_id, receipt.created_count) == (source.farm_id, 2)
        assert_links(source.farm, [new.pk, source.product_id], restored=source)
    else:
        assert receipt.resource_id == source.pk
    if readd_first:
        code = (
            "farm_product_generation_changed"
            if operation == "farmer-approval"
            else "farm_product_version_conflict"
        )
    else:
        code = "farm_product_removed"
    with pytest.raises(services.FarmConflict, match=f"^{code}$"):
        competing.result()
    assert_source_state(source, removed=False, version=3, generation=2)
    assert source_events(source, "readd") == 1
    assert source_events(source, "remove") == 1
    assert AdminCreationReceipt.objects.get().pk == receipt.pk
    assert not source.stock_batches.exists()
    if operation == "farmer-approval":
        entry.refresh_from_db()
        assert entry.status == FarmSupplyRequest.Status.PENDING
        assert entry.items.get().approved_batch_id is None
        assert not ContentAuditLog.objects.filter(action="farm.supply_request.approved").exists()


@pytest.mark.parametrize("mixed_first", [True, False], ids=["mixed-first", "single-first"])
def test_mixed_and_single_readd_do_not_both_change_the_same_generation(active_user, mixed_first):
    farm = make_farm()
    new, product = make_product("NEW"), make_product("RESTORE")
    source = removed_source(farm, product.pk, active_user)
    mixed = link_operation(farm, active_user, [new.pk, product.pk], removed=source)
    single = partial(
        services.readd_farm_product, **source_command(source, active_user), idempotency_key=uuid4()
    )
    results = run_in_lock_order(mixed, single, left_first=mixed_first)

    winning, losing = results if mixed_first else reversed(results)
    receipt, created = winning.result()
    assert created
    with pytest.raises(services.FarmConflict, match="^farm_product_version_conflict$"):
        losing.result()
    assert_links(farm, [new.pk, product.pk] if mixed_first else [product.pk], restored=source)
    assert AdminCreationReceipt.objects.get().pk == receipt.pk


@pytest.mark.parametrize("link_first", [True, False], ids=["link-first", "checkout-first"])
def test_link_only_farm_cannot_supply_concurrent_checkout(
    store_product, active_user, checkout_operation, link_first
):
    supplying = store_product.farm_offers.get()
    linked_farm = make_farm()

    linking, checking_out = run_in_lock_order(
        link_operation(linked_farm, active_user, [store_product.pk]),
        checkout_operation,
        left_first=link_first,
    )

    assert linking.result()[1] is True
    order, _, created = checking_out.result()
    assert created is True
    assert_links(linked_farm, [store_product.pk])
    reservation = StoreStockReservation.objects.get(line__order=order)
    assert (reservation.farm_id, reservation.quantity) == (supplying.farm_id, 2)
    assert not StoreStockMovement.objects.exists()
    assert active_product_stock_totals([store_product.pk]) == {store_product.pk: 10}


@pytest.mark.parametrize("link_first", [True, False], ids=["link-first", "csa-first"])
def test_link_only_farm_cannot_supply_concurrent_csa_selection(context, link_first):
    actor, product, *_ = context
    supplying = product.farm_offers.get()
    linked_farm = make_farm()

    linking, selecting = run_in_lock_order(
        link_operation(linked_farm, actor, [product.pk]),
        partial(save_selection, context),
        left_first=link_first,
    )

    assert linking.result()[1] is True
    selection = selecting.result()
    assert_links(linked_farm, [product.pk])
    hold = CSAStockHold.objects.get(selection=selection)
    assert (hold.farm_id, hold.quantity) == (supplying.farm_id, 2)
    assert active_product_stock_totals([product.pk]) == {product.pk: 10}
