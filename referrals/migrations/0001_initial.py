import hashlib
import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.models import F, Q

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def code_for(user_id, salt=0):
    digest = hashlib.sha256(f"{user_id}:{salt}".encode()).digest()
    return "".join(ALPHABET[value % len(ALPHABET)] for value in digest[:10])


def backfill_referral_accounts(apps, schema_editor):
    User = apps.get_model("identity", "User")
    ReferralAccount = apps.get_model("referrals", "ReferralAccount")
    used = set(ReferralAccount.objects.values_list("code", flat=True))
    pending = []
    for user_id in User.objects.order_by("id").values_list("id", flat=True).iterator():
        salt = 0
        code = code_for(user_id, salt)
        while code in used:
            salt += 1
            code = code_for(user_id, salt)
        used.add(code)
        pending.append(ReferralAccount(user_id=user_id, code=code))
    ReferralAccount.objects.bulk_create(pending, batch_size=500, ignore_conflicts=True)


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("identity", "0003_single_email_per_user"),
    ]

    operations = [
        migrations.CreateModel(
            name="ReferralAccount",
            fields=[
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        related_name="referral_account",
                        serialize=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                ("code", models.CharField(max_length=12, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="Referral",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "referee",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="referral_received",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "referrer",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="referrals_made",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="PointTransaction",
            fields=[
                (
                    "id",
                    models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
                ),
                (
                    "direction",
                    models.CharField(choices=[("credit", "Credit"), ("debit", "Debit")], max_length=10),
                ),
                ("amount", models.PositiveIntegerField()),
                (
                    "template",
                    models.CharField(
                        choices=[
                            ("referral_successful", "Referral successful"),
                            ("referral_bonus", "Referral bonus"),
                            ("redeemed_seasonal_vegetables", "Redeemed seasonal vegetables"),
                            ("redeemed_fresh_chicken", "Redeemed fresh chicken"),
                            ("manual_adjustment", "Manual adjustment"),
                        ],
                        max_length=50,
                    ),
                ),
                ("message", models.CharField(max_length=240)),
                ("internal_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="point_transactions_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "related_referral",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="point_transactions",
                        to="referrals.referral",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="point_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddConstraint(
            model_name="referral",
            constraint=models.CheckConstraint(
                condition=~Q(referrer=F("referee")),
                name="referrals_referrer_is_not_referee",
            ),
        ),
        migrations.AddIndex(
            model_name="referral",
            index=models.Index(fields=["referrer", "-created_at"], name="referrals_r_referre_15a4c9_idx"),
        ),
        migrations.AddConstraint(
            model_name="pointtransaction",
            constraint=models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="referrals_point_amount_positive",
            ),
        ),
        migrations.AddIndex(
            model_name="pointtransaction",
            index=models.Index(fields=["user", "-created_at"], name="referrals_p_user_id_ce2b2d_idx"),
        ),
        migrations.RunPython(backfill_referral_accounts, migrations.RunPython.noop),
    ]
