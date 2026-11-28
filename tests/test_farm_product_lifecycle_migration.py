from datetime import date, timedelta
from uuid import uuid4

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from commerce.models import StoreStockAllocation, StoreStockMovement
from content.models import ContentAuditLog
from farms import services
from farms.models import FarmProduct, FarmProductStockBatch
from tests.test_csa_rounds import context as context
from tests.test_farm_lifecycle_locking import allocated_selection as allocated_selection
from tests.test_farm_lifecycle_locking import cancel_native_order
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

LEGACY = ("farms", "0004_repair_missing_legacy_supply_table")
EXPAND = ("farms", "0005_farm_product_lifecycle_expand")
BACKFILL = ("farms", "0006_farm_product_lifecycle_backfill")
COMPLETE = ("farms", "0007_farm_product_lifecycle_constraints")
CLASSIFICATION = "farm.product.lifecycle_backfill"


@pytest.mark.django_db(transaction=True)
def test_lifecycle_cutover_and_reapply_preserve_archive_ledger_and_classification(
    context, allocated_selection, monkeypatch
):
    actor, product, _, _, now = context
    cancel_native_order(allocated_selection, actor)
    allocations = list(StoreStockAllocation.objects.values())
    movements = list(StoreStockMovement.objects.values())
    assert allocations and movements
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    legacy_targets = [node for node in latest if node[0] != "farms"] + [LEGACY]
    try:
        executor.migrate(legacy_targets)
        historical = executor.loader.project_state(legacy_targets).apps
        Source = historical.get_model("farms", "FarmProduct")
        Batch = historical.get_model("farms", "FarmProductStockBatch")
        Archive = historical.get_model("farms", "LegacyFarmWeeklySupply")
        Farm = historical.get_model("farms", "Farm")
        dormant_farm = Farm.objects.create(
            name_en="Dormant farm", name_vi="Trại nghỉ", is_active=False
        )
        active = Source.objects.create(farm_id=dormant_farm.pk, product_id=product.pk)
        disabled = Source.objects.exclude(pk=active.pk).get(product_id=product.pk)
        Source.objects.filter(pk=disabled.pk).update(is_active=False)
        first_day = date(2026, 1, 5)
        for offset, cancelled_at in [(0, None), (7, now)]:
            Batch.objects.create(
                farm_product=disabled,
                start_date=first_day + timedelta(days=offset),
                end_date=first_day + timedelta(days=offset + 6),
                quantity_units=7,
                cancelled_at=cancelled_at,
                version=2 if cancelled_at else 1,
            )
        Archive.objects.create(farm_product=disabled, week_start=first_day, quantity_units=0)
        original_batches = list(Batch.objects.order_by("pk").values())
        original_archive = list(Archive.objects.values())
        legacy_audit = ContentAuditLog.objects.create(
            action="farm.product.update",
            entity_type="farmproduct",
            entity_id=str(disabled.pk),
            actor=actor,
            context={"is_active": False},
        )

        MigrationExecutor(connection).migrate([EXPAND])
        assert FarmProduct.objects.values_list("removed_at", "supply_generation").get(
            pk=disabled.pk
        ) == (None, 1)
        MigrationExecutor(connection).migrate([BACKFILL])

        def assert_cutover_data():
            assert FarmProduct.objects.values_list(
                "is_active", "removed_at", "removed_by_id", "supply_generation", "version"
            ).get(pk=active.pk) == (True, None, None, 1, 1)
            removed = FarmProduct.objects.get(pk=disabled.pk)
            assert (removed.is_active, removed.removed_at, removed.removed_by_id) == (
                False,
                now,
                None,
            )
            assert (removed.supply_generation, removed.version) == (1, 1)
            assert removed.updated_at == disabled.updated_at
            assert list(FarmProductStockBatch.objects.order_by("pk").values()) == [
                {**batch, "supply_generation": 1} for batch in original_batches
            ]
            assert list(Archive.objects.values()) == original_archive
            assert list(StoreStockAllocation.objects.values()) == allocations
            assert list(StoreStockMovement.objects.values()) == movements
            assert ContentAuditLog.objects.get(pk=legacy_audit.pk).context == {"is_active": False}
            audit = ContentAuditLog.objects.get(action=CLASSIFICATION, entity_id=str(disabled.pk))
            assert audit.actor_id is None
            assert audit.context == {
                "farm_id": str(disabled.farm_id),
                "product_id": str(disabled.product_id),
                "supply_generation": 1,
                "classified_at": now.isoformat(),
                "provenance": "legacy_disabled_link",
            }
            return audit.pk

        audit_id = assert_cutover_data()
        MigrationExecutor(connection).migrate([COMPLETE])
        assert assert_cutover_data() == audit_id
        MigrationExecutor(connection).migrate(legacy_targets)
        monkeypatch.setattr(timezone, "now", lambda: now + timedelta(hours=2))
        MigrationExecutor(connection).migrate([COMPLETE])
        assert assert_cutover_data() == audit_id
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db
@pytest.mark.parametrize("values", [{"supply_generation": 0}, {"version": 0}, {"is_active": False}])
def test_farm_product_rejects_invalid_lifecycle_state(store_product, values):
    source = store_product.farm_offers.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProduct.objects.filter(pk=source.pk).update(**values)


@pytest.mark.django_db
def test_stock_batch_rejects_zero_supply_generation(store_product):
    with pytest.raises(IntegrityError), transaction.atomic():
        FarmProductStockBatch.objects.filter(farm_product__product=store_product).update(
            supply_generation=0
        )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("readded", [False, True])
def test_lifecycle_rollback_blocks_after_source_has_real_transitions(
    store_product, active_user, readded, settings
):
    settings.CONTENT_FRONTEND_REVALIDATE_URL = ""
    source = store_product.farm_offers.get()
    old_batch = source.stock_batches.get()
    intent = {
        "farm_id": source.farm_id,
        "product_id": source.product_id,
        "expected_link_id": source.pk,
        "actor": active_user,
    }
    removed = services.remove_farm_product(**intent, expected_version=source.version)
    if readded:
        services.readd_farm_product(
            **intent,
            expected_version=removed.version,
            idempotency_key=uuid4(),
        )
    original_audit = list(ContentAuditLog.objects.order_by("pk").values())
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        with pytest.raises(RuntimeError, match="roll forward|compatible backend"):
            executor.migrate([LEGACY])
        assert COMPLETE in MigrationExecutor(connection).loader.applied_migrations
        source.refresh_from_db()
        assert (source.version, source.supply_generation) == ((3, 2) if readded else (2, 1))
        assert source.is_active is readded
        old_batch.refresh_from_db()
        assert old_batch.supply_generation == 1
        assert list(ContentAuditLog.objects.order_by("pk").values()) == original_audit
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_lifecycle_cutover_rechecks_disabled_source_with_open_allocation(
    allocated_selection,
):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    executor.migrate([LEGACY])
    source = (
        allocated_selection.order.lines.get()
        .stock_allocations.values_list("batch__farm_product_id", flat=True)
        .get()
    )
    FarmProduct.objects.filter(pk=source).update(is_active=False)
    try:
        with pytest.raises(RuntimeError, match=str(source)):
            MigrationExecutor(connection).migrate([COMPLETE])
        executor = MigrationExecutor(connection)
        assert EXPAND in executor.loader.applied_migrations
        assert BACKFILL not in executor.loader.applied_migrations
        assert COMPLETE not in executor.loader.applied_migrations
        assert FarmProduct.objects.values_list("is_active", "removed_at").get(pk=source) == (
            False,
            None,
        )
        assert not ContentAuditLog.objects.filter(action=CLASSIFICATION).exists()
    finally:
        FarmProduct.objects.filter(pk=source).update(is_active=True)
        MigrationExecutor(connection).migrate(latest)
