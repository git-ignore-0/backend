from django.db import migrations, models


def add_disabled_farms_link(apps, schema_editor):
    ExternalLink = apps.get_model("content", "ExternalLink")
    SiteSettings = apps.get_model("content", "SiteSettings")
    settings, _created = SiteSettings.objects.get_or_create(key="default")
    ExternalLink.objects.get_or_create(
        settings=settings,
        kind="farms",
        defaults={
            "label": "See our farms",
            "url": "",
            "position": 50,
            "is_enabled": False,
        },
    )


def remove_disabled_farms_link(apps, schema_editor):
    ExternalLink = apps.get_model("content", "ExternalLink")
    ExternalLink.objects.filter(kind="farms", url="", is_enabled=False).delete()


class Migration(migrations.Migration):
    dependencies = [("content", "0014_testimonial_version_and_full_story_limit")]

    operations = [
        migrations.AlterField(
            model_name="externallink",
            name="kind",
            field=models.CharField(
                choices=[
                    ("facebook", "Facebook"),
                    ("youtube", "YouTube"),
                    ("store", "Store"),
                    ("forum", "Forum"),
                    ("farms", "See our farms"),
                    ("other", "Other"),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(add_disabled_farms_link, remove_disabled_farms_link),
    ]
