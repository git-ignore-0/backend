import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

from farms.migrations._lifecycle_rollback import validate_lifecycle_rollback


class Migration(migrations.Migration):
    dependencies = [
        ("commerce", "0015_product_status_legacy_flags_match"),
        ("farms", "0004_repair_missing_legacy_supply_table"),
        ("memberships", "0043_csa_customer_note"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="farmproduct",
            name="removed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="farmproduct",
            name="removed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="removed_farm_products",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="farmproduct",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="farmproduct",
            name="supply_generation",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="farmproductstockbatch",
            name="supply_generation",
            field=models.PositiveIntegerField(default=1, editable=False),
        ),
        migrations.RunPython(migrations.RunPython.noop, validate_lifecycle_rollback),
    ]
