import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_tracker_farm_photos(apps, schema_editor):
    TrackerFarm = apps.get_model("content", "TrackerFarm")
    TrackerFarmPhoto = apps.get_model("content", "TrackerFarmPhoto")

    for farm in TrackerFarm.objects.exclude(image_id=None).iterator():
        if TrackerFarmPhoto.objects.filter(
            farm_id=farm.pk,
            image_id=farm.image_id,
        ).exists():
            continue
        used_sort_orders = set(
            TrackerFarmPhoto.objects.filter(farm_id=farm.pk).values_list(
                "sort_order",
                flat=True,
            )
        )
        sort_order = 0
        while sort_order in used_sort_orders:
            sort_order += 1
        TrackerFarmPhoto.objects.create(
            farm_id=farm.pk,
            image_id=farm.image_id,
            sort_order=sort_order,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0017_tracker_farm_content"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrackerFarmPhoto",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("sort_order", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "farm",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="photos",
                        to="content.trackerfarm",
                    ),
                ),
                (
                    "image",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="tracker_farm_photos",
                        to="content.mediaasset",
                    ),
                ),
            ],
            options={
                "ordering": ["sort_order", "created_at", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("farm", "sort_order"),
                        name="content_tracker_photo_order_unique",
                    ),
                    models.UniqueConstraint(
                        fields=("farm", "image"),
                        name="content_tracker_photo_image_unique",
                    ),
                ],
            },
        ),
        migrations.RunPython(
            backfill_tracker_farm_photos,
            migrations.RunPython.noop,
        ),
    ]
