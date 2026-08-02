from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("memberships", "0003_remove_membershippaymentsettings_bank_code_and_more"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="product",
            name="category",
        ),
        migrations.DeleteModel(
            name="ProductCategory",
        ),
    ]
