from django.db import migrations, models
from django.db.models import Q

from farms.migrations._lifecycle_rollback import validate_lifecycle_rollback


class Migration(migrations.Migration):
    dependencies = [("farms", "0006_farm_product_lifecycle_backfill")]

    operations = [
        migrations.AddConstraint(
            model_name="farmproduct",
            constraint=models.CheckConstraint(
                condition=(
                    Q(is_active=True, removed_at__isnull=True, removed_by__isnull=True)
                    | Q(is_active=False, removed_at__isnull=False)
                ),
                name="farms_product_lifecycle_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="farmproduct",
            constraint=models.CheckConstraint(
                condition=Q(version__gt=0), name="farms_product_version_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="farmproduct",
            constraint=models.CheckConstraint(
                condition=Q(supply_generation__gt=0), name="farms_product_generation_positive"
            ),
        ),
        migrations.AddConstraint(
            model_name="farmproductstockbatch",
            constraint=models.CheckConstraint(
                condition=Q(supply_generation__gt=0), name="farms_batch_generation_positive"
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, validate_lifecycle_rollback),
    ]
