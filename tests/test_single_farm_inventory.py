from collections import Counter
from datetime import timedelta
from itertools import combinations_with_replacement, product
from uuid import UUID, uuid4

import pytest
from django.utils import timezone

from commerce.inventory import _add_recovery_options, _pack_holds, _partition_assigned
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


def supply(product, name, quantity, *, ends_in=4, farm_id=None):
    today = timezone.localdate()
    farm = Farm.objects.create(
        pk=farm_id or uuid4(),
        name_vi=name,
        name_en=name,
        location_vi="Việt Nam",
        location_en="Vietnam",
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


def _whole_placement_exists(capacity, quantities):
    holds = list(quantities.elements())
    farms = tuple(capacity)
    for assignments in product(farms, repeat=len(holds)):
        used = Counter()
        for farm, quantity in zip(assignments, holds, strict=True):
            used[farm] += quantity
        if all(used[farm] <= capacity[farm] for farm in farms):
            return True
    return False


def test_whole_hold_packing_is_deterministic_and_only_publishes_verified_free_capacity():
    farms = (UUID(int=1), UUID(int=2))
    for sizes in range(4):
        for holds in combinations_with_replacement(range(1, 5), sizes):
            demand = Counter(holds)
            for quantities in product(range(6), repeat=len(farms)):
                physical = dict(zip(farms, quantities, strict=True))
                free, unplaced = _pack_holds(demand, physical)
                reverse = dict(reversed(tuple(physical.items())))
                assert (free, unplaced) == _pack_holds(Counter(reversed(holds)), reverse)
                if unplaced:
                    assert not free
                    assert unplaced[0] > unplaced[1] >= 0
                else:
                    assert all(0 <= free[farm] <= physical[farm] for farm in farms)
                    spent = {farm: physical[farm] - free[farm] for farm in farms}
                    assert sum(spent.values()) == sum(holds)
                    assert _whole_placement_exists(spent, demand)


def test_conservative_hold_placement_can_leave_an_existing_feasible_plan_unverified():
    first, second = UUID(int=1), UUID(int=2)
    physical = {first: 8, second: 7}
    holds = Counter((7, 5, 3))
    assert _whole_placement_exists(physical, holds)
    assert _pack_holds(holds, physical) == ({}, (3, 2))


def test_recovery_options_keep_protected_demand_after_proposed_consumption():
    origin, backup = UUID(int=1), UUID(int=2)
    for first, second, quantity, other, legacy_size in product(
        range(5), range(5), range(1, 5), range(4), range(3)
    ):
        physical = {origin: first, backup: second}
        assigned = {origin: Counter([other])} if other else {}
        legacy = Counter([legacy_size]) if legacy_size else Counter()
        own = {origin: Counter([quantity])}
        before_free, before_stranded, before_deficient = _partition_assigned(
            physical, {origin: Counter([quantity]) + assigned.get(origin, Counter())}
        )
        _, before_shortfall = _pack_holds(legacy + before_stranded, before_free)
        free, stranded, deficient = _partition_assigned(physical, assigned)
        plan, _ = _pack_holds(legacy + stranded, free)
        plan = {farm: units for farm, units in plan.items() if farm not in deficient}
        _add_recovery_options(plan, own, physical, assigned, legacy, for_approval=True)
        for target, available in plan.items():
            assert 0 <= available <= physical[target]
            if available < quantity:
                continue
            after = physical | {target: physical[target] - quantity}
            after_free, after_stranded, after_deficient = _partition_assigned(after, assigned)
            assert not after_deficient.difference(before_deficient)
            if before_shortfall is None:
                assert _whole_placement_exists(after_free, legacy + after_stranded)
            elif target == origin and origin not in before_deficient:
                assert (after_free, after_stranded) == (before_free, before_stranded)
            else:
                assert not after_deficient.difference(deficient)
                assert _whole_placement_exists(after_free, legacy)


@pytest.mark.parametrize(
    ("stocks", "own_size", "other_sizes", "legacy_sizes", "approve", "verified", "eligible"),
    [
        pytest.param((4, 2), 4, ((2,),), (), True, True, (0,), id="own-larger"),
        pytest.param((4, 4), 2, ((4,),), (), True, True, (0, 1), id="own-smaller"),
        pytest.param((4, 2, 2), 4, ((2, 2),), (), True, True, (0,), id="multiple-holds-and-farms"),
        pytest.param((4, 2, 3), 4, ((2,),), (3,), True, True, (0,), id="legacy-hold"),
        pytest.param((4, 4, 2), 4, ((2,), (2,)), (), True, True, (0,), id="healthy-other-hold"),
        pytest.param((5, 4, 4), 4, ((4,),), (5,), True, True, (), id="post-consumption-shortfall"),
        pytest.param((4, 1), 4, ((2,),), (), True, False, (), id="partial-rejects-origin"),
        pytest.param((0, 4), 4, ((3,),), (), True, False, (1,), id="partial-keeps-alternate"),
        pytest.param((4, 2), 4, ((2,),), (), False, True, (), id="csa-replacement"),
    ],
)
def test_contextual_capacity_verifies_proposed_consumption(
    stocks, own_size, other_sizes, legacy_sizes, approve, verified, eligible
):
    farms = [UUID(int=index) for index in range(1, len(stocks) + 1)]
    original = farms[0]
    physical = dict(zip(farms, stocks, strict=True))
    other = {farm: Counter(sizes) for farm, sizes in zip(farms, other_sizes, strict=False) if sizes}
    own = {original: Counter({own_size: 1})}
    legacy = Counter(legacy_sizes)
    all_assigned = other | {original: other.get(original, Counter()) + own[original]}
    full_free, full_stranded, full_deficient = _partition_assigned(physical, all_assigned)
    _, full_shortfall = _pack_holds(legacy + full_stranded, full_free)
    assert (full_shortfall is None) is verified
    free, stranded, deficient = _partition_assigned(physical, other)
    plan, _ = _pack_holds(legacy + stranded, free)
    plan = {farm: units for farm, units in plan.items() if farm not in deficient}

    _add_recovery_options(plan, own, physical, other, legacy, for_approval=approve)

    assert {farm for farm, units in plan.items() if units >= own_size} == {
        farms[index] for index in eligible
    }
    if verified and approve:
        for index in eligible:
            target = farms[index]
            after_stock = physical | {target: physical[target] - own_size}
            after_free, after_stranded, after_deficient = _partition_assigned(after_stock, other)
            assert not after_deficient - full_deficient
            assert _pack_holds(legacy + after_stranded, after_free)[1] is None


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
    assert capacity.recovery_shortfalls[product.pk] == (7, 6)
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
    assert product.pk not in capacity.recovery_shortfalls
    assert capacity.legacy_hold_counts[product.pk] == 5


def strand_supply(batch, monkeypatch):
    """Advance across the actual batch date boundary without expiring the order."""
    today = timezone.localdate()
    batch.start_date = today - timedelta(days=1)
    batch.end_date = today
    batch.save(update_fields=["start_date", "end_date"])
    tomorrow = timezone.now() + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: tomorrow)


def test_stranded_store_hold_keeps_recovery_stock_from_catalog_quote_and_checkout(
    store_product, payment_settings, active_user, client, monkeypatch
):
    from uuid import uuid4

    from commerce.store_orders import approve_store_order

    original = store_product.farm_offers.get().stock_batches.get()
    order, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(store_product, quantity=4)
    )
    backup = supply(store_product, "Recovery", 4)
    hold = order.lines.get().stock_reservations.get()
    assert hold.farm_id == original.farm_product.farm_id
    strand_supply(original, monkeypatch)
    type(order).objects.filter(pk=order.pk).update(expires_at=timezone.now() + timedelta(hours=12))

    payload = checkout_payload(store_product, quantity=4, expected_available=4)
    with pytest.raises(StoreOrderError, match="^store_requote_required$") as conflict:
        create_store_checkout(user=None, payload=payload)
    assert conflict.value.context["available_quantity"] == 0
    with pytest.raises(StoreOrderError, match="^store_stock_changed$") as quote:
        preview_store_voucher(user=None, payload=payload)
    assert quote.value.context["available_quantity"] == 0
    listing = client.get("/api/v1/public/store-products?locale=vi")
    detail = client.get(f"/api/v1/public/store-products/{store_product.slug}?locale=vi")
    assert listing.json()["data"][0]["available_quantity"] == 0
    assert detail.json()["data"]["available_quantity"] == 0

    hold.refresh_from_db()
    assert hold.farm_id == original.farm_product.farm_id
    assert hold.released_at is None
    assert type(order).objects.count() == 1
    approve_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid4(),
        sources=[{"line_id": hold.line_id, "farm_id": backup.pk}],
    )
    assert hold.line.stock_allocations.get().batch.farm_product.farm_id == backup.pk


@pytest.mark.parametrize("release", ["expiry", "cancel"])
def test_recovery_protection_ends_at_reservation_release(
    store_product, payment_settings, active_user, monkeypatch, release
):
    from uuid import uuid4

    from commerce.inventory import inventory_capacity
    from commerce.store_orders import cancel_store_order

    product = store_product
    original = product.farm_offers.get().stock_batches.get()
    order, _, _ = create_store_checkout(user=None, payload=checkout_payload(product, quantity=4))
    backup = supply(product, "Recovery", 4)
    strand_supply(original, monkeypatch)
    type(order).objects.filter(pk=order.pk).update(expires_at=timezone.now() + timedelta(hours=12))
    assert inventory_capacity([product.pk], now=timezone.now()).maximum(product.pk) == 0
    if release == "expiry":
        type(order).objects.filter(pk=order.pk).update(expires_at=timezone.now())
    else:
        cancel_store_order(
            order_id=order.pk, actor=active_user, idempotency_key=uuid4(), reason="Cancelled"
        )
    next_order, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(product, quantity=4, expected_available=4)
    )
    assert next_order.lines.get().stock_reservations.get().farm_id == backup.pk


def test_legacy_and_stranded_holds_are_counted_whole_in_preflight(store_product, payment_settings):
    import json
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from commerce.inventory import inventory_capacity

    product = store_product
    original = product.farm_offers.get().stock_batches.get()
    first, _, _ = create_store_checkout(user=None, payload=checkout_payload(product, quantity=4))
    second, _, _ = create_store_checkout(
        user=None, payload=checkout_payload(product, quantity=3, expected_available=6)
    )
    second.lines.get().stock_reservations.update(farm_id=None)
    original.end_date = timezone.localdate() - timedelta(days=1)
    original.save(update_fields=["end_date"])
    replacement = supply(product, "Replacement", 4)
    another = supply(product, "Another", 2)
    output = StringIO()
    with pytest.raises(CommandError, match="no verified single-Farm plan"):
        call_command("check_farm_fulfillment", stdout=output)
    assert '"stranded_holds": 1' in output.getvalue()
    assert '"unassigned_holds": 1' in output.getvalue()
    assert '"blocked": 1' in output.getvalue()
    blocked = json.loads(output.getvalue().splitlines()[0])
    assert blocked["unplaced_hold"] == [3, 2]
    assert blocked["remaining_capacity"] == 0
    assert inventory_capacity([product.pk], now=timezone.now()).maximum(product.pk) == 0

    another.products.get().stock_batches.update(quantity_units=3)
    output = StringIO()
    call_command("check_farm_fulfillment", stdout=output)
    assert '"stranded_holds": 1' in output.getvalue()
    assert '"blocked": 0' in output.getvalue()
    verified = json.loads(output.getvalue().splitlines()[0])
    assert verified["unplaced_hold"] is None
    assert verified["remaining_capacity"] == 0
    capacity = inventory_capacity([product.pk], now=timezone.now())
    assert capacity.remaining(product.pk, replacement.pk) == 0
    assert capacity.remaining(product.pk, another.pk) == 0
    assert first.lines.get().stock_reservations.get().farm_id == original.farm_product.farm_id
    assert second.lines.get().stock_reservations.get().farm_id is None
