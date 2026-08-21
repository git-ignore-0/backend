import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0015_add_farms_external_link"),
    ]

    operations = [
        migrations.CreateModel(
            name="TrackerFarm",
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
                ("name_en", models.CharField(max_length=240)),
                ("name_vi", models.CharField(max_length=240)),
                ("location_en", models.CharField(max_length=240)),
                ("location_vi", models.CharField(max_length=240)),
                ("signup_count", models.PositiveIntegerField(default=0)),
                ("is_visible", models.BooleanField(default=True)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("version", models.PositiveIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["sort_order", "created_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["is_visible", "sort_order", "created_at"],
                        name="content_tracker_public_idx",
                    )
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("signup_count__gte", 0)),
                        name="content_tracker_signup_nonnegative",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("sort_order__gte", 0)),
                        name="content_tracker_sort_nonnegative",
                    ),
                ],
            },
        ),
    ]
