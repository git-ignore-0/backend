# ruff: noqa: DJ008
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

from memberships.pricing import calculate_total_price_vnd


class QuotaPolicy(models.TextChoices):
    EXPIRE = "expire", "Expire"
    ROLLOVER = "rollover", "Rollover"


class MembershipStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "Active"
    ENDED = "ended", "Ended"


class UsageOrderStatus(models.TextChoices):
    APPLIED = "applied", "Applied"
    REVERSED = "reversed", "Reversed"


class MembershipContentLocale(models.TextChoices):
    VI = "vi", "Vietnamese"
    EN = "en", "English"


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    short_description = models.CharField(max_length=800, blank=True)
    unit_size = models.DecimalField(max_digits=10, decimal_places=3)
    unit_label_vi = models.CharField(max_length=40)
    unit_label_en = models.CharField(max_length=40)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_size__gt=0), name="memberships_product_unit_size_positive"
            )
        ]


class ProductTranslation(models.Model):
    product = models.ForeignKey(Product, related_name="translations", on_delete=models.CASCADE)
    locale = models.CharField(max_length=2, choices=MembershipContentLocale.choices)
    name = models.CharField(max_length=200)
    short_description = models.CharField(max_length=800, blank=True)

    class Meta:
        ordering = ["locale", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "locale"], name="memberships_product_translation_unique"
            ),
            models.CheckConstraint(
                condition=Q(locale__in=MembershipContentLocale.values),
                name="memberships_product_translation_locale_valid",
            ),
        ]


class MembershipPackage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    quota_policy = models.CharField(max_length=16, choices=QuotaPolicy.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(quota_policy__in=QuotaPolicy.values),
                name="memberships_package_policy_valid",
            ),
        ]


class MembershipPackagePriceOption(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    package = models.ForeignKey(
        MembershipPackage, related_name="price_options", on_delete=models.CASCADE
    )
    duration_months = models.PositiveSmallIntegerField()
    monthly_price_vnd = models.PositiveBigIntegerField()
    is_active = models.BooleanField(default=True)
    active_duration_key = models.PositiveSmallIntegerField(null=True, editable=False)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "duration_months", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["package", "active_duration_key"],
                name="memberships_package_active_duration_unique",
            ),
            models.CheckConstraint(
                condition=Q(duration_months__gt=0),
                name="memberships_package_option_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(monthly_price_vnd__gt=0),
                name="memberships_package_option_monthly_price_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_active=True, active_duration_key=models.F("duration_months"))
                    | Q(is_active=False, active_duration_key__isnull=True)
                ),
                name="memberships_package_option_active_key_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        self.active_duration_key = self.duration_months if self.is_active else None
        if update_fields := kwargs.get("update_fields"):
            kwargs["update_fields"] = set(update_fields) | {"active_duration_key"}
        return super().save(*args, **kwargs)

    @property
    def total_price_vnd(self):
        return calculate_total_price_vnd(
            duration_months=self.duration_months,
            monthly_price_vnd=self.monthly_price_vnd,
        )


class MembershipPackageTranslation(models.Model):
    package = models.ForeignKey(
        MembershipPackage, related_name="translations", on_delete=models.CASCADE
    )
    locale = models.CharField(max_length=2, choices=MembershipContentLocale.choices)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["locale", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["package", "locale"], name="memberships_package_translation_unique"
            ),
            models.CheckConstraint(
                condition=Q(locale__in=MembershipContentLocale.values),
                name="memberships_package_translation_locale_valid",
            ),
        ]


class MembershipPackageItem(models.Model):
    package = models.ForeignKey(MembershipPackage, related_name="items", on_delete=models.CASCADE)
    product = models.ForeignKey(Product, related_name="package_items", on_delete=models.PROTECT)
    quota_units = models.PositiveIntegerField()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["package", "product"], name="memberships_package_product_unique"
            ),
            models.CheckConstraint(
                condition=Q(quota_units__gt=0), name="memberships_package_item_quota_positive"
            ),
        ]


class Membership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="memberships", on_delete=models.PROTECT
    )
    package = models.ForeignKey(
        MembershipPackage, related_name="memberships", on_delete=models.PROTECT
    )
    price_option = models.ForeignKey(
        MembershipPackagePriceOption, related_name="memberships", on_delete=models.PROTECT
    )
    package_name_snapshot = models.CharField(max_length=200)
    package_description_snapshot = models.TextField(blank=True)
    package_name_vi_snapshot = models.CharField(max_length=200, blank=True, default="")
    package_name_en_snapshot = models.CharField(max_length=200, blank=True, default="")
    package_description_vi_snapshot = models.TextField(blank=True, default="")
    package_description_en_snapshot = models.TextField(blank=True, default="")
    monthly_price_vnd_snapshot = models.PositiveBigIntegerField()
    total_price_vnd_snapshot = models.PositiveBigIntegerField()
    duration_months_snapshot = models.PositiveSmallIntegerField()
    quota_policy_snapshot = models.CharField(max_length=16, choices=QuotaPolicy.choices)
    farmbrite_order_reference = models.CharField(max_length=120, unique=True)
    farmbrite_order_reference_key = models.CharField(
        max_length=240, unique=True, editable=False
    )
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=12, choices=MembershipStatus.choices, default=MembershipStatus.ACTIVE
    )
    current_slot = models.BooleanField(null=True, default=True, editable=False)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="memberships_activated",
        null=True,
        on_delete=models.SET_NULL,
    )
    activated_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-activated_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "current_slot"], name="memberships_one_current_per_user"
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=models.F("start_date")), name="memberships_dates_valid"
            ),
            models.CheckConstraint(
                condition=Q(monthly_price_vnd_snapshot__gt=0),
                name="memberships_monthly_price_snapshot_positive",
            ),
            models.CheckConstraint(
                condition=Q(total_price_vnd_snapshot__gt=0),
                name="memberships_total_price_snapshot_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    total_price_vnd_snapshot=(
                        models.F("monthly_price_vnd_snapshot")
                        * models.F("duration_months_snapshot")
                    )
                ),
                name="memberships_price_snapshot_total_exact",
            ),
            models.CheckConstraint(
                condition=Q(status__in=MembershipStatus.values), name="memberships_status_valid"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status__in=[MembershipStatus.SCHEDULED, MembershipStatus.ACTIVE],
                        current_slot=True,
                    )
                    | Q(status=MembershipStatus.ENDED, current_slot__isnull=True)
                ),
                name="memberships_current_slot_consistent",
            ),
        ]


class MembershipProductSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        Membership, related_name="product_snapshots", on_delete=models.PROTECT
    )
    product = models.ForeignKey(
        Product, related_name="membership_snapshots", on_delete=models.PROTECT
    )
    product_name = models.CharField(max_length=200)
    product_name_vi_snapshot = models.CharField(max_length=200, blank=True, default="")
    product_name_en_snapshot = models.CharField(max_length=200, blank=True, default="")
    unit_size = models.DecimalField(max_digits=10, decimal_places=3)
    unit_label_vi = models.CharField(max_length=40)
    unit_label_en = models.CharField(max_length=40)
    quota_units_per_cycle = models.PositiveIntegerField()

    class Meta:
        ordering = ["product_name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "product"], name="memberships_snapshot_product_unique"
            ),
            models.CheckConstraint(
                condition=Q(unit_size__gt=0), name="memberships_snapshot_unit_size_positive"
            ),
            models.CheckConstraint(
                condition=Q(quota_units_per_cycle__gt=0), name="memberships_snapshot_quota_positive"
            ),
        ]


class MembershipCycle(models.Model):
    membership = models.ForeignKey(Membership, related_name="cycles", on_delete=models.PROTECT)
    sequence = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "sequence"], name="memberships_cycle_sequence_unique"
            ),
            models.CheckConstraint(
                condition=Q(sequence__gt=0), name="memberships_cycle_sequence_positive"
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=models.F("start_date")),
                name="memberships_cycle_dates_valid",
            ),
        ]


class MembershipCycleQuota(models.Model):
    cycle = models.ForeignKey(MembershipCycle, related_name="quotas", on_delete=models.PROTECT)
    product_snapshot = models.ForeignKey(
        MembershipProductSnapshot, related_name="cycle_quotas", on_delete=models.PROTECT
    )
    granted_units = models.PositiveIntegerField()
    remaining_units = models.PositiveIntegerField()
    expired_units = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["cycle__sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cycle", "product_snapshot"], name="memberships_cycle_product_unique"
            ),
            models.CheckConstraint(
                condition=Q(granted_units__gt=0), name="memberships_cycle_granted_positive"
            ),
            models.CheckConstraint(
                condition=Q(remaining_units__gte=0), name="memberships_cycle_remaining_nonnegative"
            ),
            models.CheckConstraint(
                condition=Q(
                    remaining_units__lte=models.F("granted_units") - models.F("expired_units")
                ),
                name="memberships_cycle_quota_totals_valid",
            ),
        ]


class MembershipUsageOrder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        Membership, related_name="usage_orders", on_delete=models.PROTECT
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="membership_usage_orders", on_delete=models.PROTECT
    )
    idempotency_key = models.UUIDField()
    status = models.CharField(
        max_length=12, choices=UsageOrderStatus.choices, default=UsageOrderStatus.APPLIED
    )
    note = models.CharField(max_length=500, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="membership_usage_orders_created",
        null=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["membership", "-created_at"]),
            models.Index(fields=["user", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "idempotency_key"],
                name="memberships_usage_idempotency_unique",
            ),
            models.CheckConstraint(
                condition=Q(status__in=UsageOrderStatus.values),
                name="memberships_usage_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status=UsageOrderStatus.APPLIED, reversed_at__isnull=True)
                    | Q(status=UsageOrderStatus.REVERSED, reversed_at__isnull=False)
                ),
                name="memberships_usage_reversal_state_consistent",
            ),
        ]


class MembershipUsageLine(models.Model):
    order = models.ForeignKey(MembershipUsageOrder, related_name="lines", on_delete=models.PROTECT)
    product_snapshot = models.ForeignKey(
        MembershipProductSnapshot, related_name="usage_lines", on_delete=models.PROTECT
    )
    units = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "product_snapshot"], name="memberships_usage_product_unique"
            ),
            models.CheckConstraint(
                condition=Q(units__gt=0), name="memberships_usage_units_positive"
            ),
        ]


class MembershipUsageAllocation(models.Model):
    line = models.ForeignKey(
        MembershipUsageLine, related_name="allocations", on_delete=models.PROTECT
    )
    cycle_quota = models.ForeignKey(
        MembershipCycleQuota, related_name="usage_allocations", on_delete=models.PROTECT
    )
    units = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "cycle_quota"], name="memberships_usage_allocation_unique"
            ),
            models.CheckConstraint(
                condition=Q(units__gt=0), name="memberships_allocation_units_positive"
            ),
        ]


class MembershipUsageReversal(models.Model):
    order = models.OneToOneField(
        MembershipUsageOrder, primary_key=True, related_name="reversal", on_delete=models.PROTECT
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="membership_usage_reversals",
        null=True,
        on_delete=models.SET_NULL,
    )
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
