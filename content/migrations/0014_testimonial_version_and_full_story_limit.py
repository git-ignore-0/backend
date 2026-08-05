from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0013_testimonial_testimonialtranslation_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="testimonial",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterField(
            model_name="testimonialtranslation",
            name="full_story",
            field=models.TextField(blank=True, max_length=20_000),
        ),
    ]
