from datetime import timedelta

import pytest
from django.utils import timezone

from commerce.models import StoreOrder, StoreStockReservation
from farms import services
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from farms.views import source_capabilities
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_source_lifecycle import order_for
from tests.test_farm_source_lifecycle_mysql import batch_data, source_command
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def add_backup_farm(product, quantity):
    farm = Farm.objects.create(
        name_en="Backup", name_vi="Dự phòng", location_en="Da Lat", location_vi="Đà Lạt"
    )
    source = FarmProduct.objects.create(farm=farm, product=product)
    FarmProductStockBatch.objects.create(farm_product=source, **batch_data(quantity))
    return farm


def batch_command(source, batch, actor):
    return {
        "farm_id": source.farm_id,
        "product_id": source.product_id,
        "batch_id": batch.pk,
        "expected_version": batch.version,
        "actor": actor,
    }


def test_batch_reductions_and_cancellation_protect_the_assigned_farm(
    store_product, active_user, payment_settings
):
    source = store_product.farm_offers.get()
    batch = source.stock_batches.get()
    order = order_for(store_product, quantity=8)
    add_backup_farm(store_product, 100)

    updated = services.update_stock_batch(
        **batch_command(source, batch, active_user), data={"quantity_units": 8}
    )
    with pytest.raises(services.FarmConflict, match="^stock_reserved$"):
        services.update_stock_batch(
            **batch_command(source, updated, active_user), data={"quantity_units": 7}
        )
    with pytest.raises(services.FarmConflict, match="^stock_reserved$"):
        services.cancel_stock_batch(**batch_command(source, updated, active_user))

    batch.refresh_from_db()
    reservation = order.lines.get().stock_reservations.get()
    assert (batch.quantity_units, batch.version, batch.cancelled_at) == (8, 2, None)
    assert (reservation.farm_id, reservation.released_at) == (source.farm_id, None)


def test_removal_capability_and_mutation_protect_csa_hold_on_assigned_source(context):
    actor, product, *_ = context
    source = product.farm_offers.get()
    selection = save_selection(context, quantity=4)
    add_backup_farm(product, 100)

    capability = source_capabilities(source, now=timezone.now())
    assert capability["can_remove"] is False
    with pytest.raises(services.FarmConflict, match="^farm_product_supply_shortfall$") as exc:
        services.remove_farm_product(**source_command(source, actor))
    assert capability["blockers"]["remove"] == {
        "code": exc.value.code,
        "context": {"required_units": 4, "remaining_units": 0, "shortfall_units": 4},
    }
    assert selection.stock_holds.get().farm_id == source.farm_id


def test_deactivation_rejects_assigned_stock_even_with_a_larger_backup(
    store_product, active_user, payment_settings
):
    farm = store_product.farm_offers.get().farm
    order = order_for(store_product, quantity=8)
    backup = add_backup_farm(store_product, 100)

    with pytest.raises(services.FarmConflict, match="^stock_reserved$"):
        services.update_farm(
            farm_id=farm.pk, actor=active_user, expected_version=1, data={"is_active": False}
        )
    services.update_farm(
        farm_id=backup.pk, actor=active_user, expected_version=1, data={"is_active": False}
    )
    farm.refresh_from_db()
    assert (farm.is_active, farm.version) == (True, 1)
    assert order.lines.get().stock_reservations.get().farm_id == farm.pk


def test_removal_reports_legacy_hold_shortfall_when_backup_stock_is_fragmented(
    store_product, active_user, payment_settings
):
    source = store_product.farm_offers.get()
    order = order_for(store_product, quantity=8)
    add_backup_farm(store_product, 6)
    add_backup_farm(store_product, 6)
    StoreStockReservation.objects.filter(line__order=order).update(farm=None)

    with pytest.raises(services.FarmConflict, match="^farm_product_supply_shortfall$") as exc:
        services.remove_farm_product(**source_command(source, active_user))
    assert exc.value.context == {"required_units": 8, "remaining_units": 6, "shortfall_units": 2}
    assert order.lines.get().stock_reservations.get().farm_id is None


@pytest.mark.parametrize("terminal", ["released", "expired"])
def test_delete_capability_and_mutation_guard_active_reservation_then_keep_historical_row(
    store_product, active_user, payment_settings, terminal
):
    source = store_product.farm_offers.get()
    order = order_for(store_product, quantity=8)
    # Exercise the final preflight for a source withdrawn before this check was introduced.
    FarmProduct.objects.filter(pk=source.pk).update(is_active=False, removed_at=timezone.now())
    decision = services.farm_capabilities(source.farm_id)["delete"]
    assert decision == {
        "allowed": False,
        "reasons": [{"code": "store_order", "id": str(order.pk), "label": order.reference_code}],
    }
    with pytest.raises(services.FarmConflict, match="^farm_in_use$") as exc:
        services.delete_farm(farm_id=source.farm_id, actor=active_user)
    assert exc.value.context == {"reasons": decision["reasons"]}

    reservation = order.lines.get().stock_reservations.get()
    if terminal == "released":
        StoreStockReservation.objects.filter(pk=reservation.pk).update(released_at=timezone.now())
    else:
        StoreOrder.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
    assert services.farm_capabilities(source.farm_id)["delete"] == {"allowed": True, "reasons": []}

    services.delete_farm(farm_id=source.farm_id, actor=active_user)
    reservation.refresh_from_db()
    assert (reservation.quantity, reservation.farm_id) == (8, None)
    assert not Farm.objects.filter(pk=source.farm_id).exists()
