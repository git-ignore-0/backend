"""Repair a missing archive without changing stock or existing weekly history."""

from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from farms.models import Farm, FarmProduct, FarmProductStockBatch, LegacyFarmWeeklySupply
from tests.test_store_orders import store_product as store_product


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("missing", [True, False])
def test_legacy_archive_repair_preserves_current_stock_and_existing_history(store_product, missing):
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    parent = ("farms", "0003_replace_weekly_supply_with_stock_batches")
    repair = ("farms", "0004_repair_missing_legacy_supply_table")
    try:
        executor.migrate([parent])
        offer = FarmProduct.objects.get(product=store_product)
        legacy = LegacyFarmWeeklySupply.objects.create(
            farm_product=offer,
            week_start=date(2026, 10, 26),
            quantity_units=7,
        )
        original_stock = list(FarmProductStockBatch.objects.values().order_by("id"))
        original_farms = list(Farm.objects.values().order_by("id"))
        if missing:
            with connection.schema_editor() as editor:
                editor.delete_model(LegacyFarmWeeklySupply)
        executor = MigrationExecutor(connection)
        executor.migrate([repair])
        assert LegacyFarmWeeklySupply._meta.db_table in connection.introspection.table_names()
        assert list(FarmProductStockBatch.objects.values().order_by("id")) == original_stock
        assert list(Farm.objects.values().order_by("id")) == original_farms
        if missing:
            assert LegacyFarmWeeklySupply.objects.count() == 0
        else:
            assert LegacyFarmWeeklySupply.objects.get(pk=legacy.pk).quantity_units == 7
        # Reapplying the repair must preserve a row in the now-existing archive.
        preserved = LegacyFarmWeeklySupply.objects.first() or LegacyFarmWeeklySupply.objects.create(
            farm_product=offer,
            week_start=date(2026, 10, 26),
            quantity_units=9,
        )
        executor = MigrationExecutor(connection)
        executor.migrate([parent])
        executor = MigrationExecutor(connection)
        executor.migrate([repair])
        assert (
            LegacyFarmWeeklySupply.objects.get(pk=preserved.pk).quantity_units
            == preserved.quantity_units
        )
    finally:
        MigrationExecutor(connection).migrate(latest)
