import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0016_tracker_farm"),
    ]

    operations = [
        migrations.AddField(
            model_name="trackerfarm",
            name="description_en",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="trackerfarm",
            name="description_vi",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="trackerfarm",
            name="image",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tracker_farms",
                to="content.mediaasset",
            ),
        ),
    ]
