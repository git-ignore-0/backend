import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("commerce", "0002_catalogtreelock"),
        ("content", "0019_tracker_farm_one_month_signup_count"),
    ]

    operations = [
        migrations.CreateModel(
            name="Farm",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("name_en", models.CharField(max_length=240)),
                ("name_vi", models.CharField(max_length=240)),
                ("location_en", models.CharField(max_length=240)),
                ("location_vi", models.CharField(max_length=240)),
                ("description_en", models.TextField(blank=True, default="")),
                ("description_vi", models.TextField(blank=True, default="")),
                ("is_active", models.BooleanField(default=True)),
                ("legacy_tracker_owned", models.BooleanField(default=False)),
                ("tracker_profile_revision", models.PositiveIntegerField(default=0)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("image", models.ForeignKey("content.MediaAsset", null=True, blank=True, related_name="farms", on_delete=django.db.models.deletion.PROTECT)),
            ],
            options={"ordering": ["name_en", "id"]},
        ),
        migrations.CreateModel(
            name="FarmProduct",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("farm", models.ForeignKey("farms.Farm", related_name="products", on_delete=django.db.models.deletion.PROTECT)),
                ("product", models.ForeignKey("commerce.Product", related_name="farm_offers", on_delete=django.db.models.deletion.PROTECT)),
            ],
            options={"ordering": ["product__sku", "id"]},
        ),
        migrations.CreateModel(
            name="FarmWeeklySupply",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("week_start", models.DateField()),
                ("quantity_units", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("farm_product", models.ForeignKey("farms.FarmProduct", related_name="weekly_supplies", on_delete=django.db.models.deletion.PROTECT)),
            ],
            options={"ordering": ["-week_start", "id"]},
        ),
        migrations.CreateModel(
            name="FarmPhoto",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("sort_order", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("farm", models.ForeignKey("farms.Farm", related_name="photos", on_delete=django.db.models.deletion.CASCADE)),
                ("image", models.ForeignKey("content.MediaAsset", related_name="farm_photos", on_delete=django.db.models.deletion.PROTECT)),
            ],
            options={
                "ordering": ["sort_order", "created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(fields=("farm", "sort_order"), name="farms_photo_order_unique"),
                    models.UniqueConstraint(fields=("farm", "image"), name="farms_photo_image_unique"),
                ],
            },
        ),
        migrations.AddConstraint("farmproduct", models.UniqueConstraint(fields=("farm", "product"), name="farms_product_unique")),
        migrations.AddConstraint("farmweeklysupply", models.UniqueConstraint(fields=("farm_product", "week_start"), name="farms_weekly_supply_unique")),
        migrations.AddConstraint("farmweeklysupply", models.CheckConstraint(condition=models.Q(quantity_units__gte=0), name="farms_weekly_supply_nonnegative")),
    ]
