from django.db import migrations


def create_legacy_supply_table_if_missing(apps, schema_editor):
    """Restore a missing archive schema; never infer history from current stock."""
    legacy_supply = apps.get_model("farms", "LegacyFarmWeeklySupply")
    if legacy_supply._meta.db_table not in schema_editor.connection.introspection.table_names():
        schema_editor.create_model(legacy_supply)


class Migration(migrations.Migration):
    dependencies = [
        ("farms", "0003_replace_weekly_supply_with_stock_batches"),
    ]

    operations = [
        migrations.RunPython(create_legacy_supply_table_if_missing, migrations.RunPython.noop),
    ]
