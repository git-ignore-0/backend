import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0012_workshoprevision_content_workshop_revision_not_draft_and_published"),
        ("referrals", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="pointtransaction",
            name="template",
            field=models.CharField(
                choices=[
                    ("referral_successful", "Referral successful"),
                    ("referral_bonus", "Referral bonus"),
                    ("redeemed_seasonal_vegetables", "Redeemed seasonal vegetables"),
                    ("redeemed_fresh_chicken", "Redeemed fresh chicken"),
                    ("reward_redemption", "Reward redemption"),
                    ("reward_refund", "Reward refund"),
                    ("manual_adjustment", "Manual adjustment"),
                ],
                max_length=50,
            ),
        ),
        migrations.CreateModel(
            name="Reward",
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
                ("point_cost", models.PositiveIntegerField()),
                ("position", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "image",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rewards",
                        to="content.mediaasset",
                    ),
                ),
            ],
            options={"ordering": ["position", "created_at", "id"]},
        ),
        migrations.CreateModel(
            name="RedemptionRequest",
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
                ("reward_name_vi_snapshot", models.CharField(max_length=240)),
                ("reward_name_en_snapshot", models.CharField(max_length=240)),
                ("point_cost_snapshot", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("contacted", "Contacted"),
                            ("completed", "Completed"),
                            ("rejected", "Rejected"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("idempotency_key", models.UUIDField()),
                ("rejection_message", models.TextField(blank=True)),
                ("internal_note", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("contacted_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("rejected_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "contacted_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="redemptions_contacted",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "debit_transaction",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="debited_redemption",
                        to="referrals.pointtransaction",
                    ),
                ),
                (
                    "processed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="redemptions_processed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "refund_transaction",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="refunded_redemption",
                        to="referrals.pointtransaction",
                    ),
                ),
                (
                    "reward",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="redemption_requests",
                        to="referrals.reward",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="redemption_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.CreateModel(
            name="RewardTranslation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "locale",
                    models.CharField(
                        choices=[("vi", "Vietnamese"), ("en", "English")],
                        max_length=2,
                    ),
                ),
                ("name", models.CharField(max_length=240)),
                ("short_description", models.CharField(blank=True, max_length=800)),
                (
                    "reward",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="translations",
                        to="referrals.reward",
                    ),
                ),
            ],
            options={"ordering": ["locale", "id"]},
        ),
        migrations.AddConstraint(
            model_name="reward",
            constraint=models.CheckConstraint(
                condition=models.Q(("point_cost__gt", 0)),
                name="referrals_reward_point_cost_positive",
            ),
        ),
        migrations.AddIndex(
            model_name="redemptionrequest",
            index=models.Index(
                fields=["user", "-created_at"],
                name="referrals_r_user_id_4daaa8_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="redemptionrequest",
            index=models.Index(
                fields=["status", "-created_at"],
                name="referrals_r_status_e7bc6a_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(("point_cost_snapshot__gt", 0)),
                name="referrals_redemption_point_cost_positive",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.UniqueConstraint(
                fields=("user", "idempotency_key"),
                name="referrals_redemption_user_idempotency_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("refund_transaction__isnull", True),
                    ("debit_transaction__isnull", True),
                    models.Q(
                        ("refund_transaction", models.F("debit_transaction")),
                        _negated=True,
                    ),
                    _connector="OR",
                ),
                name="referrals_redemption_debit_refund_distinct",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["pending", "contacted", "completed", "rejected"])
                ),
                name="referrals_redemption_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("completed_at__isnull", False),
                        ("refund_transaction__isnull", True),
                        ("rejected_at__isnull", True),
                        ("rejection_message", ""),
                        ("status", "completed"),
                    ),
                    models.Q(
                        ("completed_at__isnull", True),
                        models.Q(("status", "completed"), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="referrals_redemption_completed_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("completed_at__isnull", True),
                        ("refund_transaction__isnull", False),
                        ("rejected_at__isnull", False),
                        ("status", "rejected"),
                        models.Q(("rejection_message", ""), _negated=True),
                    ),
                    models.Q(
                        models.Q(("status", "rejected"), _negated=True),
                        ("refund_transaction__isnull", True),
                        ("rejected_at__isnull", True),
                        ("rejection_message", ""),
                    ),
                    _connector="OR",
                ),
                name="referrals_redemption_rejected_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="redemptionrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "contacted"), _negated=True),
                    ("contacted_at__isnull", False),
                    _connector="OR",
                ),
                name="referrals_redemption_contacted_timestamp",
            ),
        ),
        migrations.AddConstraint(
            model_name="rewardtranslation",
            constraint=models.UniqueConstraint(
                fields=("reward", "locale"),
                name="referrals_reward_translation_locale_unique",
            ),
        ),
        migrations.AddConstraint(
            model_name="rewardtranslation",
            constraint=models.CheckConstraint(
                condition=models.Q(("locale__in", ["vi", "en"])),
                name="referrals_reward_translation_locale_supported",
            ),
        ),
    ]
