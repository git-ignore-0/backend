from datetime import date

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


@pytest.mark.django_db(transaction=True)
def test_product_status_backfill_preserves_legacy_draft_stock_and_projects_old_flags():
    before = ("commerce", "0012_product_status_expand")
    after = ("commerce", "0014_product_status_required")
    farms_node = ("farms", "0004_repair_missing_legacy_supply_table")
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([before])
        apps = executor.loader.project_state([before, farms_node]).apps
        product_model = apps.get_model("commerce", "Product")
        category = apps.get_model("commerce", "Category").objects.create(slug="migration")
        farm = apps.get_model("farms", "Farm").objects.create(name_vi="Farm", name_en="Farm")
        states = {
            "draft": (True, False),
            "active": (True, True),
            "discontinued": (False, False),
        }
        products = {}
        for key, flags in states.items():
            products[key] = product_model.objects.create(
                category_id=category.pk,
                sku=key,
                slug=key,
                unit_size=1,
                unit_label_vi="kg",
                unit_label_en="kg",
                retail_price_vnd=1000,
                is_active=flags[0],
                is_published=flags[1],
            )
        offer = apps.get_model("farms", "FarmProduct").objects.create(
            farm_id=farm.pk, product_id=products["draft"].pk
        )
        stock = apps.get_model("farms", "FarmProductStockBatch")
        legacy = apps.get_model("farms", "LegacyFarmWeeklySupply")
        stock.objects.create(
            farm_product_id=offer.pk,
            start_date=date(2026, 1, 5),
            end_date=date(2026, 1, 11),
            quantity_units=9,
        )
        legacy.objects.create(
            farm_product_id=offer.pk, week_start=date(2026, 1, 5), quantity_units=9
        )
        original_stock = list(stock.objects.values())
        original_legacy = list(legacy.objects.values())

        if connection.vendor == "sqlite":
            # Reproduce a database imported without the old check constraint.
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA ignore_check_constraints = ON")
            try:
                product_model.objects.filter(pk=products["draft"].pk).update(
                    is_active=False, is_published=True
                )
                with pytest.raises(RuntimeError, match=str(products["draft"].pk)):
                    MigrationExecutor(connection).migrate([after])
                assert not product_model.objects.filter(status__isnull=False).exists()
            finally:
                product_model.objects.filter(pk=products["draft"].pk).update(
                    is_active=True, is_published=False
                )
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA ignore_check_constraints = OFF")

        executor = MigrationExecutor(connection)
        executor.migrate([after])
        apps = executor.loader.project_state([after, farms_node]).apps
        migrated = apps.get_model("commerce", "Product")
        for key, original in products.items():
            row = migrated.objects.get(pk=original.pk)
            assert (row.status, row.sku) == (key, original.sku)
            assert (row.is_active, row.is_published) == states[key]
        assert (
            list(apps.get_model("farms", "FarmProductStockBatch").objects.values())
            == original_stock
        )
        assert (
            list(apps.get_model("farms", "LegacyFarmWeeklySupply").objects.values())
            == original_legacy
        )
    finally:
        MigrationExecutor(connection).migrate(latest)
