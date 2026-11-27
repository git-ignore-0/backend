from datetime import timedelta

import pytest
from django.utils import timezone

from commerce.models import CatalogTreeLock, StoreStockReservation
from commerce.serializers import StoreCheckoutSerializer
from commerce.store_orders import StoreOrderError, create_store_checkout, preview_store_voucher
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import guest_payload
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area


def supply(product, name, quantity, *, ends_in=4):
    today = timezone.localdate()
    farm = Farm.objects.create(
        name_vi=name, name_en=name, location_vi="Việt Nam", location_en="Vietnam"
    )
    offer = FarmProduct.objects.create(farm=farm, product=product)
    FarmProductStockBatch.objects.create(
        farm_product=offer,
        start_date=today,
        end_date=today + timedelta(days=ends_in),
        quantity_units=quantity,
    )
    return farm


def checkout_payload(product, **kwargs):
    serializer = StoreCheckoutSerializer(data=guest_payload(product, **kwargs))
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def test_checkout_reserves_one_farm_and_cannot_use_fragmented_capacity(
    store_product, payment_settings
):
    product = store_product
    batch = FarmProductStockBatch.objects.get(farm_product__product=product)
    batch.quantity_units = 6
    batch.save(update_fields=["quantity_units"])
    alternative = supply(product, "Alternative", 6)
    first_payload = checkout_payload(product, quantity=4, expected_available=6)
    first, _, _ = create_store_checkout(user=None, payload=first_payload)
    second, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(product, quantity=4, expected_available=6)
    )
    assert set(
        StoreStockReservation.objects.filter(line__order__in=[first, second]).values_list(
            "farm_id", flat=True
        )
    ) == {batch.farm_product.farm_id, alternative.pk}
    with pytest.raises(StoreOrderError) as caught:
        preview_store_voucher(
            user=None, payload=checkout_payload(product, quantity=4, expected_available=2)
        )
    assert caught.value.code == "store_stock_changed"
    assert caught.value.context["available_quantity"] == 2


def test_public_catalog_and_quote_share_single_farm_limit(client, store_product):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    product = store_product
    original = FarmProductStockBatch.objects.get(farm_product__product=product)
    original.quantity_units = 6
    original.save(update_fields=["quantity_units"])
    supply(product, "Alternative", 8)

    with CaptureQueriesContext(connection) as listing:
        response = client.get("/api/v1/public/store-products?locale=vi")
    assert response.status_code == 200
    assert response.json()["data"][0]["available_quantity"] == 8
    assert len(listing) <= 14
    response = client.get(f"/api/v1/public/store-products/{product.slug}?locale=vi")
    assert response.status_code == 200
    response = response.json()["data"]
    assert response["available_quantity"] == 8
    assert len(response["suppliers"]) == 2
    quote = preview_store_voucher(
        user=None, payload=checkout_payload(product, quantity=8, expected_available=8)
    )
    assert quote["lines"][0]["expected_available_quantity"] == 8


def test_legacy_holds_are_placed_whole_or_safely_blocked(store_product, payment_settings):
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from commerce.inventory import inventory_capacity

    product = store_product
    original = FarmProductStockBatch.objects.get(farm_product__product=product)
    other = supply(product, "Alternative", 6)
    order, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(product, quantity=7, expected_available=10)
    )
    hold = StoreStockReservation.objects.get(line__order=order)
    StoreStockReservation.objects.filter(pk=hold.pk).update(farm_id=None)
    assert inventory_capacity([product.pk], now=timezone.now()).maximum(product.pk) == 6

    output = StringIO()
    call_command("check_farm_fulfillment", stdout=output)
    assert '"unassigned_holds": 1' in output.getvalue()
    assert '"blocked": 0' in output.getvalue()

    original.quantity_units = 6
    original.save(update_fields=["quantity_units"])
    capacity = inventory_capacity([product.pk], now=timezone.now())
    assert capacity.maximum(product.pk) == 0
    assert capacity.legacy_shortfalls[product.pk] == (7, 6)
    assert capacity.physical[product.pk, other.pk] == 6
    with pytest.raises(CommandError, match="no verified single-Farm plan"):
        call_command("check_farm_fulfillment", stdout=output)
    assert '"blocked": 1' in output.getvalue()
    hold.refresh_from_db()
    assert hold.quantity == 7 and hold.farm_id is None and hold.released_at is None

    StoreStockReservation.objects.filter(pk=hold.pk).update(released_at=timezone.now())
    assert inventory_capacity([product.pk], now=timezone.now()).maximum(product.pk) == 6


def test_checkout_rejects_bool_quantity_at_service_boundary(store_product, payment_settings):
    payload = checkout_payload(store_product, quantity=1)
    payload["lines"][0]["quantity"] = True
    with pytest.raises(StoreOrderError, match="store_order_invalid"):
        create_store_checkout(user=None, payload=payload)
    assert not StoreStockReservation.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_nullable_farm_migrations_preserve_legacy_store_and_csa_rows(
    store_product, payment_settings, context
):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from memberships.models import CSAStockHold

    # Store persistence crosses the migration boundary; CSA schema is rolled back
    # alongside it so the test also exercises its independent migration graph.
    CatalogTreeLock.objects.get_or_create(pk=1)
    order, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(store_product, quantity=3)
    )
    hold = StoreStockReservation.objects.get(line__order=order)
    original_assignment = hold.farm_id
    csa_hold = save(context, quantity=2).stock_holds.get()
    assert csa_hold.farm_id is not None
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    legacy = [
        ("commerce", "0016_product_version"),
        ("memberships", "0044_csa_schedule_versions"),
    ]
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(legacy)
        apps = executor.loader.project_state(legacy).apps
        for app, model in [("commerce", "StoreStockReservation"), ("memberships", "CSAStockHold")]:
            fields = {field.name for field in apps.get_model(app, model)._meta.get_fields()}
            assert "farm" not in fields
        old_holds = apps.get_model("commerce", "StoreStockReservation")
        assert old_holds.objects.get(pk=hold.pk).quantity == 3

        MigrationExecutor(connection).migrate(latest)
        restored = StoreStockReservation.objects.get(pk=hold.pk)
        assert restored.farm_id is None
        assert restored.quantity == hold.quantity
        assert restored.released_at == hold.released_at
        assert restored.line.order.request_fingerprint == order.request_fingerprint
        preserved_csa = CSAStockHold.objects.get(pk=csa_hold.pk)
        assert preserved_csa.quantity == csa_hold.quantity
        assert preserved_csa.selection_id == csa_hold.selection_id
        assert preserved_csa.farm_id is None
        assert original_assignment is not None
    finally:
        MigrationExecutor(connection).migrate(latest)


def test_checkout_keeps_the_quantity_ceiling_separate_from_farm_capacity(
    store_product, payment_settings
):
    product = store_product
    batch = FarmProductStockBatch.objects.get(farm_product__product=product)
    batch.quantity_units = 1_000
    batch.save(update_fields=["quantity_units"])
    wrong = StoreCheckoutSerializer(
        data=guest_payload(product, quantity=1_000, expected_available=1_000)
    )
    assert not wrong.is_valid()
    order, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(product, quantity=999, expected_available=1_000)
    )
    hold = StoreStockReservation.objects.get(line__order=order)
    assert (hold.quantity, hold.farm_id) == (999, batch.farm_product.farm_id)


def test_a_new_assignment_cannot_invalidate_the_verified_legacy_placement(
    store_product, payment_settings
):
    from commerce.inventory import inventory_capacity

    product = store_product
    original = FarmProductStockBatch.objects.get(farm_product__product=product)
    original.quantity_units = 50
    original.save(update_fields=["quantity_units"])
    supply(product, "Second", 5)
    supply(product, "Third", 5)
    for quantity, available in zip((3, 3, 2, 2, 2), (50, 47, 44, 42, 40), strict=True):
        order, _, _ = create_store_checkout(
            user=None,
            payload=checkout_payload(product, quantity=quantity, expected_available=available),
        )
    StoreStockReservation.objects.update(farm_id=None)
    lots = product.farm_offers.order_by("farm_id").values_list("pk", "farm_id")
    for (source_id, _), quantity in zip(lots, (3, 5, 5), strict=True):
        FarmProductStockBatch.objects.filter(farm_product_id=source_id).update(
            quantity_units=quantity
        )

    capacity = inventory_capacity([product.pk], now=timezone.now())
    assert capacity.maximum(product.pk) == 1
    order, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(product, quantity=1, expected_available=1)
    )
    assert StoreStockReservation.objects.get(line__order=order).farm_id == lots[2][1]
    capacity = inventory_capacity([product.pk], now=timezone.now())
    assert capacity.maximum(product.pk) == 0
    assert product.pk not in capacity.legacy_shortfalls
    assert capacity.legacy_hold_counts[product.pk] == 5
