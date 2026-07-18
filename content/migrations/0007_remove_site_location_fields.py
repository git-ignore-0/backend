from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("content", "0006_remove_google_maps_external_link")]

    operations = [
        migrations.RemoveField(
            model_name="sitesettings",
            name="is_location_enabled",
        ),
        migrations.RemoveField(
            model_name="sitesettingstranslation",
            name="address",
        ),
        migrations.RemoveField(
            model_name="sitesettingstranslation",
            name="location_name",
        ),
    ]
