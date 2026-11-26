from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("commerce", "0011_allow_address_without_recipient_details")]

    operations = [
        migrations.AddField(
            model_name="product",
            name="status",
            field=models.CharField(max_length=12, null=True),
        ),
    ]
