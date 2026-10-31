from datetime import date, timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def table_columns(table_name):
    with connection.cursor() as cursor:
        return {
            column.name
            for column in connection.introspection.get_table_description(cursor, table_name)
        }


@pytest.mark.django_db(transaction=True)
def test_existing_tracker_farm_moves_to_canonical_farm_without_losing_gallery():
    old_node = ("content", "0019_tracker_farm_one_month_signup_count")
    new_node = ("content", "0020_tracker_farm_canonical_farm")
    executor = MigrationExecutor(connection)
    latest_nodes = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([old_node])
        old_apps = executor.loader.project_state([old_node]).apps
        MediaAsset = old_apps.get_model("content", "MediaAsset")
        TrackerFarm = old_apps.get_model("content", "TrackerFarm")
        TrackerFarmPhoto = old_apps.get_model("content", "TrackerFarmPhoto")
        first = MediaAsset.objects.create(
            file="farms/first.webp", variants=[], width=100, height=100, original_name="first"
        )
        second = MediaAsset.objects.create(
            file="farms/second.webp", variants=[], width=100, height=100, original_name="second"
        )
        farm_id = uuid4()
        TrackerFarm.objects.create(
            id=farm_id,
            name_en="Old name",
            name_vi="Tên cũ",
            location_en="Old place",
            location_vi="Địa điểm cũ",
            description_en="Old description",
            description_vi="Mô tả cũ",
            image_id=first.pk,
            signup_count=14,
            one_month_signup_count=4,
            sort_order=3,
            is_visible=False,
            version=7,
        )
        TrackerFarmPhoto.objects.create(farm_id=farm_id, image_id=first.pk, sort_order=0)
        TrackerFarmPhoto.objects.create(farm_id=farm_id, image_id=second.pk, sort_order=1)

        executor = MigrationExecutor(connection)
        executor.migrate([new_node])
        new_apps = executor.loader.project_state([new_node]).apps
        Farm = new_apps.get_model("farms", "Farm")
        FarmPhoto = new_apps.get_model("farms", "FarmPhoto")
        Profile = new_apps.get_model("content", "TrackerFarm")
        farm = Farm.objects.get(pk=farm_id)
        profile = Profile.objects.get(pk=farm_id)
        assert profile.farm_id == farm_id
        assert (farm.name_en, farm.name_vi, farm.location_en, farm.location_vi) == (
            "Old name",
            "Tên cũ",
            "Old place",
            "Địa điểm cũ",
        )
        assert (farm.description_en, farm.description_vi, farm.image_id) == (
            "Old description",
            "Mô tả cũ",
            first.pk,
        )
        assert list(
            FarmPhoto.objects.filter(farm_id=farm_id).values_list("image_id", "sort_order")
        ) == [(first.pk, 0), (second.pk, 1)]
        assert (
            profile.signup_count,
            profile.one_month_signup_count,
            profile.sort_order,
            profile.version,
        ) == (14, 4, 3, 7)
        assert profile.is_visible is False
        assert farm.tracker_profile_revision == profile.version
        independent_id = uuid4()
        Farm.objects.create(
            id=independent_id,
            name_en="Independent",
            name_vi="Độc lập",
            location_en="Place",
            location_vi="Địa điểm",
            image_id=second.pk,
        )
        FarmPhoto.objects.create(farm_id=independent_id, image_id=second.pk, sort_order=0)
        Profile.objects.create(
            id=independent_id,
            farm_id=independent_id,
            name_en="Independent",
            name_vi="Độc lập",
            location_en="Place",
            location_vi="Địa điểm",
            image_id=second.pk,
            sort_order=4,
        )
        TrackerPhoto = new_apps.get_model("content", "TrackerFarmPhoto")
        TrackerPhoto.objects.create(farm_id=independent_id, image_id=second.pk, sort_order=0)
        Farm.objects.filter(pk=farm_id).update(tracker_profile_revision=10)
        executor = MigrationExecutor(connection)
        executor.migrate([old_node])
        executor = MigrationExecutor(connection)
        executor.migrate([new_node])
        reapplied_apps = executor.loader.project_state([new_node]).apps
        ReappliedFarm = reapplied_apps.get_model("farms", "Farm")
        ReappliedPhoto = reapplied_apps.get_model("farms", "FarmPhoto")
        assert ReappliedFarm.objects.filter(pk=farm_id).count() == 1
        assert ReappliedFarm.objects.get(pk=farm_id).tracker_profile_revision == 10
        assert ReappliedPhoto.objects.filter(farm_id=farm_id).count() == 2
        assert ReappliedFarm.objects.filter(pk=independent_id).count() == 1
        assert ReappliedPhoto.objects.filter(farm_id=independent_id).count() == 1
    finally:
        MigrationExecutor(connection).migrate(latest_nodes)


@pytest.mark.django_db(transaction=True)
def test_repair_migration_adds_missing_tracker_revision_without_losing_farm_data():
    before_repair = ("content", "0020_tracker_farm_canonical_farm")
    repair = ("farms", "0002_repair_tracker_profile_revision")
    executor = MigrationExecutor(connection)
    latest_nodes = executor.loader.graph.leaf_nodes()
    try:
        before_targets = [before_repair, ("farms", "0001_initial")]
        executor.migrate(before_targets)
        before_apps = executor.loader.project_state(before_targets).apps
        Farm = before_apps.get_model("farms", "Farm")
        farm_id = uuid4()
        Farm.objects.create(
            id=farm_id,
            name_en="Repair me",
            name_vi="Sửa schema",
            location_en="Vietnam",
            location_vi="Việt Nam",
            tracker_profile_revision=9,
        )

        revision_field = Farm._meta.get_field("tracker_profile_revision")
        with connection.schema_editor() as schema_editor:
            schema_editor.remove_field(Farm, revision_field)
        assert "tracker_profile_revision" not in table_columns(Farm._meta.db_table)

        executor = MigrationExecutor(connection)
        executor.migrate([repair])
        repaired_apps = executor.loader.project_state([repair]).apps
        RepairedFarm = repaired_apps.get_model("farms", "Farm")
        repaired = RepairedFarm.objects.get(pk=farm_id)

        assert "tracker_profile_revision" in table_columns(RepairedFarm._meta.db_table)
        assert repaired.name_en == "Repair me"
        assert repaired.tracker_profile_revision == 0
    finally:
        MigrationExecutor(connection).migrate(latest_nodes)


@pytest.mark.django_db(transaction=True)
def test_stock_batch_migration_preserves_weekly_history_and_converts_positive_supply():
    migrate_from = ("farms", "0002_repair_tracker_profile_revision")
    migrate_to = ("farms", "0003_replace_weekly_supply_with_stock_batches")
    executor = MigrationExecutor(connection)
    latest_nodes = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([migrate_from])
        old_apps = executor.loader.project_state([migrate_from]).apps
        Category = old_apps.get_model("commerce", "Category")
        Product = old_apps.get_model("commerce", "Product")
        Farm = old_apps.get_model("farms", "Farm")
        FarmProduct = old_apps.get_model("farms", "FarmProduct")
        WeeklySupply = old_apps.get_model("farms", "FarmWeeklySupply")

        category = Category.objects.create(slug="migration-vegetables", is_active=True)
        product = Product.objects.create(
            category=category,
            sku="MIGRATION-TOMATO",
            slug="migration-tomato",
            unit_size="1.000",
            unit_label_vi="kg",
            unit_label_en="kg",
            retail_price_vnd=25_000,
        )
        farm = Farm.objects.create(
            name_en="Migration Farm",
            name_vi="Farm migration",
            location_en="Da Lat",
            location_vi="Đà Lạt",
        )
        relation = FarmProduct.objects.create(farm=farm, product=product)
        positive_id = uuid4()
        zero_id = uuid4()
        week_start = date(2026, 10, 19)
        positive = WeeklySupply.objects.create(
            id=positive_id,
            farm_product=relation,
            week_start=week_start,
            quantity_units=12,
        )
        WeeklySupply.objects.create(
            id=zero_id,
            farm_product=relation,
            week_start=week_start + timedelta(days=7),
            quantity_units=0,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([migrate_to])
        migrated_apps = executor.loader.project_state([migrate_to]).apps
        LegacySupply = migrated_apps.get_model("farms", "LegacyFarmWeeklySupply")
        StockBatch = migrated_apps.get_model("farms", "FarmProductStockBatch")

        assert set(LegacySupply.objects.values_list("id", flat=True)) == {
            positive_id,
            zero_id,
        }
        batch = StockBatch.objects.get(pk=positive_id)
        assert batch.farm_product_id == relation.pk
        assert batch.start_date == week_start
        assert batch.end_date == week_start + timedelta(days=6)
        assert batch.quantity_units == 12
        assert batch.created_at == positive.created_at
        assert batch.updated_at == positive.updated_at
        assert not StockBatch.objects.filter(pk=zero_id).exists()
    finally:
        MigrationExecutor(connection).migrate(latest_nodes)
