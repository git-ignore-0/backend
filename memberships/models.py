# ruff: noqa: DJ008
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class QuotaPolicy(models.TextChoices):
    EXPIRE = "expire", "Expire"
    ROLLOVER = "rollover", "Rollover"


class MembershipRequestStatus(models.TextChoices):
    CONSULTATION_REQUESTED = "consultation_requested", "Consultation requested"
    CONTACTED = "contacted", "Contacted"
    PAYMENT_PENDING = "payment_pending", "Payment pending"
    PAYMENT_SUBMITTED = "payment_submitted", "Payment submitted"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"


OPEN_REQUEST_STATUSES = [
    MembershipRequestStatus.CONSULTATION_REQUESTED,
    MembershipRequestStatus.CONTACTED,
    MembershipRequestStatus.PAYMENT_PENDING,
    MembershipRequestStatus.PAYMENT_SUBMITTED,
]
TERMINAL_REQUEST_STATUSES = [
    MembershipRequestStatus.APPROVED,
    MembershipRequestStatus.REJECTED,
]


class MembershipRequestEventAction(models.TextChoices):
    REQUEST_CREATED = "request_created", "Request created"
    CONTACTED = "contacted", "Contacted"
    PAYMENT_INSTRUCTION_ISSUED = "payment_instruction_issued", "Payment instruction issued"
    PAYMENT_SUBMITTED = "payment_submitted", "Payment submitted"
    PAYMENT_RETURNED = "payment_returned", "Payment returned"
    REJECTED = "rejected", "Rejected"
    APPROVED = "approved", "Approved"


class MembershipStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "Active"
    ENDED = "ended", "Ended"


class UsageOrderStatus(models.TextChoices):
    APPLIED = "applied", "Applied"
    REVERSED = "reversed", "Reversed"


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


class MembershipPackage(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    upfront_price = models.DecimalField(max_digits=14, decimal_places=0)
    duration_months = models.PositiveSmallIntegerField(default=6)
    quota_policy = models.CharField(max_length=16, choices=QuotaPolicy.choices)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(upfront_price__gt=0), name="memberships_package_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(duration_months__gt=0), name="memberships_package_duration_positive"
            ),
            models.CheckConstraint(
                condition=Q(quota_policy__in=QuotaPolicy.values),
                name="memberships_package_policy_valid",
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


class MembershipPaymentSettings(models.Model):
    key = models.CharField(primary_key=True, max_length=20, default="default", editable=False)
    direct_transfer_enabled = models.BooleanField(default=False)
    bank_bin = models.CharField(max_length=6, blank=True)
    bank_name = models.CharField(max_length=160, blank=True)
    account_number = models.CharField(max_length=19)
    account_name = models.CharField(max_length=160)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(key="default"), name="memberships_payment_settings_singleton_key"
            )
        ]


class MembershipRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    short_code = models.CharField(max_length=10, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="membership_requests", on_delete=models.PROTECT
    )
    package = models.ForeignKey(
        MembershipPackage, related_name="requests", on_delete=models.PROTECT
    )
    package_name_snapshot = models.CharField(max_length=200)
    package_description_snapshot = models.TextField(blank=True)
    upfront_price_snapshot = models.DecimalField(max_digits=14, decimal_places=0)
    duration_months_snapshot = models.PositiveSmallIntegerField()
    quota_policy_snapshot = models.CharField(max_length=16, choices=QuotaPolicy.choices)
    phone_snapshot = models.CharField(max_length=32)
    status = models.CharField(max_length=32, choices=MembershipRequestStatus.choices)
    open_slot = models.BooleanField(null=True, default=True, editable=False)
    contacted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="membership_requests_contacted",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="membership_requests_processed",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    return_reason = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    contacted_at = models.DateTimeField(null=True, blank=True)
    payment_submitted_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["user", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "open_slot"], name="memberships_one_open_request_per_user"
            ),
            models.CheckConstraint(
                condition=Q(upfront_price_snapshot__gt=0), name="memberships_request_price_positive"
            ),
            models.CheckConstraint(
                condition=Q(duration_months_snapshot__gt=0),
                name="memberships_request_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(status__in=MembershipRequestStatus.values),
                name="memberships_request_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=OPEN_REQUEST_STATUSES, open_slot=True)
                    | Q(status__in=TERMINAL_REQUEST_STATUSES, open_slot__isnull=True)
                ),
                name="memberships_request_open_slot_consistent",
            ),
        ]


class MembershipRequestEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    request = models.ForeignKey(MembershipRequest, related_name="events", on_delete=models.PROTECT)
    action = models.CharField(max_length=40, choices=MembershipRequestEventAction.choices)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="membership_request_events",
        null=True,
        on_delete=models.SET_NULL,
    )
    reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["request", "created_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(action__in=MembershipRequestEventAction.values),
                name="memberships_request_event_action_valid",
            )
        ]


class MembershipPaymentInstruction(models.Model):
    request = models.OneToOneField(
        MembershipRequest,
        primary_key=True,
        related_name="payment_instruction",
        on_delete=models.PROTECT,
    )
    bank_code = models.CharField(max_length=20)
    bank_bin = models.CharField(max_length=6, blank=True)
    bank_name = models.CharField(max_length=160, blank=True)
    account_number = models.CharField(max_length=34)
    account_name = models.CharField(max_length=160)
    amount = models.DecimalField(max_digits=14, decimal_places=0)
    phone_snapshot = models.CharField(max_length=32)
    transfer_content = models.CharField(max_length=80)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="membership_payment_instructions_issued",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    issued_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="memberships_instruction_amount_positive"
            )
        ]


class MembershipRequestItemSnapshot(models.Model):
    request = models.ForeignKey(
        MembershipRequest, related_name="item_snapshots", on_delete=models.PROTECT
    )
    product = models.ForeignKey(
        Product, related_name="membership_request_snapshots", on_delete=models.PROTECT
    )
    product_name = models.CharField(max_length=200)
    unit_size = models.DecimalField(max_digits=10, decimal_places=3)
    unit_label_vi = models.CharField(max_length=40)
    unit_label_en = models.CharField(max_length=40)
    quota_units_per_cycle = models.PositiveIntegerField()

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["request", "product"], name="memberships_request_snapshot_product_unique"
            ),
            models.CheckConstraint(
                condition=Q(unit_size__gt=0), name="memberships_request_snapshot_unit_positive"
            ),
            models.CheckConstraint(
                condition=Q(quota_units_per_cycle__gt=0),
                name="memberships_request_snapshot_quota_positive",
            ),
        ]


class Membership(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, related_name="memberships", on_delete=models.PROTECT
    )
    request = models.OneToOneField(
        MembershipRequest, related_name="membership", on_delete=models.PROTECT
    )
    package = models.ForeignKey(
        MembershipPackage, related_name="memberships", on_delete=models.PROTECT
    )
    package_name_snapshot = models.CharField(max_length=200)
    package_description_snapshot = models.TextField(blank=True)
    upfront_price_snapshot = models.DecimalField(max_digits=14, decimal_places=0)
    duration_months_snapshot = models.PositiveSmallIntegerField()
    quota_policy_snapshot = models.CharField(max_length=16, choices=QuotaPolicy.choices)
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
