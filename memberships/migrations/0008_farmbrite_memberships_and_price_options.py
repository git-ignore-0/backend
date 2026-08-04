import uuid

import django.db.models.deletion
from django.db import migrations, models


def exact_monthly_price(*, total, duration, identity):
    total = int(total)
    duration = int(duration)
    if total % duration:
        raise RuntimeError(
            f"Cannot migrate {identity}: legacy total {total} is not exactly divisible "
            f"by duration_months {duration}. Correct the test data before retrying."
        )
    return total // duration


def backfill_price_options_and_memberships(apps, schema_editor):
    Membership = apps.get_model("memberships", "Membership")
    MembershipPackage = apps.get_model("memberships", "MembershipPackage")
    PriceOption = apps.get_model("memberships", "MembershipPackagePriceOption")

    for package in MembershipPackage.objects.all().order_by("id").iterator():
        total = int(package.upfront_price)
        duration = int(package.duration_months)
        monthly_price = exact_monthly_price(
            total=total,
            duration=duration,
            identity=f"MembershipPackage {package.pk}",
        )
        PriceOption.objects.create(
            package_id=package.pk,
            duration_months=duration,
            monthly_price_vnd=monthly_price,
            is_active=True,
            active_duration_key=duration,
            sort_order=0,
        )

    for membership in Membership.objects.all().order_by("id").iterator():
        total = int(membership.total_price_vnd_snapshot)
        duration = int(membership.duration_months_snapshot)
        monthly_price = exact_monthly_price(
            total=total,
            duration=duration,
            identity=f"Membership {membership.pk}",
        )
        reference = f"legacy-{membership.pk}"
        option = (
            PriceOption.objects.filter(
                package_id=membership.package_id,
                duration_months=duration,
                monthly_price_vnd=monthly_price,
            )
            .order_by("-is_active", "id")
            .first()
        )
        if option is None:
            option = PriceOption.objects.create(
                package_id=membership.package_id,
                duration_months=duration,
                monthly_price_vnd=monthly_price,
                is_active=False,
                active_duration_key=None,
                sort_order=0,
            )
        membership.price_option_id = option.pk
        membership.monthly_price_vnd_snapshot = monthly_price
        membership.farmbrite_order_reference = reference
        membership.farmbrite_order_reference_key = reference
        membership.save(
            update_fields=[
                "price_option",
                "monthly_price_vnd_snapshot",
                "farmbrite_order_reference",
                "farmbrite_order_reference_key",
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("memberships", "0007_membership_package_description_en_snapshot_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="MembershipPackagePriceOption",
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
                ("duration_months", models.PositiveSmallIntegerField()),
                ("monthly_price_vnd", models.PositiveBigIntegerField()),
                ("is_active", models.BooleanField(default=True)),
                (
                    "active_duration_key",
                    models.PositiveSmallIntegerField(editable=False, null=True),
                ),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "package",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="price_options",
                        to="memberships.membershippackage",
                    ),
                ),
            ],
            options={
                "ordering": ["sort_order", "duration_months", "id"],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("package", "active_duration_key"),
                        name="memberships_package_active_duration_unique",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("duration_months__gt", 0)),
                        name="memberships_package_option_duration_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("monthly_price_vnd__gt", 0)),
                        name="memberships_package_option_monthly_price_positive",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("active_duration_key", models.F("duration_months")),
                                ("is_active", True),
                            ),
                            models.Q(
                                ("active_duration_key__isnull", True),
                                ("is_active", False),
                            ),
                            _connector="OR",
                        ),
                        name="memberships_package_option_active_key_consistent",
                    ),
                ],
            },
        ),
        migrations.RenameField(
            model_name="membership",
            old_name="upfront_price_snapshot",
            new_name="total_price_vnd_snapshot",
        ),
        migrations.AddField(
            model_name="membership",
            name="price_option",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="memberships",
                to="memberships.membershippackagepriceoption",
            ),
        ),
        migrations.AddField(
            model_name="membership",
            name="monthly_price_vnd_snapshot",
            field=models.PositiveBigIntegerField(null=True),
        ),
        migrations.AddField(
            model_name="membership",
            name="farmbrite_order_reference",
            field=models.CharField(max_length=120, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="membership",
            name="farmbrite_order_reference_key",
            field=models.CharField(editable=False, max_length=240, null=True, unique=True),
        ),
        migrations.RunPython(
            backfill_price_options_and_memberships,
            migrations.RunPython.noop,
        ),
        migrations.RemoveField(
            model_name="membership",
            name="request",
        ),
        migrations.RemoveConstraint(
            model_name="membershippackage",
            name="memberships_package_price_positive",
        ),
        migrations.RemoveConstraint(
            model_name="membershippackage",
            name="memberships_package_duration_positive",
        ),
        migrations.RemoveField(
            model_name="membershippackage",
            name="upfront_price",
        ),
        migrations.RemoveField(
            model_name="membershippackage",
            name="duration_months",
        ),
        migrations.DeleteModel(name="MembershipRequestEvent"),
        migrations.DeleteModel(name="MembershipPaymentInstruction"),
        migrations.DeleteModel(name="MembershipRequestItemSnapshot"),
        migrations.DeleteModel(name="MembershipPaymentSettings"),
        migrations.DeleteModel(name="MembershipRequest"),
        migrations.AlterField(
            model_name="membership",
            name="total_price_vnd_snapshot",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AlterField(
            model_name="membership",
            name="price_option",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="memberships",
                to="memberships.membershippackagepriceoption",
            ),
        ),
        migrations.AlterField(
            model_name="membership",
            name="monthly_price_vnd_snapshot",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AlterField(
            model_name="membership",
            name="farmbrite_order_reference",
            field=models.CharField(max_length=120, unique=True),
        ),
        migrations.AlterField(
            model_name="membership",
            name="farmbrite_order_reference_key",
            field=models.CharField(editable=False, max_length=240, unique=True),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.CheckConstraint(
                condition=models.Q(("monthly_price_vnd_snapshot__gt", 0)),
                name="memberships_monthly_price_snapshot_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.CheckConstraint(
                condition=models.Q(("total_price_vnd_snapshot__gt", 0)),
                name="memberships_total_price_snapshot_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="membership",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "total_price_vnd_snapshot",
                        models.F("monthly_price_vnd_snapshot")
                        * models.F("duration_months_snapshot"),
                    )
                ),
                name="memberships_price_snapshot_total_exact",
            ),
        ),
    ]
