import uuid
from datetime import timedelta

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def migrate_positive_weekly_supply(apps, schema_editor):
    LegacySupply = apps.get_model("farms", "LegacyFarmWeeklySupply")
    StockBatch = apps.get_model("farms", "FarmProductStockBatch")
    batches = []
    original_timestamps = {}
    for supply in LegacySupply.objects.filter(quantity_units__gt=0).iterator():
        batches.append(
            StockBatch(
                id=supply.pk,
                farm_product_id=supply.farm_product_id,
                start_date=supply.week_start,
                end_date=supply.week_start + timedelta(days=6),
                quantity_units=supply.quantity_units,
            )
        )
        original_timestamps[supply.pk] = (supply.created_at, supply.updated_at)
    StockBatch.objects.bulk_create(batches)
    if batches:
        for batch in batches:
            batch.created_at, batch.updated_at = original_timestamps[batch.pk]
        StockBatch.objects.bulk_update(batches, ["created_at", "updated_at"])


def remove_migrated_weekly_supply(apps, schema_editor):
    LegacySupply = apps.get_model("farms", "LegacyFarmWeeklySupply")
    StockBatch = apps.get_model("farms", "FarmProductStockBatch")
    StockBatch.objects.filter(
        pk__in=LegacySupply.objects.filter(quantity_units__gt=0).values("pk")
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("farms", "0002_repair_tracker_profile_revision"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RenameModel(
            old_name="FarmWeeklySupply",
            new_name="LegacyFarmWeeklySupply",
        ),
        migrations.CreateModel(
            name="FarmProductStockBatch",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("quantity_units", models.PositiveIntegerField()),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "cancelled_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="cancelled_farm_product_stock_batches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "farm_product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="stock_batches",
                        to="farms.farmproduct",
                    ),
                ),
            ],
            options={
                "ordering": ["-start_date", "-created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["farm_product", "cancelled_at", "start_date", "end_date"],
                        name="farms_batch_availability_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("end_date__gte", models.F("start_date"))),
                        name="farms_batch_dates_ordered",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("quantity_units__gt", 0)),
                        name="farms_batch_quantity_positive",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            migrate_positive_weekly_supply,
            remove_migrated_weekly_supply,
        ),
    ]
