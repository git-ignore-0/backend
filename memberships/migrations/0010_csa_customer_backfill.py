import re
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


PHONE_PATTERN = re.compile(r"[1-9][0-9]{8,9}")


def normalize_phone(value):
    raw = (value or "").strip()
    if raw.startswith("+84"):
        national = raw[3:]
    elif raw.startswith("84"):
        national = raw[2:]
    elif raw.startswith("0"):
        national = raw[1:]
    else:
        return None
    if not PHONE_PATTERN.fullmatch(national):
        return None
    return f"+84{national}"


def backfill_customers(apps, schema_editor):
    Customer = apps.get_model("memberships", "CSACustomer")
    Membership = apps.get_model("memberships", "Membership")
    UsageOrder = apps.get_model("memberships", "MembershipUsageOrder")
    MemberProfile = apps.get_model("members", "MemberProfile")
    User = apps.get_model("identity", "User")

    user_ids = list(
        Membership.objects.order_by("user_id")
        .values_list("user_id", flat=True)
        .distinct()
    )
    used_phones = set()
    customers = {}
    profiles = {
        profile.user_id: profile
        for profile in MemberProfile.objects.filter(user_id__in=user_ids).iterator()
    }
    for user in User.objects.filter(pk__in=user_ids).order_by("pk").iterator():
        profile = profiles.get(user.pk)
        phone = normalize_phone(profile.phone_number if profile else None)
        if phone in used_phones:
            phone = None
        if phone:
            used_phones.add(phone)
        customer = Customer.objects.create(
            user_id=user.pk,
            name=user.name,
            phone_e164=phone,
            province_code=profile.province_code if profile else "",
            ward_code=profile.ward_code if profile else "",
            address=profile.address if profile else "",
        )
        customers[user.pk] = customer.pk

    for user_id, customer_id in customers.items():
        Membership.objects.filter(user_id=user_id).update(customer_id=customer_id)
    for usage in UsageOrder.objects.select_related("membership").iterator():
        usage.customer_id = usage.membership.customer_id
        usage.save(update_fields=["customer"])


class Migration(migrations.Migration):
    dependencies = [
        ("members", "0002_member_profile_administrative_codes"),
        ("memberships", "0009_revoke_membership_fields"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CSACustomer",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("name", models.CharField(max_length=150)),
                (
                    "phone_e164",
                    models.CharField(blank=True, max_length=13, null=True, unique=True),
                ),
                ("province_code", models.CharField(blank=True, max_length=2)),
                ("ward_code", models.CharField(blank=True, max_length=5)),
                ("address", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="csa_customer",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["name", "id"]},
        ),
        migrations.AddField(
            model_name="membership",
            name="customer",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="memberships",
                to="memberships.csacustomer",
            ),
        ),
        migrations.AddField(
            model_name="membershipusageorder",
            name="customer",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="usage_orders",
                to="memberships.csacustomer",
            ),
        ),
        migrations.RunPython(backfill_customers, migrations.RunPython.noop),
    ]
