import uuid
from datetime import timedelta
from functools import partial

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import connection, transaction
from django.utils import timezone

from commerce.models import (
    Product,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockMovementKind,
)
from commerce.serializers import StoreCheckoutSerializer
from commerce.store_orders import (
    StoreOrderError,
    approve_store_order,
    cancel_store_order,
    create_store_checkout,
)
from content.models import ContentAuditLog, MediaAsset, TrackerFarm
from content.services import delete_tracker_farm, update_tracker_farm
from farms.models import Farm, FarmPhoto, FarmProduct, FarmProductStockBatch
from farms.services import (
    FarmConflict,
    add_farm_product,
    create_growth_tracker_profile,
    delete_farm,
    delete_growth_tracker_profile,
    readd_farm_product,
    remove_farm_product,
    update_farm,
    update_growth_tracker_profile,
)
from tests.test_farm_lifecycle_locking import record_resource_locks, second_product_at_same_farm
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_product_lifecycle_locking import run_in_lock_order
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

requires_mysql = pytest.mark.skipif(connection.vendor != "mysql", reason="Requires MySQL row locks")


@pytest.fixture
def stock_data():
    today = timezone.localdate()
    return {"start_date": today, "end_date": today + timedelta(days=2), "quantity_units": 4}


def another_farm():
    return Farm.objects.create(
        name_en="Farm B",
        name_vi="Nông trại B",
        location_en="Da Lat",
        location_vi="Đà Lạt",
    )


def remove_supply(source, actor):
    return remove_farm_product(
        farm_id=source.farm_id,
        product_id=source.product_id,
        expected_link_id=source.pk,
        expected_version=source.version,
        actor=actor,
    )


@pytest.mark.django_db
def test_farm_deletion_locks_only_its_stock_sources_in_order(
    store_product, active_user, stock_data, monkeypatch
):
    first_source = store_product.farm_offers.get()
    first_batch = first_source.stock_batches.get()
    second_batch = second_product_at_same_farm(store_product)
    second_source = second_batch.farm_product
    remove_supply(first_source, active_user)
    remove_supply(second_source, active_user)
    farm_id = first_source.farm_id
    product_ids = sorted([first_source.product_id, second_source.product_id])
    other_farm = another_farm()
    other_source, other_batch = add_farm_product(
        farm_id=other_farm.pk, product_id=store_product.pk, actor=active_user, **stock_data
    )
    recorded = record_resource_locks(monkeypatch)

    delete_farm(farm_id=farm_id, actor=active_user)

    resource_models = {Product, FarmProduct, Farm, FarmProductStockBatch}
    locks = [(model, ids, query) for model, ids, query in recorded if model in resource_models]
    assert [(model, ids) for model, ids, _ in locks] == [
        (Product, product_ids),
        (FarmProduct, sorted([first_source.pk, second_source.pk])),
        (Farm, [farm_id]),
        (FarmProductStockBatch, sorted([first_batch.pk, second_batch.pk])),
    ]
    assert all(
        sum(count > 0 for count in query.alias_refcount.values()) == 1 for _, _, query in locks
    )
    assert not Farm.objects.filter(pk=farm_id).exists()
    assert Product.objects.filter(pk__in=product_ids).count() == 2
    assert FarmProduct.objects.filter(pk=other_source.pk).exists()
    assert FarmProductStockBatch.objects.filter(pk=other_batch.pk).exists()


@pytest.mark.django_db(transaction=True)
@requires_mysql
@pytest.mark.parametrize("deletion_first", [True, False], ids=["delete-first", "attach-first"])
@pytest.mark.parametrize("operation", ["add", "readd"])
def test_mysql_farm_deletion_and_attachment_serialize(
    store_product, active_user, stock_data, deletion_first, operation
):
    removed = remove_supply(store_product.farm_offers.get(), active_user)
    farm_id = removed.farm_id
    if operation == "add":
        # A different Product is absent from the deletion's initial source set.
        new_product = Product.objects.get(pk=store_product.pk)
        new_product.pk = uuid.uuid4()
        new_product.sku = "CARROTS"
        new_product.slug = "carrots"
        new_product.save(force_insert=True)
        lock_model = Farm

        def attach():
            return add_farm_product(
                farm_id=farm_id, product_id=new_product.pk, actor=active_user, **stock_data
            )

    else:
        lock_model = Product

        def attach():
            return readd_farm_product(
                farm_id=farm_id,
                product_id=store_product.pk,
                expected_link_id=removed.pk,
                expected_version=removed.version,
                actor=active_user,
                data=stock_data,
            )

    deleting, attaching = run_in_lock_order(
        lambda: delete_farm(farm_id=farm_id, actor=active_user),
        attach,
        left_first=deletion_first,
        lock_model=lock_model,
    )

    assert Product.objects.count() == (2 if operation == "add" else 1)
    if deletion_first:
        deleting.result()
        if operation == "add":
            with pytest.raises(Farm.DoesNotExist):
                attaching.result()
        else:
            with pytest.raises(FarmConflict, match="^farm_product_changed$"):
                attaching.result()
        assert not Farm.objects.filter(pk=farm_id).exists()
        assert not FarmProduct.objects.filter(farm_id=farm_id).exists()
        assert not FarmProductStockBatch.objects.filter(farm_product__farm_id=farm_id).exists()
        return

    source, batch = attaching.result()
    with pytest.raises(FarmConflict, match="^farm_(in_use|product_changed)$"):
        deleting.result()
    assert Farm.objects.filter(pk=farm_id).exists()
    source.refresh_from_db()
    batch.refresh_from_db()
    assert source.farm_id == farm_id
    assert source.removed_at is None
    assert batch.farm_product_id == source.pk
    assert batch.quantity_units == stock_data["quantity_units"]
    assert batch.supply_generation == (1 if operation == "add" else 2)


@pytest.mark.django_db(transaction=True)
@requires_mysql
@pytest.mark.parametrize("deletion_first", [True, False], ids=["delete-first", "approval-first"])
def test_mysql_farm_deletion_and_store_approval_serialize(
    store_product, active_user, payment_settings, stock_data, deletion_first
):
    source = store_product.farm_offers.get()
    farm_id = source.farm_id
    batch_id = source.stock_batches.get().pk
    # Pending Store reservations belong to the Product. Alternate stock lets
    # the Farm retire its last source without stranding the pending order.
    add_farm_product(
        farm_id=another_farm().pk,
        product_id=store_product.pk,
        actor=active_user,
        **stock_data,
    )
    payload = StoreCheckoutSerializer(data=guest_payload(store_product, expected_available=14))
    payload.is_valid(raise_exception=True)
    order, _, created = create_store_checkout(user=None, payload=payload.validated_data)
    assert created
    line = order.lines.get()

    def approve():
        with transaction.atomic():
            approve_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                allocations=[{"line_id": line.pk, "batch_id": batch_id, "quantity": line.quantity}],
            )
            # Commit only a removed source with historical sales, so a check
            # limited to current supply or net sales would delete it.
            cancel_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                reason="Delivery cancelled",
            )
            remove_supply(source, active_user)

    def delete():
        with transaction.atomic():
            if deletion_first:
                remove_supply(source, active_user)
            delete_farm(farm_id=farm_id, actor=active_user)

    deleting, approving = run_in_lock_order(delete, approve, left_first=deletion_first)
    order.refresh_from_db()

    if deletion_first:
        deleting.result()
        with pytest.raises(StoreOrderError, match="^store_allocation_stale$"):
            approving.result()
        assert order.status == StoreOrderStatus.PENDING_REVIEW
        assert line.stock_reservations.get().released_at is None
        assert not line.stock_allocations.exists()
        assert not line.stock_movements.exists()
        assert not order.actions.exists()
        assert not Farm.objects.filter(pk=farm_id).exists()
        assert not FarmProductStockBatch.objects.filter(pk=batch_id).exists()
        return

    approving.result()
    with pytest.raises(FarmConflict, match="^farm_in_use$"):
        deleting.result()
    assert order.status == StoreOrderStatus.CANCELLED
    assert Farm.objects.filter(pk=farm_id).exists()
    assert FarmProduct.objects.get(pk=source.pk).removed_at is not None
    assert FarmProductStockBatch.objects.filter(pk=batch_id).exists()
    assert StoreStockAllocation.objects.get(line=line, batch_id=batch_id).quantity == line.quantity
    movements = StoreStockMovement.objects.filter(line=line, batch_id=batch_id).order_by("kind")
    assert list(movements.values_list("kind", "quantity")) == [
        (StoreStockMovementKind.RESTOCK, line.quantity),
        (StoreStockMovementKind.SALE, line.quantity),
    ]


@pytest.mark.django_db(transaction=True)
@requires_mysql
@pytest.mark.parametrize("delete_first", [True, False], ids=["delete-first", "patch-first"])
@pytest.mark.parametrize("data", [{"name_en": "Edited Farm"}, {"is_active": False}])
def test_mysql_farm_deletion_and_patch_serialize(store_product, active_user, delete_first, data):
    source = remove_supply(store_product.farm_offers.get(), active_user)
    farm = source.farm
    # Status PATCH and DELETE meet at Product; information PATCH meets DELETE at Farm.
    lock_model = Product if "is_active" in data else Farm
    deleting, patching = run_in_lock_order(
        partial(delete_farm, farm_id=farm.pk, actor=active_user),
        partial(
            update_farm,
            farm_id=farm.pk,
            actor=active_user,
            expected_version=farm.version,
            data=data,
        ),
        left_first=delete_first,
        lock_model=lock_model,
    )

    deleting.result()
    if delete_first:
        with pytest.raises(Farm.DoesNotExist):
            patching.result()
    else:
        updated = patching.result()
        assert updated.version == farm.version + 1
        for field, value in data.items():
            assert getattr(updated, field) == value
    assert ContentAuditLog.objects.filter(action="farm.update", entity_id=str(farm.pk)).count() == (
        0 if delete_first else 1
    )
    assert not Farm.objects.filter(pk=farm.pk).exists()
    assert not FarmProduct.objects.filter(pk=source.pk).exists()
    assert Product.objects.filter(pk=store_product.pk).exists()


@pytest.mark.django_db(transaction=True)
@requires_mysql
@pytest.mark.parametrize("delete_first", [True, False], ids=["delete-first", "tracker-first"])
@pytest.mark.parametrize(
    ("service", "creates_profile", "versioned"),
    [
        (create_growth_tracker_profile, True, False),
        (update_growth_tracker_profile, False, True),
        (delete_growth_tracker_profile, False, False),
        (update_tracker_farm, False, True),
        (delete_tracker_farm, False, False),
    ],
    ids=["create-profile", "update-profile", "delete-profile", "legacy-patch", "legacy-delete"],
)
def test_mysql_farm_deletion_and_tracker_writes_serialize(
    active_user, delete_first, service, creates_profile, versioned
):
    farm = another_farm()
    params = {"farm_id": farm.pk, "actor": active_user}
    if not creates_profile:
        profile = create_growth_tracker_profile(**params, data={})
    if creates_profile or versioned:
        params["data"] = {"signup_count": 7}
    if versioned:
        params["expected_version"] = profile.version

    deleting, writing = run_in_lock_order(
        partial(delete_farm, farm_id=farm.pk, actor=active_user),
        partial(service, **params),
        left_first=delete_first,
        lock_model=Farm,
    )

    deleting.result()
    if delete_first:
        with pytest.raises(Farm.DoesNotExist):
            writing.result()
    else:
        result = writing.result()
        if creates_profile or versioned:
            assert result.signup_count == 7
    assert not Farm.objects.filter(pk=farm.pk).exists()
    assert not TrackerFarm.objects.filter(farm_id=farm.pk).exists()


@pytest.mark.django_db(transaction=True)
@requires_mysql
def test_mysql_concurrent_deletions_of_same_farm_only_commit_once(store_product, active_user):
    source = remove_supply(store_product.farm_offers.get(), active_user)
    farm_id = source.farm_id
    profile = create_growth_tracker_profile(farm_id=farm_id, actor=active_user, data={})
    delete = partial(delete_farm, farm_id=farm_id, actor=active_user)

    winner, contender = run_in_lock_order(delete, delete, left_first=True)

    winner.result()
    with pytest.raises(Farm.DoesNotExist):
        contender.result()
    assert not Farm.objects.filter(pk=farm_id).exists()
    assert not TrackerFarm.objects.filter(pk=profile.pk).exists()
    assert not FarmProduct.objects.filter(pk=source.pk).exists()
    assert ContentAuditLog.objects.filter(action="farm.delete", entity_id=str(farm_id)).count() == 1
    assert (
        ContentAuditLog.objects.filter(
            action="tracker_farm.delete", entity_id=str(profile.pk)
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
@requires_mysql
@pytest.mark.parametrize("retain_reference", [False, True], ids=["orphaned", "still-referenced"])
def test_mysql_concurrent_farm_deletions_serialize_shared_media_cleanup(
    active_user, retain_reference
):
    first, second = another_farm(), another_farm()
    original = default_storage.save("farms/shared.webp", ContentFile(b"original"))
    variant = default_storage.save("farms/shared-small.webp", ContentFile(b"variant"))
    asset = MediaAsset.objects.create(
        file=original, variants=[{"file": variant}], width=100, height=100
    )
    FarmPhoto.objects.create(farm=first, image=asset, sort_order=0)
    second.image = asset
    second.save(update_fields=["image"])
    if retain_reference:
        FarmPhoto.objects.create(farm=another_farm(), image=asset, sort_order=0)

    deleting_first, deleting_second = run_in_lock_order(
        partial(delete_farm, farm_id=first.pk, actor=active_user),
        partial(delete_farm, farm_id=second.pk, actor=active_user),
        left_first=True,
        lock_model=MediaAsset,
    )

    deleting_first.result()
    deleting_second.result()
    assert not Farm.objects.filter(pk__in=[first.pk, second.pk]).exists()
    assert MediaAsset.objects.filter(pk=asset.pk).exists() is retain_reference
    assert default_storage.exists(original) is retain_reference
    assert default_storage.exists(variant) is retain_reference
