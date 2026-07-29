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
