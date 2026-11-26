import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import (
    Product,
    StoreOrder,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
)
from commerce.store_orders import (
    StoreOrderError,
    _restore_committed_stock,
    active_product_stock_totals,
    approve_store_order,
    restock_store_order,
)
from content.models import ContentAuditLog
from farms import services
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from memberships.models import CSAStockHold
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_farms_api import farm_payload
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def intent(link, actor):
    return {
        "farm_id": link.farm_id,
        "product_id": link.product_id,
        "expected_link_id": link.pk,
        "expected_version": link.version,
        "actor": actor,
    }


def values(quantity, *, days=0):
    today = timezone.localdate()
    return {
        "start_date": today + timedelta(days=days),
        "end_date": today + timedelta(days=days + 5),
        "quantity_units": quantity,
    }


@pytest.mark.parametrize("status", ["draft", "active", "discontinued"])
def test_remove_preserves_link_and_batches_for_every_product_status(
    store_product, active_user, status
):
    source = store_product.farm_offers.get()
    old_batch = source.stock_batches.get()
    Product.objects.filter(pk=store_product.pk).update(
        status=status, is_active=status != "discontinued", is_published=status == "active"
    )

    removed = services.remove_farm_product(**intent(source, active_user))

    assert removed.pk == source.pk
    assert removed.removed_by_id == active_user.pk
    assert removed.removed_at is not None
    assert (removed.is_active, removed.version, removed.supply_generation) == (False, 2, 1)
    old_batch.refresh_from_db()
    assert (old_batch.supply_generation, old_batch.quantity_units, old_batch.cancelled_at) == (
        1,
        10,
        None,
    )
    assert active_product_stock_totals([store_product.pk]).get(store_product.pk, 0) == 0
    store_product.refresh_from_db()
    assert store_product.status == status
    assert (
        ContentAuditLog.objects.filter(action="farm.product.remove", entity_id=source.pk).count()
        == 1
    )


def test_readd_preserves_id_and_excludes_all_past_generations(store_product, active_user):
    source = store_product.farm_offers.get()
    old = source.stock_batches.get()
    source.stock_batches.create(**values(50, days=1))
    removed = services.remove_farm_product(**intent(source, active_user))
    with pytest.raises(services.FarmConflict, match="^farm_product_removed$"):
        services.add_farm_product(
            farm_id=source.farm_id, product_id=source.product_id, actor=active_user, **values(4)
        )
    restored, first = services.readd_farm_product(**intent(removed, active_user), data=values(4))
    assert restored.pk == source.pk
    assert restored.removed_at is None and restored.removed_by is None
    assert (restored.version, first.supply_generation) == (3, 2)
    assert active_product_stock_totals([store_product.pk]) == {store_product.pk: 4}

    batch = services.create_stock_batch(**intent(restored, active_user), data=values(3))
    assert batch.supply_generation == 2
    with pytest.raises(services.FarmConflict, match="farm_product_version_conflict"):
        services.create_stock_batch(**intent(restored, active_user), data=values(3))
    kwargs = {
        "farm_id": source.farm_id,
        "product_id": source.product_id,
        "batch_id": old.pk,
        "expected_version": old.version,
        "actor": active_user,
    }
    with pytest.raises(services.FarmConflict, match="farm_product_generation_changed"):
        services.update_stock_batch(**kwargs, data={"quantity_units": 1})
    with pytest.raises(services.FarmConflict, match="farm_product_generation_changed"):
        services.cancel_stock_batch(**kwargs)

    restored.refresh_from_db()
    removed_again = services.remove_farm_product(**intent(restored, active_user))
    current, latest = services.readd_farm_product(
        **intent(removed_again, active_user), data=values(2)
    )
    assert current.pk == source.pk
    assert latest.supply_generation == 3
    assert source.stock_batches.count() == 5
    assert active_product_stock_totals([store_product.pk]) == {store_product.pk: 2}


def test_stale_or_wrong_link_intent_and_non_selling_readd_are_rejected(store_product, active_user):
    source = store_product.farm_offers.get()
    with pytest.raises(services.FarmConflict, match="farm_product_identity_changed"):
        services.remove_farm_product(
            **(intent(source, active_user) | {"expected_link_id": uuid.uuid4()})
        )
    removed = services.remove_farm_product(**intent(source, active_user))
    with pytest.raises(services.FarmConflict, match="farm_product_version_conflict"):
        services.readd_farm_product(**intent(source, active_user), data=values(2))
    Product.objects.filter(pk=store_product.pk).update(
        status="discontinued", is_active=False, is_published=False
    )
    with pytest.raises(services.FarmConflict, match="product_not_selling"):
        services.readd_farm_product(**intent(removed, active_user), data=values(2))
    assert source.stock_batches.count() == 1


def test_source_transition_rolls_back_if_new_batch_cannot_be_saved(
    store_product, active_user, monkeypatch
):
    source = store_product.farm_offers.get()
    removed = services.remove_farm_product(**intent(source, active_user))

    def fail(**kwargs):
        raise RuntimeError("batch write failed")

    monkeypatch.setattr(FarmProductStockBatch.objects, "create", fail)
    with pytest.raises(RuntimeError, match="batch write failed"):
        services.readd_farm_product(**intent(removed, active_user), data=values(2))
    removed.refresh_from_db()
    assert (removed.is_active, removed.version, removed.supply_generation) == (False, 2, 1)
    assert removed.removed_by_id == active_user.pk
    assert source.stock_batches.count() == 1
    assert not ContentAuditLog.objects.filter(
        action="farm.product.readd", entity_id=source.pk
    ).exists()


def order_for(product, *, expected_available=10, quantity=2):
    checkout = APIClient().post(
        "/api/v1/public/store-checkouts",
        guest_payload(product, expected_available=expected_available, quantity=quantity),
        format="json",
    )
    assert checkout.status_code == 201, checkout.json()
    return StoreOrder.objects.get(pk=checkout.json()["data"]["id"])


@pytest.mark.parametrize("remaining", [5, 3])
def test_remove_covers_store_and_csa_demand_without_changing_holds(
    context, payment_settings, remaining
):
    actor, product, _, _, _ = context
    source = product.farm_offers.get()
    other = FarmProduct.objects.create(
        farm=Farm.objects.create(**farm_payload(name_en="Other farm")), product=product
    )
    other.stock_batches.create(**values(remaining))
    selection = save(context)
    order = order_for(product, expected_available=10 + remaining - 2)
    if remaining == 3:
        with pytest.raises(services.FarmConflict, match="farm_product_supply_shortfall") as error:
            services.remove_farm_product(**intent(source, actor))
        assert error.value.context == {
            "required_units": 4,
            "remaining_units": 3,
            "shortfall_units": 1,
        }
        assert not ContentAuditLog.objects.filter(action="farm.product.remove").exists()
    else:
        services.remove_farm_product(**intent(source, actor))
    source.refresh_from_db()
    assert (source.removed_at is not None) == (remaining == 5)
    assert order.lines.get().stock_reservations.get().released_at is None
    assert CSAStockHold.objects.get(selection=selection).quantity == 2


@pytest.mark.parametrize("terminal", [StoreOrderStatus.DELIVERED, StoreOrderStatus.CANCELLED])
def test_open_allocation_blocks_even_with_other_farm_and_terminal_keeps_history(
    store_product, active_user, payment_settings, terminal
):
    source = store_product.farm_offers.get()
    batch = source.stock_batches.get()
    order = order_for(store_product)
    line = order.lines.get()
    approve_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        allocations=[{"line_id": line.pk, "batch_id": batch.pk, "quantity": line.quantity}],
    )
    other = FarmProduct.objects.create(
        farm=Farm.objects.create(**farm_payload(name_en="Other")), product=store_product
    )
    other.stock_batches.create(**values(50))
    # An elapsed payment deadline does not make an allocated order terminal.
    StoreOrder.objects.filter(pk=order.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(services.FarmConflict, match="farm_product_has_open_allocations") as error:
        services.remove_farm_product(**intent(source, active_user))
    assert error.value.context["orders"] == [
        {"id": str(order.pk), "reference_code": order.reference_code}
    ]

    StoreOrder.objects.filter(pk=order.pk).update(status=terminal)
    removed = services.remove_farm_product(**intent(source, active_user))
    assert removed.removed_at is not None
    allocation = StoreStockAllocation.objects.get(line=line, batch=batch)
    assert StoreStockMovement.objects.get(allocation=allocation, kind="sale").quantity == 2
    assert order.lines.get().sku_snapshot == store_product.sku

    if terminal == StoreOrderStatus.CANCELLED:
        restored, _ = services.readd_farm_product(**intent(removed, active_user), data=values(4))
        with pytest.raises(StoreOrderError, match="store_restock_invalid") as error:
            restock_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                allocations=[{"allocation_id": allocation.pk, "quantity": 1}],
            )
        assert error.value.context["reason"] == "batch_historical"
        _restore_committed_stock(order, active_user)
        assert (
            StoreStockMovement.objects.get(allocation=allocation, kind="restock").batch_id
            == batch.pk
        )
        assert active_product_stock_totals([store_product.pk]) == {store_product.pk: 54}
        assert restored.supply_generation == 2


def test_only_current_generation_enters_public_farm_store_and_csa_totals(context):
    from commerce.public_catalog import public_product_queryset
    from commerce.store_orders import active_product_stock
    from memberships.csa_rounds import selection_stock_capacity

    actor, product, _, _, _ = context
    source = product.farm_offers.get()
    alternate = FarmProduct.objects.create(
        farm=Farm.objects.create(**farm_payload(name_en="Alternate stock")), product=product
    )
    alternate.stock_batches.create(**values(5))
    selection = save(context)

    removed = services.remove_farm_product(**intent(source, actor))
    current, first = services.readd_farm_product(**intent(removed, actor), data=values(4))
    assert first.supply_generation == 2
    assert active_product_stock([product.pk])[0][product.pk] == 9
    assert active_product_stock_totals([product.pk]) == {product.pk: 9}
    assert services.farm_product_queryset().get(pk=current.pk).available_quantity == 4
    assert services.farm_queryset().get(pk=source.farm_id).in_stock_product_count == 1
    assert public_product_queryset().get(pk=product.pk).available_quantity == 7
    assert selection_stock_capacity([product.pk], selection) == {product.pk: 9}
    assert selection_stock_capacity([product.pk], None) == {product.pk: 7}


def test_remove_checks_shortfall_even_if_target_already_contributes_zero(context):
    actor, product, _, _, _ = context
    source = product.farm_offers.get()
    other = FarmProduct.objects.create(
        farm=Farm.objects.create(**farm_payload(name_en="Other farm")), product=product
    )
    other.stock_batches.create(**values(1))
    selection = save(context)
    source.stock_batches.update(end_date=timezone.localdate() - timedelta(days=1))

    with pytest.raises(services.FarmConflict, match="farm_product_supply_shortfall") as error:
        services.remove_farm_product(**intent(source, actor))

    assert error.value.context == {"required_units": 2, "remaining_units": 1, "shortfall_units": 1}
    assert services.farm_product_queryset().get(pk=source.pk).available_quantity == 0
    assert CSAStockHold.objects.get(selection=selection).quantity == 2
    source.refresh_from_db()
    assert source.removed_at is None


def test_source_inventory_suggestion_preserves_effective_other_demand(
    store_product, payment_settings
):
    from commerce.store_orders import source_inventory

    first = order_for(store_product, quantity=5)
    other = order_for(store_product, quantity=5, expected_available=5)
    store_product.farm_offers.get().stock_batches.update(quantity_units=7)

    inventory = source_inventory(first)
    assert inventory["suggestion"][0]["quantity"] == 2
    assert inventory["candidates"][0]["available_quantity"] == 7
    assert inventory["product_reservations"] == [
        {
            "product_id": str(store_product.pk),
            "reserved_quantity": 10,
            "this_order_quantity": 5,
            "other_orders_quantity": 5,
        }
    ]
    StoreOrder.objects.filter(pk=other.pk).update(expires_at=timezone.now())
    inventory = source_inventory(first)
    assert inventory["suggestion"][0]["quantity"] == 5
    assert inventory["product_reservations"][0]["other_orders_quantity"] == 0


def test_removing_last_source_preserves_public_product_and_activation_does_not_readd(
    store_product, active_user
):
    from commerce.services import transition_product

    source = store_product.farm_offers.get()
    farm = source.farm
    client = APIClient()
    public = f"/api/v1/public/store-products/{store_product.slug}?locale=vi"
    assert client.get(public).json()["data"]["available_quantity"] == 10

    services.remove_farm_product(**intent(source, active_user))
    still_listed = client.get(public)
    assert still_listed.status_code == 200
    assert still_listed.json()["data"]["id"] == str(store_product.pk)
    assert still_listed.json()["data"]["available_quantity"] == 0
    assert still_listed.json()["data"]["suppliers"] == []
    result = client.get("/api/v1/public/store-products", {"locale": "vi", "search": farm.name_vi})
    assert result.json()["data"] == []

    transition_product(
        actor=active_user,
        product_id=store_product.pk,
        expected_status="active",
        target_status="discontinued",
    )
    transition_product(
        actor=active_user,
        product_id=store_product.pk,
        expected_status="discontinued",
        target_status="active",
    )
    changed = services.update_farm(
        farm_id=farm.pk, expected_version=farm.version, actor=active_user, data={"is_active": False}
    )
    services.update_farm(
        farm_id=farm.pk,
        expected_version=changed.version,
        actor=active_user,
        data={"is_active": True},
    )
    source.refresh_from_db()
    assert source.supply_generation == 1
    assert source.removed_at is not None
    assert client.get(public).json()["data"]["available_quantity"] == 0
