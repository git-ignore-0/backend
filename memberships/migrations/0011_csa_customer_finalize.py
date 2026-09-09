import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("memberships", "0010_csa_customer_backfill")]

    operations = [
        migrations.RemoveConstraint(
            model_name="membership", name="memberships_one_current_per_user"
        ),
        migrations.RemoveIndex(
            model_name="membershipusageorder",
            name="memberships_user_id_6c27b3_idx",
        ),
        migrations.AlterField(
            model_name="membership",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="memberships",
                to="memberships.csacustomer",
            ),
        ),
        migrations.AlterField(
            model_name="membershipusageorder",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="usage_orders",
                to="memberships.csacustomer",
            ),
        ),
        migrations.RemoveField(model_name="membership", name="user"),
        migrations.RemoveField(model_name="membershipusageorder", name="user"),
        migrations.AddIndex(
            model_name="membershipusageorder",
            index=models.Index(
                fields=["customer", "-created_at"],
                name="memberships_custome_259d19_idx",
            ),
        ),
    ]
