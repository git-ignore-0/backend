from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0018_tracker_farm_photos"),
    ]

    operations = [
        migrations.AddField(
            model_name="trackerfarm",
            name="one_month_signup_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddConstraint(
            model_name="trackerfarm",
            constraint=models.CheckConstraint(
                condition=models.Q(("one_month_signup_count__gte", 0)),
                name="content_tracker_month_signup_nonneg",
            ),
        ),
    ]
