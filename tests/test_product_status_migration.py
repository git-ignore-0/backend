from datetime import date

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from commerce.models import CatalogTreeLock, Category, Product
from farms.models import LegacyFarmWeeklySupply
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import approve_review_order, create_review_quota_checkout
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area
from tests.test_store_orders import zero_shipping_fee as zero_shipping_fee


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


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("status", "flags", "drift"),
    [
        ("draft", (True, False), {"is_active": False}),
        ("active", (True, True), {"is_published": False}),
        ("discontinued", (False, False), {"is_active": True}),
        ("draft", (True, False), {"status": "invalid"}),
    ],
)
def test_product_rejects_invalid_status_or_drift_between_status_and_legacy_flags(
    status, flags, drift
):
    product = Product.objects.create(
        category=Category.objects.create(slug="status-check"),
        sku="STATUS-CHECK",
        slug="status-check",
        unit_size=1,
        unit_label_vi="kg",
        unit_label_en="kg",
        retail_price_vnd=1000,
        status=status,
        is_active=flags[0],
        is_published=flags[1],
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Product.objects.filter(pk=product.pk).update(**drift)
    product.refresh_from_db()
    assert (product.status, product.is_active, product.is_published) == (status, *flags)


@pytest.mark.django_db(transaction=True)
def test_status_constraint_migration_reports_mismatches_without_rewriting_rows():
    before = ("commerce", "0014_product_status_required")
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    row = None
    try:
        executor.migrate([before])
        apps = executor.loader.project_state([before]).apps
        historical = apps.get_model("commerce", "Product")
        row = historical.objects.create(
            category=apps.get_model("commerce", "Category").objects.create(slug="status-drift"),
            sku="STATUS-DRIFT",
            slug="status-drift",
            unit_size=1,
            unit_label_vi="kg",
            unit_label_en="kg",
            retail_price_vnd=1000,
            status="active",
            is_active=True,
            is_published=False,
        )
        with pytest.raises(RuntimeError, match=str(row.pk)):
            MigrationExecutor(connection).migrate(latest)
        row.refresh_from_db()
        assert (row.status, row.is_active, row.is_published) == ("active", True, False)
    finally:
        if row is not None:
            historical.objects.filter(pk=row.pk).update(is_published=True)
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_status_schema_roundtrip_preserves_order_and_csa_history(
    context, zero_shipping_fee, access_token
):
    actor, product, _, _, _ = context
    CatalogTreeLock.objects.get_or_create(pk=1)
    checkout = create_review_quota_checkout(actor, access_token, product)
    approve_review_order(checkout, actor)
    save(context, quantity=2)
    LegacyFarmWeeklySupply.objects.create(
        farm_product=product.farm_offers.get(), week_start=date(2026, 1, 5), quantity_units=9
    )

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    models = (
        ("commerce", "StoreOrder"),
        ("commerce", "StoreOrderLine"),
        ("commerce", "StoreStockReservation"),
        ("commerce", "StoreQuotaReservation"),
        ("commerce", "StoreStockAllocation"),
        ("commerce", "StoreStockMovement"),
        ("farms", "FarmProductStockBatch"),
        ("farms", "LegacyFarmWeeklySupply"),
        ("memberships", "CSASelection"),
        ("memberships", "CSASelectionLine"),
        ("memberships", "CSAStockHold"),
        ("memberships", "CSAQuotaHold"),
        ("memberships", "CSASelectionEvent"),
    )

    def history():
        apps = MigrationExecutor(connection).loader.project_state(latest).apps
        return {
            f"{app}.{model}": list(apps.get_model(app, model).objects.order_by("pk").values())
            for app, model in models
        }

    original = history()
    assert all(original.values())
    store_holds = "commerce.StoreStockReservation"
    assert all(row["farm_id"] is not None for row in original[store_holds])
    try:
        executor.migrate([("commerce", "0012_product_status_expand")])
        MigrationExecutor(connection).migrate(latest)
        product.refresh_from_db()
        assert (product.sku, product.status, product.is_active, product.is_published) == (
            "TOMATO",
            "active",
            True,
            True,
        )
        # Rolling Commerce back past its assignment migration drops only this
        # column. Reapplying must preserve the historical hold and ledger rows.
        expected = {
            **original,
            store_holds: [{**row, "farm_id": None} for row in original[store_holds]],
        }
        assert history() == expected
    finally:
        MigrationExecutor(connection).migrate(latest)
