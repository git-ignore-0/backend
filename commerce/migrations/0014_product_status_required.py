from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("commerce", "0013_product_status_backfill")]

    operations = [
        migrations.AlterField(
            model_name="product",
            name="status",
            field=models.CharField(
                choices=[
                    ("draft", "Draft"),
                    ("active", "Active"),
                    ("discontinued", "Discontinued"),
                ],
                default="draft",
                max_length=12,
            ),
        ),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=["draft", "active", "discontinued"]),
                name="commerce_product_status_valid",
            ),
        ),
    ]
