import uuid

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class ReferralAccount(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        related_name="referral_account",
        on_delete=models.CASCADE,
    )
    code = models.CharField(max_length=12, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.code


class Referral(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    referrer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="referrals_made",
        on_delete=models.PROTECT,
    )
    referee = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="referral_received",
        on_delete=models.PROTECT,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(referrer=F("referee")),
                name="referrals_referrer_is_not_referee",
            )
        ]
        indexes = [models.Index(fields=["referrer", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.referrer_id} -> {self.referee_id}"


class PointDirection(models.TextChoices):
    CREDIT = "credit", "Credit"
    DEBIT = "debit", "Debit"


class PointTemplate(models.TextChoices):
    REFERRAL_SUCCESSFUL = "referral_successful", "Referral successful"
    REFERRAL_BONUS = "referral_bonus", "Referral bonus"
    REDEEMED_SEASONAL_VEGETABLES = (
        "redeemed_seasonal_vegetables",
        "Redeemed seasonal vegetables",
    )
    REDEEMED_FRESH_CHICKEN = "redeemed_fresh_chicken", "Redeemed fresh chicken"
    REWARD_REDEMPTION = "reward_redemption", "Reward redemption"
    REWARD_REFUND = "reward_refund", "Reward refund"
    MANUAL_ADJUSTMENT = "manual_adjustment", "Manual adjustment"


class PointTransaction(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="point_transactions",
        on_delete=models.PROTECT,
    )
    direction = models.CharField(max_length=10, choices=PointDirection.choices)
    amount = models.PositiveIntegerField()
    template = models.CharField(max_length=50, choices=PointTemplate.choices)
    message = models.CharField(max_length=240)
    internal_note = models.TextField(blank=True)
    related_referral = models.ForeignKey(
        Referral,
        related_name="point_transactions",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="point_transactions_created",
        null=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="referrals_point_amount_positive",
            )
        ]
        indexes = [models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.user_id}: {self.direction} {self.amount}"


class Reward(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    point_cost = models.PositiveIntegerField()
    image = models.ForeignKey(
        "content.MediaAsset",
        related_name="rewards",
        on_delete=models.PROTECT,
    )
    position = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position", "created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(point_cost__gt=0),
                name="referrals_reward_point_cost_positive",
            ),
        ]

    def __str__(self) -> str:
        return str(self.pk)


class RewardTranslation(models.Model):
    reward = models.ForeignKey(
        Reward,
        related_name="translations",
        on_delete=models.CASCADE,
    )
    locale = models.CharField(max_length=2, choices=[("vi", "Vietnamese"), ("en", "English")])
    name = models.CharField(max_length=240)
    short_description = models.CharField(max_length=800, blank=True)

    class Meta:
        ordering = ["locale", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["reward", "locale"],
                name="referrals_reward_translation_locale_unique",
            ),
            models.CheckConstraint(
                condition=Q(locale__in=["vi", "en"]),
                name="referrals_reward_translation_locale_supported",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.reward_id}:{self.locale}"


class RedemptionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    CONTACTED = "contacted", "Contacted"
    COMPLETED = "completed", "Completed"
    REJECTED = "rejected", "Rejected"


class RedemptionRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="redemption_requests",
        on_delete=models.PROTECT,
    )
    reward = models.ForeignKey(
        Reward,
        related_name="redemption_requests",
        on_delete=models.PROTECT,
    )
    reward_name_vi_snapshot = models.CharField(max_length=240)
    reward_name_en_snapshot = models.CharField(max_length=240)
    point_cost_snapshot = models.PositiveIntegerField()
    status = models.CharField(
        max_length=16,
        choices=RedemptionStatus.choices,
        default=RedemptionStatus.PENDING,
    )
    idempotency_key = models.UUIDField()
    debit_transaction = models.OneToOneField(
        PointTransaction,
        related_name="debited_redemption",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    refund_transaction = models.OneToOneField(
        PointTransaction,
        related_name="refunded_redemption",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    contacted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="redemptions_contacted",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="redemptions_processed",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    rejection_message = models.TextField(blank=True)
    internal_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    contacted_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status", "-created_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(point_cost_snapshot__gt=0),
                name="referrals_redemption_point_cost_positive",
            ),
            models.UniqueConstraint(
                fields=["user", "idempotency_key"],
                name="referrals_redemption_user_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(refund_transaction__isnull=True)
                    | Q(debit_transaction__isnull=True)
                    | ~Q(refund_transaction=F("debit_transaction"))
                ),
                name="referrals_redemption_debit_refund_distinct",
            ),
            models.CheckConstraint(
                condition=Q(status__in=RedemptionStatus.values),
                name="referrals_redemption_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=RedemptionStatus.COMPLETED,
                        completed_at__isnull=False,
                        rejected_at__isnull=True,
                        refund_transaction__isnull=True,
                        rejection_message="",
                    )
                    | (Q(completed_at__isnull=True) & ~Q(status=RedemptionStatus.COMPLETED))
                ),
                name="referrals_redemption_completed_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(
                            status=RedemptionStatus.REJECTED,
                            completed_at__isnull=True,
                            rejected_at__isnull=False,
                            refund_transaction__isnull=False,
                        )
                        & ~Q(rejection_message="")
                    )
                    | Q(
                        ~Q(status=RedemptionStatus.REJECTED),
                        rejected_at__isnull=True,
                        refund_transaction__isnull=True,
                        rejection_message="",
                    )
                ),
                name="referrals_redemption_rejected_consistent",
            ),
            models.CheckConstraint(
                condition=(~Q(status=RedemptionStatus.CONTACTED) | Q(contacted_at__isnull=False)),
                name="referrals_redemption_contacted_timestamp",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}: {self.status} {self.point_cost_snapshot}"
