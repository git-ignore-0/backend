from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("content", "0004_alter_externallink_url")]

    operations = [
        migrations.RemoveField(
            model_name="sitesettings",
            name="phone_e164",
        ),
    ]
