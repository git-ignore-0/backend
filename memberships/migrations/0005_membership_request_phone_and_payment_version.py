from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations, models


def populate_request_phone_snapshots(apps, schema_editor):
    MembershipRequest = apps.get_model("memberships", "MembershipRequest")
    for item in MembershipRequest.objects.select_related("user").iterator():
        phone = ""
        try:
            phone = item.payment_instruction.phone_snapshot
        except ObjectDoesNotExist:
            try:
                phone = item.user.member_profile.phone_number
            except ObjectDoesNotExist:
                pass
        item.phone_snapshot = phone.strip()
        item.save(update_fields=["phone_snapshot"])


class Migration(migrations.Migration):
    dependencies = [("memberships", "0004_remove_product_category")]

    operations = [
        migrations.AddField(
            model_name="membershippaymentsettings",
            name="version",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="membershiprequest",
            name="phone_snapshot",
            field=models.CharField(default="", max_length=32),
            preserve_default=False,
        ),
        migrations.RunPython(populate_request_phone_snapshots, migrations.RunPython.noop),
    ]
