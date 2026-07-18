from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("content", "0002_seed_site_settings")]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="is_email_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="is_location_enabled",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="sitesettings",
            name="is_phone_enabled",
            field=models.BooleanField(default=True),
        ),
    ]
