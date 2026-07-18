from django.db import migrations, models


def remove_google_maps_links(apps, schema_editor):
    external_link = apps.get_model("content", "ExternalLink")
    external_link.objects.filter(kind="google_maps").delete()


class Migration(migrations.Migration):
    dependencies = [("content", "0005_remove_sitesettings_phone_e164")]

    operations = [
        migrations.RunPython(remove_google_maps_links, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="externallink",
            name="kind",
            field=models.CharField(
                choices=[
                    ("facebook", "Facebook"),
                    ("youtube", "YouTube"),
                    ("store", "Store"),
                    ("forum", "Forum"),
                    ("other", "Other"),
                ],
                max_length=32,
            ),
        ),
    ]
