from django.db import migrations


def add_tracker_profile_revision_if_missing(apps, schema_editor):
    Farm = apps.get_model("farms", "Farm")
    field = Farm._meta.get_field("tracker_profile_revision")
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, Farm._meta.db_table
            )
        }

    if field.column not in columns:
        schema_editor.add_field(Farm, field)


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0020_tracker_farm_canonical_farm"),
        ("farms", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(
            add_tracker_profile_revision_if_missing,
            migrations.RunPython.noop,
        ),
    ]
