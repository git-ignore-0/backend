from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("content", "0003_site_settings_visibility")]

    operations = [
        migrations.AlterField(
            model_name="externallink",
            name="url",
            field=models.URLField(blank=True, max_length=500),
        ),
    ]
