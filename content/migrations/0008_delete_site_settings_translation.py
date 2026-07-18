from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("content", "0007_remove_site_location_fields")]

    operations = [
        migrations.DeleteModel(
            name="SiteSettingsTranslation",
        ),
    ]
