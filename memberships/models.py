# ruff: noqa: DJ008
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Trim, Upper

from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone
from memberships.pricing import calculate_total_price_vnd


class QuotaPolicy(models.TextChoices):
    EXPIRE = "expire", "Expire"
    ROLLOVER = "rollover", "Rollover"


class MembershipStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Scheduled"
    ACTIVE = "active", "Active"
    ENDED = "ended", "Ended"
    REVOKED = "revoked", "Revoked"


class UsageOrderStatus(models.TextChoices):
    APPLIED = "applied", "Applied"
    REVERSED = "reversed", "Reversed"


class MembershipContentLocale(models.TextChoices):
    VI = "vi", "Vietnamese"
    EN = "en", "English"


class MembershipPaymentType(models.TextChoices):
    FULL = "full", "Full payment"
    INSTALLMENT = "installment", "Installment payment"


class CSAPurchasePaymentStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    REPORTED = "reported", "Reported"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"
    OVERDUE = "overdue", "Overdue"


class CSAPurchaseRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PAYMENT_CONFIRMED = "payment_confirmed", "Payment confirmed"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    EXPIRED = "expired", "Expired"


class CSAPurchaseRefundStatus(models.TextChoices):
    NOT_APPLICABLE = "not_applicable", "Not applicable"
    PENDING = "pending", "Pending"
    COMPLETED = "completed", "Completed"


class CSAContractSource(models.TextChoices):
    ONLINE = "online", "Online"
    ADMIN_MANUAL = "admin_manual", "Admin manual"


class CSAContractStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    REVOKED = "revoked", "Revoked"


class CSAContractTermsAcceptanceMethod(models.TextChoices):
    ONLINE = "online", "Online"
    IN_STORE = "in_store", "In store"


class CSAContractVersionReason(models.TextChoices):
    ISSUED = "issued", "Issued"
    PAYMENT_CONFIRMED = "payment_confirmed", "Payment confirmed"
    MEMBERSHIP_REVOKED = "membership_revoked", "Membership revoked"


OPEN_CSA_PURCHASE_REQUEST_STATUSES = [
    CSAPurchaseRequestStatus.PENDING,
    CSAPurchaseRequestStatus.PAYMENT_CONFIRMED,
]
TERMINAL_CSA_PURCHASE_REQUEST_STATUSES = [
    CSAPurchaseRequestStatus.APPROVED,
    CSAPurchaseRequestStatus.REJECTED,
    CSAPurchaseRequestStatus.EXPIRED,
]


def generate_csa_purchase_request_code() -> str:
    return f"CSA-{uuid.uuid4().hex[:12].upper()}"


class CSACustomer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="csa_customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    name = models.CharField(max_length=150)
    phone_e164 = models.CharField(max_length=13, null=True, blank=True, unique=True)
    province_code = models.CharField(max_length=2, blank=True)
    ward_code = models.CharField(max_length=5, blank=True)
    address = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


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


class MembershipPaymentPlan(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    price_option = models.ForeignKey(
        MembershipPackagePriceOption, related_name="payment_plans", on_delete=models.PROTECT
    )
    name = models.CharField(max_length=120)
    payment_type = models.CharField(max_length=12, choices=MembershipPaymentType.choices)
    total_amount = models.DecimalField(max_digits=19, decimal_places=0)
    installment_count = models.PositiveSmallIntegerField()
    # A newly assembled plan must pass cross-row validation before activation.
    is_active = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["price_option", "is_active", "sort_order"],
                name="membership_plan_catalog_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(payment_type__in=MembershipPaymentType.values),
                name="membership_plan_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(payment_type=MembershipPaymentType.FULL, installment_count=1)
                    | Q(payment_type=MembershipPaymentType.INSTALLMENT, installment_count__gte=2)
                ),
                name="membership_plan_type_count_valid",
            ),
            models.CheckConstraint(
                condition=Q(total_amount__gt=0), name="membership_plan_total_positive"
            ),
        ]


class MembershipPaymentPlanInstallment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payment_plan = models.ForeignKey(
        MembershipPaymentPlan, related_name="installments", on_delete=models.CASCADE
    )
    sequence = models.PositiveSmallIntegerField()
    amount = models.DecimalField(max_digits=19, decimal_places=0)
    cycle_count = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["payment_plan", "sequence"],
                name="membership_plan_installment_sequence_unique",
            ),
            models.CheckConstraint(
                condition=Q(sequence__gt=0), name="membership_plan_sequence_positive"
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0), name="membership_plan_installment_amount_positive"
            ),
            models.CheckConstraint(
                condition=Q(cycle_count__gte=1), name="membership_plan_cycle_count_positive"
            ),
        ]


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


class MembershipPaymentSettings(models.Model):
    key = models.CharField(primary_key=True, max_length=20, default="default", editable=False)
    direct_transfer_enabled = models.BooleanField(default=False)
    bank_bin = models.CharField(max_length=6)
    bank_name = models.CharField(max_length=160)
    account_number = models.CharField(max_length=19)
    account_name = models.CharField(max_length=160)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="membership_payment_settings_updates",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(key="default"), name="memberships_payment_settings_singleton_key"
            )
        ]

    def save(self, *args, **kwargs):
        self.full_clean(validate_unique=False, validate_constraints=False)
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from memberships.banks import BankDirectoryUnavailable, find_transfer_bank

        self.bank_bin = self.bank_bin.strip()
        self.bank_name = self.bank_name.strip()
        self.account_number = self.account_number.strip()
        self.account_name = self.account_name.strip()
        errors = {}
        if len(self.bank_bin) != 6 or not self.bank_bin.isdigit():
            errors["bank_bin"] = "Bank BIN must contain exactly 6 digits."
        if not (6 <= len(self.account_number) <= 19) or not self.account_number.isdigit():
            errors["account_number"] = "Account number must contain 6 to 19 digits."
        if not self.bank_name:
            errors["bank_name"] = "Bank name is required."
        if not self.account_name:
            errors["account_name"] = "Account name is required."
        if errors:
            raise ValidationError(errors)
        try:
            bank = find_transfer_bank(self.bank_bin)
        except BankDirectoryUnavailable as error:
            raise ValidationError({"bank_bin": "The bank directory is unavailable."}) from error
        if not bank:
            raise ValidationError({"bank_bin": "Select a supported VietQR bank."})
        if self.bank_name != bank["name"].strip():
            raise ValidationError({"bank_name": "Bank name does not match the selected BIN."})


class CSACoupon(models.Model):
    class DiscountType(models.TextChoices):
        PERCENT = "percent", "Percent"
        FIXED = "fixed", "Fixed VND"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=64)
    discount_type = models.CharField(max_length=7, choices=DiscountType.choices)
    discount_value = models.DecimalField(max_digits=19, decimal_places=2)
    is_active = models.BooleanField(default=True)
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    usage_limit = models.PositiveIntegerField(default=1)
    used_count = models.PositiveIntegerField(default=0, editable=False)
    package = models.ForeignKey(MembershipPackage, null=True, blank=True, on_delete=models.PROTECT)
    price_option = models.ForeignKey(
        MembershipPackagePriceOption, null=True, blank=True, on_delete=models.PROTECT
    )
    payment_type = models.CharField(
        max_length=12, choices=MembershipPaymentType.choices, null=True, blank=True
    )
    installment_count = models.PositiveSmallIntegerField(null=True, blank=True)
    minimum_amount = models.DecimalField(max_digits=19, decimal_places=0, null=True, blank=True)
    maximum_discount_amount = models.DecimalField(
        max_digits=19, decimal_places=0, null=True, blank=True
    )
    package_ids = models.JSONField(default=list, blank=True)
    price_option_ids = models.JSONField(default=list, blank=True)
    payment_types = models.JSONField(default=list, blank=True)
    installment_counts = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "id"]
        constraints = [
            models.UniqueConstraint(Upper(Trim("code")), name="memberships_coupon_code_ci_unique"),
            models.CheckConstraint(condition=Q(usage_limit__gt=0), name="coupon_limit_positive"),
            models.CheckConstraint(
                condition=Q(used_count__lte=models.F("usage_limit")),
                name="coupon_usage_within_limit",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__gt=models.F("starts_at")), name="coupon_dates_ordered"
            ),
            models.CheckConstraint(condition=Q(discount_value__gt=0), name="coupon_value_positive"),
            models.CheckConstraint(
                condition=Q(discount_type__in=["percent", "fixed"]), name="coupon_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(minimum_amount__isnull=True) | Q(minimum_amount__gt=0),
                name="coupon_minimum_positive",
            ),
            models.CheckConstraint(
                condition=Q(maximum_discount_amount__isnull=True)
                | Q(discount_type="percent", maximum_discount_amount__gt=0),
                name="coupon_max_discount_valid",
            ),
            models.CheckConstraint(
                condition=Q(installment_count__isnull=True) | Q(installment_count__gt=0),
                name="coupon_count_positive",
            ),
        ]

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from memberships.coupons import CouponError, normalize_coupon_code

        try:
            self.code = normalize_coupon_code(self.code)
        except CouponError as error:
            raise ValidationError({"code": error.code}) from error
        errors = {}
        try:
            value = (
                Decimal(str(self.discount_value))
                if not isinstance(self.discount_value, float)
                else None
            )
        except (InvalidOperation, TypeError, ValueError):
            value = None
        if value is None or not value.is_finite():
            errors["discount_value"] = "coupon_invalid_value"
        elif self.discount_type == self.DiscountType.PERCENT and value > 100:
            errors["discount_value"] = "coupon_invalid_value"
        if self.maximum_discount_amount is not None and (
            self.maximum_discount_amount <= 0 or self.discount_type != self.DiscountType.PERCENT
        ):
            errors["maximum_discount_amount"] = "coupon_invalid_value"
        if (
            self.discount_type == self.DiscountType.FIXED
            and value is not None
            and value.is_finite()
            and value != value.to_integral_value()
        ):
            errors["discount_value"] = "coupon_invalid_value"
        if (
            self.price_option_id
            and self.package_id
            and self.price_option.package_id != self.package_id
        ):
            errors["price_option"] = "coupon_not_applicable"
        if (
            self.installment_count
            and self.payment_type == MembershipPaymentType.FULL
            and self.installment_count != 1
        ):
            errors["installment_count"] = "coupon_not_applicable"
        if errors:
            raise ValidationError(errors)


class CSAPurchaseRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_purchase_requests",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    request_code = models.CharField(
        max_length=20, unique=True, default=generate_csa_purchase_request_code, editable=False
    )
    phone_e164 = models.CharField(max_length=13, db_index=True)
    name_snapshot = models.CharField(max_length=150)
    email_snapshot = models.EmailField(max_length=254, null=True, blank=True, editable=False)
    zalo_whatsapp_snapshot = models.CharField(max_length=64, null=True, blank=True, editable=False)
    province_code_snapshot = models.CharField(max_length=2)
    ward_code_snapshot = models.CharField(max_length=5)
    address_snapshot = models.CharField(max_length=500)
    package = models.ForeignKey(
        MembershipPackage, related_name="purchase_requests", on_delete=models.PROTECT
    )
    price_option = models.ForeignKey(
        MembershipPackagePriceOption,
        related_name="purchase_requests",
        on_delete=models.PROTECT,
    )
    payment_plan = models.ForeignKey(
        MembershipPaymentPlan,
        related_name="purchase_requests",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        editable=False,
    )
    package_name_snapshot = models.CharField(max_length=200)
    price_option_name_snapshot = models.CharField(max_length=120)
    duration_months_snapshot = models.PositiveSmallIntegerField()
    amount_snapshot = models.PositiveBigIntegerField()
    price_option_monthly_amount_snapshot = models.PositiveBigIntegerField(
        null=True, blank=True, editable=False
    )
    payment_plan_name_snapshot = models.CharField(
        max_length=120, null=True, blank=True, editable=False
    )
    payment_type_snapshot = models.CharField(
        max_length=12,
        choices=MembershipPaymentType.choices,
        null=True,
        blank=True,
        editable=False,
    )
    total_payment_amount_snapshot = models.DecimalField(
        max_digits=19, decimal_places=0, null=True, blank=True, editable=False
    )
    installment_count_snapshot = models.PositiveSmallIntegerField(
        null=True, blank=True, editable=False
    )
    payment_installments_snapshot = models.JSONField(null=True, blank=True, editable=False)
    coupon = models.ForeignKey(
        CSACoupon, null=True, blank=True, on_delete=models.PROTECT, editable=False
    )
    coupon_snapshot = models.JSONField(null=True, blank=True, editable=False)
    customer_payable_total_snapshot = models.DecimalField(
        max_digits=19, decimal_places=0, null=True, blank=True, editable=False
    )
    currency_snapshot = models.CharField(max_length=3, default="VND")
    bank_bin_snapshot = models.CharField(max_length=6)
    bank_name_snapshot = models.CharField(max_length=160)
    account_number_snapshot = models.CharField(max_length=19)
    account_name_snapshot = models.CharField(max_length=160)
    transfer_content = models.CharField(max_length=80, unique=True, editable=False)
    quote_nonce = models.CharField(
        max_length=64, unique=True, null=True, blank=True, editable=False
    )
    guest_token_hash = models.CharField(max_length=128, blank=True, editable=False)
    payment_config_version = models.PositiveIntegerField()
    terms_version = models.CharField(max_length=64, null=True, blank=True, editable=False)
    terms_locale = models.CharField(
        max_length=2,
        choices=MembershipContentLocale.choices,
        null=True,
        blank=True,
        editable=False,
    )
    terms_snapshot = models.JSONField(null=True, blank=True, editable=False)
    terms_hash = models.CharField(max_length=64, null=True, blank=True, editable=False)
    terms_accepted_at = models.DateTimeField(null=True, blank=True, editable=False)
    terms_acceptance_ip = models.GenericIPAddressField(null=True, blank=True, editable=False)
    terms_acceptance_user_agent = models.TextField(null=True, blank=True, editable=False)
    terms_acceptance_method = models.CharField(
        max_length=16,
        choices=[(CSAContractTermsAcceptanceMethod.ONLINE, "Online")],
        null=True,
        blank=True,
        editable=False,
    )
    status = models.CharField(
        max_length=20,
        choices=CSAPurchaseRequestStatus.choices,
        default=CSAPurchaseRequestStatus.PENDING,
        db_index=True,
    )
    open_slot = models.BooleanField(null=True, default=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True, editable=False)
    expired_at = models.DateTimeField(null=True, blank=True, editable=False)
    expired_reason = models.CharField(max_length=64, blank=True, editable=False)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    rejected_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_purchase_requests_approved",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_purchase_requests_rejected",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    rejection_reason = models.TextField(blank=True)
    refund_status = models.CharField(
        max_length=16,
        choices=CSAPurchaseRefundStatus.choices,
        default=CSAPurchaseRefundStatus.NOT_APPLICABLE,
    )
    refund_marked_at = models.DateTimeField(null=True, blank=True)
    refund_marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_purchase_requests_refunded",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    refund_note = models.CharField(max_length=1000, null=True, blank=True)
    customer = models.ForeignKey(
        CSACustomer,
        related_name="purchase_requests",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    membership = models.OneToOneField(
        "Membership",
        related_name="purchase_request",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE_FIELDS = (
        "user_id",
        "request_code",
        "phone_e164",
        "name_snapshot",
        "email_snapshot",
        "zalo_whatsapp_snapshot",
        "province_code_snapshot",
        "ward_code_snapshot",
        "address_snapshot",
        "package_id",
        "price_option_id",
        "payment_plan_id",
        "package_name_snapshot",
        "price_option_name_snapshot",
        "duration_months_snapshot",
        "amount_snapshot",
        "price_option_monthly_amount_snapshot",
        "payment_plan_name_snapshot",
        "payment_type_snapshot",
        "total_payment_amount_snapshot",
        "installment_count_snapshot",
        "payment_installments_snapshot",
        "coupon_id",
        "coupon_snapshot",
        "customer_payable_total_snapshot",
        "currency_snapshot",
        "bank_bin_snapshot",
        "bank_name_snapshot",
        "account_number_snapshot",
        "account_name_snapshot",
        "transfer_content",
        "quote_nonce",
        "guest_token_hash",
        "payment_config_version",
        "terms_version",
        "terms_locale",
        "terms_snapshot",
        "terms_hash",
        "terms_accepted_at",
        "terms_acceptance_ip",
        "terms_acceptance_user_agent",
        "terms_acceptance_method",
        "expires_at",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        coupon__isnull=True,
                        coupon_snapshot__isnull=True,
                        customer_payable_total_snapshot__isnull=True,
                    )
                    | Q(
                        coupon__isnull=False,
                        coupon_snapshot__isnull=False,
                        customer_payable_total_snapshot__gte=0,
                    )
                ),
                name="memberships_csa_coupon_snapshot_complete",
            ),
            models.UniqueConstraint(
                fields=["phone_e164", "open_slot"],
                name="memberships_csa_purchase_one_open_phone",
            ),
            models.CheckConstraint(
                condition=Q(status__in=CSAPurchaseRequestStatus.values),
                name="memberships_csa_purchase_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(status__in=OPEN_CSA_PURCHASE_REQUEST_STATUSES, open_slot=True)
                    | Q(status__in=TERMINAL_CSA_PURCHASE_REQUEST_STATUSES, open_slot__isnull=True)
                ),
                name="memberships_csa_purchase_open_slot_consistent",
            ),
            models.CheckConstraint(
                condition=Q(duration_months_snapshot__gt=0),
                name="memberships_csa_purchase_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(amount_snapshot__gt=0),
                name="memberships_csa_purchase_amount_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        payment_plan__isnull=True,
                        payment_plan_name_snapshot__isnull=True,
                        payment_type_snapshot__isnull=True,
                        total_payment_amount_snapshot__isnull=True,
                        installment_count_snapshot__isnull=True,
                        payment_installments_snapshot__isnull=True,
                    )
                    | Q(
                        payment_plan__isnull=False,
                        payment_plan_name_snapshot__isnull=False,
                        payment_type_snapshot__in=MembershipPaymentType.values,
                        total_payment_amount_snapshot__gt=0,
                        installment_count_snapshot__gt=0,
                        payment_installments_snapshot__isnull=False,
                        amount_snapshot=models.F("total_payment_amount_snapshot"),
                    )
                ),
                name="memberships_csa_purchase_plan_snapshot_complete",
            ),
            models.CheckConstraint(
                condition=Q(currency_snapshot="VND"),
                name="memberships_csa_purchase_currency_vnd",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        terms_version__isnull=True,
                        terms_locale__isnull=True,
                        terms_snapshot__isnull=True,
                        terms_hash__isnull=True,
                        terms_accepted_at__isnull=True,
                        terms_acceptance_method__isnull=True,
                    )
                    | Q(
                        terms_version__isnull=False,
                        terms_locale__in=MembershipContentLocale.values,
                        terms_snapshot__isnull=False,
                        terms_hash__isnull=False,
                        terms_accepted_at__isnull=False,
                        terms_acceptance_method=CSAContractTermsAcceptanceMethod.ONLINE,
                    )
                ),
                name="memberships_csa_purchase_terms_complete",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        refund_status=CSAPurchaseRefundStatus.NOT_APPLICABLE,
                        refund_marked_at__isnull=True,
                        refund_marked_by__isnull=True,
                        refund_note__isnull=True,
                    )
                    | Q(
                        status=CSAPurchaseRequestStatus.REJECTED,
                        refund_status=CSAPurchaseRefundStatus.PENDING,
                        refund_marked_at__isnull=True,
                        refund_marked_by__isnull=True,
                        refund_note__isnull=True,
                    )
                    | Q(
                        status=CSAPurchaseRequestStatus.REJECTED,
                        refund_status=CSAPurchaseRefundStatus.COMPLETED,
                        refund_marked_at__isnull=False,
                        refund_marked_by__isnull=False,
                    )
                ),
                name="memberships_csa_purchase_refund_consistent",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=CSAPurchaseRequestStatus.EXPIRED,
                        expires_at__isnull=False,
                        expired_at__isnull=False,
                        expired_reason="request_expiry_elapsed",
                        customer__isnull=True,
                        membership__isnull=True,
                        approved_at__isnull=True,
                        approved_by__isnull=True,
                        rejected_at__isnull=True,
                        rejected_by__isnull=True,
                        rejection_reason="",
                    )
                    | Q(
                        ~Q(status=CSAPurchaseRequestStatus.EXPIRED),
                        expired_at__isnull=True,
                        expired_reason="",
                    )
                ),
                name="memberships_csa_purchase_expiry_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        try:
            self.phone_e164 = normalize_vietnam_phone(self.phone_e164)
        except InvalidPhoneNumber as error:
            raise ValidationError({"phone_e164": str(error)}) from error
        self.open_slot = self.status in OPEN_CSA_PURCHASE_REQUEST_STATUSES or None
        self._validate_immutable_fields()
        self.full_clean(validate_unique=False, validate_constraints=False)
        if update_fields := kwargs.get("update_fields"):
            kwargs["update_fields"] = set(update_fields) | {"phone_e164", "open_slot"}
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}
        if self.package_id and self.price_option_id:
            option = self.price_option
            if option.package_id != self.package_id:
                errors["price_option"] = "Price option does not belong to the selected package."
            if (
                self._state.adding
                and not self.quote_nonce
                and (not self.package.is_active or not option.is_active)
            ):
                errors["price_option"] = "Package and price option must be active."
        terms_core = (
            self.terms_version,
            self.terms_locale,
            self.terms_snapshot,
            self.terms_hash,
            self.terms_accepted_at,
            self.terms_acceptance_method,
        )
        if any(value is not None for value in terms_core):
            if any(value is None for value in terms_core):
                errors["terms_snapshot"] = "Terms acceptance snapshot must be complete."
            else:
                from memberships.terms import InvalidCSATerms, validate_csa_terms_snapshot

                try:
                    validate_csa_terms_snapshot(
                        version=self.terms_version,
                        locale=self.terms_locale,
                        content=self.terms_snapshot,
                        content_hash=self.terms_hash,
                    )
                except InvalidCSATerms as error:
                    errors["terms_snapshot"] = str(error)
                if self.terms_acceptance_method != CSAContractTermsAcceptanceMethod.ONLINE:
                    errors["terms_acceptance_method"] = (
                        "Purchase request terms must be accepted online."
                    )
        if errors:
            raise ValidationError(errors)

    def _validate_immutable_fields(self):
        if self._state.adding:
            return
        current = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
        if not current:
            return
        changed = [
            field for field in self.IMMUTABLE_FIELDS if getattr(self, field) != current[field]
        ]
        if changed:
            raise ValidationError(
                {
                    field.removesuffix("_id"): "Purchase request snapshots are immutable."
                    for field in changed
                }
            )


class CSAPurchasePayment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    purchase_request = models.ForeignKey(
        CSAPurchaseRequest,
        related_name="payments",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    # Manual assignment has no purchase request; exactly one owner is required.
    membership = models.ForeignKey(
        "Membership",
        related_name="manual_payments",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    installment_sequence = models.PositiveSmallIntegerField()
    amount_due = models.DecimalField(max_digits=19, decimal_places=0)
    coupon_zero_amount = models.BooleanField(default=False, editable=False)
    amount_paid = models.DecimalField(max_digits=19, decimal_places=0, null=True, blank=True)
    cycle_count = models.PositiveSmallIntegerField()
    currency = models.CharField(max_length=3, default="VND")
    status = models.CharField(
        max_length=12,
        choices=CSAPurchasePaymentStatus.choices,
        default=CSAPurchasePaymentStatus.PENDING,
        db_index=True,
    )
    due_at = models.DateTimeField(null=True, blank=True, db_index=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_payments_confirmed",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_payments_rejected",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    rejection_reason = models.CharField(max_length=1000, blank=True)
    note = models.CharField(max_length=1000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE_FIELDS = (
        "purchase_request_id",
        "membership_id",
        "installment_sequence",
        "amount_due",
        "coupon_zero_amount",
        "cycle_count",
        "currency",
    )

    class Meta:
        ordering = ["installment_sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["purchase_request", "installment_sequence"],
                name="memberships_payment_request_sequence_unique",
            ),
            models.UniqueConstraint(
                fields=["membership", "installment_sequence"],
                name="memberships_payment_manual_sequence_unique",
            ),
            models.CheckConstraint(
                condition=(
                    Q(purchase_request__isnull=False, membership__isnull=True)
                    | Q(purchase_request__isnull=True, membership__isnull=False)
                ),
                name="memberships_payment_exactly_one_owner",
            ),
            models.CheckConstraint(
                condition=Q(installment_sequence__gt=0),
                name="memberships_payment_sequence_positive",
            ),
            models.CheckConstraint(
                condition=Q(amount_due__gt=0) | Q(amount_due=0, coupon_zero_amount=True),
                name="memberships_payment_due_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(amount_paid__isnull=True)
                    | Q(amount_paid__gt=0)
                    | Q(amount_paid=0, amount_due=0, coupon_zero_amount=True)
                ),
                name="memberships_payment_paid_positive",
            ),
            models.CheckConstraint(
                condition=Q(cycle_count__gte=1), name="memberships_payment_cycles_positive"
            ),
            models.CheckConstraint(condition=Q(currency="VND"), name="memberships_payment_vnd"),
            models.CheckConstraint(
                condition=Q(status__in=CSAPurchasePaymentStatus.values),
                name="memberships_payment_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=CSAPurchasePaymentStatus.CONFIRMED,
                        amount_paid=models.F("amount_due"),
                        confirmed_at__isnull=False,
                        confirmed_by__isnull=False,
                        rejected_at__isnull=True,
                        rejected_by__isnull=True,
                        rejection_reason="",
                    )
                    | Q(
                        status=CSAPurchasePaymentStatus.REJECTED,
                        amount_paid__isnull=True,
                        confirmed_at__isnull=True,
                        confirmed_by__isnull=True,
                        rejected_at__isnull=False,
                        rejected_by__isnull=False,
                    )
                    & ~Q(rejection_reason="")
                    | Q(
                        status__in=[
                            CSAPurchasePaymentStatus.PENDING,
                            CSAPurchasePaymentStatus.REPORTED,
                            CSAPurchasePaymentStatus.OVERDUE,
                        ],
                        amount_paid__isnull=True,
                        confirmed_at__isnull=True,
                        confirmed_by__isnull=True,
                        rejected_at__isnull=True,
                        rejected_by__isnull=True,
                        rejection_reason="",
                    )
                ),
                name="memberships_payment_state_consistent",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            current = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("status", *self.IMMUTABLE_FIELDS)
                .first()
            )
            if current:
                if (
                    current["status"]
                    in {
                        CSAPurchasePaymentStatus.CONFIRMED,
                        CSAPurchasePaymentStatus.REJECTED,
                    }
                    and self.status != current["status"]
                ):
                    raise ValidationError({"status": "Terminal payment cannot change status."})
                changed = [
                    field
                    for field in self.IMMUTABLE_FIELDS
                    if getattr(self, field) != current[field]
                ]
                if changed:
                    raise ValidationError(
                        {field: "Payment schedule snapshots are immutable." for field in changed}
                    )
        self.full_clean(validate_unique=False, validate_constraints=False)
        return super().save(*args, **kwargs)


class CSATrackerSession(models.Model):
    token_hash = models.CharField(primary_key=True, max_length=64, editable=False)
    purchase_request = models.ForeignKey(
        CSAPurchaseRequest,
        related_name="tracker_sessions",
        on_delete=models.CASCADE,
        editable=False,
    )
    expires_at = models.DateTimeField(db_index=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


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
    customer = models.ForeignKey(CSACustomer, related_name="memberships", on_delete=models.PROTECT)
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
    pricing_basis = models.CharField(
        max_length=12,
        choices=(("monthly", "Monthly"), ("payment_plan", "Payment plan")),
        default="monthly",
    )
    duration_months_snapshot = models.PositiveSmallIntegerField()
    quota_policy_snapshot = models.CharField(max_length=16, choices=QuotaPolicy.choices)
    farmbrite_order_reference = models.CharField(max_length=120, unique=True)
    farmbrite_order_reference_key = models.CharField(max_length=240, unique=True, editable=False)
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
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="memberships_revoked",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    revoke_reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-activated_at", "-id"]
        constraints = [
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
                condition=(
                    Q(
                        pricing_basis="monthly",
                        total_price_vnd_snapshot=(
                            models.F("monthly_price_vnd_snapshot")
                            * models.F("duration_months_snapshot")
                        ),
                    )
                    | Q(pricing_basis="payment_plan")
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
                    | Q(
                        status__in=[MembershipStatus.ENDED, MembershipStatus.REVOKED],
                        current_slot__isnull=True,
                    )
                ),
                name="memberships_current_slot_consistent",
            ),
        ]


class CSAContract(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_code = models.CharField(max_length=20, unique=True, editable=False)
    source = models.CharField(max_length=20, choices=CSAContractSource.choices, editable=False)
    membership = models.OneToOneField(
        Membership,
        related_name="csa_contract",
        on_delete=models.PROTECT,
        editable=False,
    )
    purchase_request = models.OneToOneField(
        CSAPurchaseRequest,
        related_name="csa_contract",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        editable=False,
    )
    status = models.CharField(
        max_length=12,
        choices=CSAContractStatus.choices,
        default=CSAContractStatus.ACTIVE,
        db_index=True,
    )

    member_name_snapshot = models.CharField(max_length=150, editable=False)
    phone_e164_snapshot = models.CharField(max_length=13, editable=False)
    email_snapshot = models.EmailField(max_length=254, null=True, blank=True, editable=False)
    zalo_whatsapp_snapshot = models.CharField(max_length=64, null=True, blank=True, editable=False)
    province_code_snapshot = models.CharField(max_length=2, editable=False)
    ward_code_snapshot = models.CharField(max_length=5, editable=False)
    address_snapshot = models.CharField(max_length=500, editable=False)

    package_name_snapshot = models.CharField(max_length=200, editable=False)
    price_option_name_snapshot = models.CharField(max_length=120, editable=False)
    duration_months_snapshot = models.PositiveSmallIntegerField(editable=False)
    amount_snapshot = models.PositiveBigIntegerField(editable=False)
    currency_snapshot = models.CharField(max_length=3, editable=False)
    quota_snapshot = models.JSONField(null=True, blank=True, editable=False)
    payment_snapshot = models.JSONField(null=True, blank=True, editable=False)

    terms_version = models.CharField(max_length=64, editable=False)
    terms_locale = models.CharField(
        max_length=2, choices=MembershipContentLocale.choices, editable=False
    )
    terms_snapshot = models.JSONField(editable=False)
    terms_accepted_at = models.DateTimeField(editable=False)
    terms_acceptance_method = models.CharField(
        max_length=16,
        choices=CSAContractTermsAcceptanceMethod.choices,
        editable=False,
    )
    terms_acceptance_ip = models.GenericIPAddressField(null=True, blank=True, editable=False)
    terms_acceptance_user_agent = models.TextField(null=True, blank=True, editable=False)

    start_date = models.DateField(editable=False)
    end_date = models.DateField(editable=False)
    issued_at = models.DateTimeField(editable=False)

    provider_version = models.CharField(
        max_length=64,
        default="NFV_PROVIDER_V1",
        editable=False,
    )
    terms_snapshot_vi = models.JSONField(null=True, blank=True, editable=False)
    terms_snapshot_en = models.JSONField(null=True, blank=True, editable=False)

    pdf_vi_storage_key = models.CharField(max_length=500, null=True, blank=True, editable=False)
    pdf_en_storage_key = models.CharField(max_length=500, null=True, blank=True, editable=False)
    pdf_vi_sha256 = models.CharField(max_length=64, null=True, blank=True, editable=False)
    pdf_en_sha256 = models.CharField(max_length=64, null=True, blank=True, editable=False)
    original_pdf_vi_storage_key = models.CharField(
        max_length=500, null=True, blank=True, editable=False
    )
    original_pdf_en_storage_key = models.CharField(
        max_length=500, null=True, blank=True, editable=False
    )
    original_pdf_vi_sha256 = models.CharField(max_length=64, null=True, blank=True, editable=False)
    original_pdf_en_sha256 = models.CharField(max_length=64, null=True, blank=True, editable=False)
    pdf_generated_at = models.DateTimeField(null=True, blank=True, editable=False)
    pdf_version = models.PositiveIntegerField(default=1, editable=False)
    current_version = models.ForeignKey(
        "CSAContractVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="current_for_contracts",
        editable=False,
    )

    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="csa_contracts_revoked",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    revoked_by_name_snapshot = models.CharField(
        max_length=254, null=True, blank=True, editable=False
    )
    revocation_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    IMMUTABLE_FIELDS = (
        "reference_code",
        "source",
        "membership_id",
        "purchase_request_id",
        "member_name_snapshot",
        "phone_e164_snapshot",
        "email_snapshot",
        "zalo_whatsapp_snapshot",
        "province_code_snapshot",
        "ward_code_snapshot",
        "address_snapshot",
        "package_name_snapshot",
        "price_option_name_snapshot",
        "duration_months_snapshot",
        "amount_snapshot",
        "currency_snapshot",
        "quota_snapshot",
        "payment_snapshot",
        "terms_version",
        "terms_locale",
        "terms_snapshot",
        "terms_accepted_at",
        "terms_acceptance_method",
        "terms_acceptance_ip",
        "terms_acceptance_user_agent",
        "start_date",
        "end_date",
        "issued_at",
        "provider_version",
        "terms_snapshot_vi",
        "terms_snapshot_en",
    )
    REVOCATION_IMMUTABLE_FIELDS = (
        "status",
        "revoked_at",
        "revoked_by_id",
        "revoked_by_name_snapshot",
        "revocation_reason",
    )

    class Meta:
        ordering = ["-issued_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(source__in=CSAContractSource.values),
                name="memberships_csa_contract_source_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=CSAContractStatus.values),
                name="memberships_csa_contract_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(terms_locale__in=MembershipContentLocale.values),
                name="memberships_csa_contract_terms_locale_valid",
            ),
            models.CheckConstraint(
                condition=Q(terms_acceptance_method__in=CSAContractTermsAcceptanceMethod.values),
                name="memberships_csa_contract_acceptance_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source=CSAContractSource.ONLINE, purchase_request__isnull=False)
                    | Q(source=CSAContractSource.ADMIN_MANUAL)
                ),
                name="memberships_csa_contract_online_request",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status=CSAContractStatus.ACTIVE,
                        revoked_at__isnull=True,
                        revoked_by__isnull=True,
                        revocation_reason="",
                    )
                    | (
                        Q(
                            status=CSAContractStatus.REVOKED,
                            revoked_at__isnull=False,
                            revoked_by__isnull=False,
                        )
                        & ~Q(revocation_reason="")
                    )
                ),
                name="memberships_csa_contract_revoke_consistent",
            ),
            models.CheckConstraint(
                condition=Q(duration_months_snapshot__gte=1),
                name="memberships_csa_contract_duration_positive",
            ),
            models.CheckConstraint(
                condition=Q(amount_snapshot__gte=0),
                name="memberships_csa_contract_amount_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(end_date__gt=models.F("start_date")),
                name="memberships_csa_contract_dates_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(member_name_snapshot="")
                    & ~Q(phone_e164_snapshot="")
                    & ~Q(province_code_snapshot="")
                    & ~Q(ward_code_snapshot="")
                    & ~Q(address_snapshot="")
                ),
                name="memberships_csa_contract_identity_present",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(package_name_snapshot="")
                    & ~Q(price_option_name_snapshot="")
                    & ~Q(currency_snapshot="")
                ),
                name="memberships_csa_contract_package_present",
            ),
            models.CheckConstraint(
                condition=~Q(terms_version=""),
                name="memberships_csa_contract_terms_present",
            ),
            models.CheckConstraint(
                condition=Q(pdf_version__gte=1),
                name="memberships_csa_contract_pdf_version_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        pdf_vi_storage_key__isnull=True,
                        pdf_en_storage_key__isnull=True,
                        pdf_vi_sha256__isnull=True,
                        pdf_en_sha256__isnull=True,
                        original_pdf_vi_storage_key__isnull=True,
                        original_pdf_en_storage_key__isnull=True,
                        original_pdf_vi_sha256__isnull=True,
                        original_pdf_en_sha256__isnull=True,
                        pdf_generated_at__isnull=True,
                    )
                    | Q(
                        pdf_vi_storage_key__isnull=False,
                        pdf_en_storage_key__isnull=False,
                        pdf_vi_sha256__isnull=False,
                        pdf_en_sha256__isnull=False,
                        original_pdf_vi_storage_key__isnull=False,
                        original_pdf_en_storage_key__isnull=False,
                        original_pdf_vi_sha256__isnull=False,
                        original_pdf_en_sha256__isnull=False,
                        pdf_generated_at__isnull=False,
                    )
                ),
                name="memberships_csa_contract_pdf_metadata_complete",
            ),
        ]

    def save(self, *args, **kwargs):
        try:
            self.phone_e164_snapshot = normalize_vietnam_phone(self.phone_e164_snapshot)
        except InvalidPhoneNumber as error:
            raise ValidationError({"phone_e164_snapshot": str(error)}) from error
        self._validate_immutable_fields()
        self._validate_revocation_immutability()
        if (
            self.status == CSAContractStatus.REVOKED
            and self.revoked_by_id
            and not self.revoked_by_name_snapshot
        ):
            self.revoked_by_name_snapshot = self.revoked_by.name or self.revoked_by.email
        self.full_clean(validate_unique=False, validate_constraints=False)
        if update_fields := kwargs.get("update_fields"):
            kwargs["update_fields"] = set(update_fields) | {"phone_e164_snapshot"}
            if self.status == CSAContractStatus.REVOKED and self.revoked_by_name_snapshot:
                kwargs["update_fields"].add("revoked_by_name_snapshot")
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}
        required_text = (
            "reference_code",
            "member_name_snapshot",
            "province_code_snapshot",
            "ward_code_snapshot",
            "address_snapshot",
            "package_name_snapshot",
            "price_option_name_snapshot",
            "currency_snapshot",
            "terms_version",
            "provider_version",
        )
        for field in required_text:
            if not str(getattr(self, field, "") or "").strip():
                errors[field] = "This contract snapshot field is required."
        if self.terms_snapshot in (None, "", {}, []):
            errors["terms_snapshot"] = "Terms snapshot is required."
        if self.source == CSAContractSource.ONLINE:
            if not self.purchase_request_id:
                errors["purchase_request"] = "Online contracts require a purchase request."
            else:
                purchase_request = self.purchase_request
                if purchase_request.status != CSAPurchaseRequestStatus.APPROVED:
                    errors["purchase_request"] = (
                        "Online contracts require an approved purchase request."
                    )
                elif purchase_request.membership_id != self.membership_id:
                    errors["purchase_request"] = (
                        "Purchase request does not belong to the contract membership."
                    )
                if self.reference_code != purchase_request.request_code:
                    errors["reference_code"] = (
                        "Online contract reference must match the purchase request code."
                    )
        if self.source == CSAContractSource.ONLINE and (
            self.terms_acceptance_method != CSAContractTermsAcceptanceMethod.ONLINE
        ):
            errors["terms_acceptance_method"] = "Online contracts require online acceptance."
        if self.source == CSAContractSource.ADMIN_MANUAL and (
            self.terms_acceptance_method != CSAContractTermsAcceptanceMethod.IN_STORE
        ):
            errors["terms_acceptance_method"] = "Manual contracts require in-store acceptance."
        if self.status == CSAContractStatus.ACTIVE and (
            self.revoked_at or self.revoked_by_id or self.revocation_reason
        ):
            errors["status"] = "Active contracts cannot contain revocation data."
        if self.status == CSAContractStatus.REVOKED and (
            not self.revoked_at or not self.revoked_by_id or not self.revocation_reason.strip()
        ):
            errors["status"] = "Revoked contracts require time, actor, and reason."
        if self.current_version_id and self.current_version.contract_id != self.pk:
            errors["current_version"] = "Current version must belong to this contract."
        if errors:
            raise ValidationError(errors)

    def _validate_immutable_fields(self):
        if self._state.adding:
            return
        current = type(self).objects.filter(pk=self.pk).values(*self.IMMUTABLE_FIELDS).first()
        if not current:
            return
        changed = [
            field for field in self.IMMUTABLE_FIELDS if getattr(self, field) != current[field]
        ]
        if changed:
            raise ValidationError(
                {
                    field.removesuffix("_id"): "Contract identity and snapshots are immutable."
                    for field in changed
                }
            )

    def _validate_revocation_immutability(self):
        if self._state.adding:
            return
        current = (
            type(self).objects.filter(pk=self.pk).values(*self.REVOCATION_IMMUTABLE_FIELDS).first()
        )
        if not current or current["status"] != CSAContractStatus.REVOKED:
            return
        changed = [
            field
            for field in self.REVOCATION_IMMUTABLE_FIELDS
            if getattr(self, field) != current[field]
        ]
        if changed:
            raise ValidationError(
                {
                    field.removesuffix("_id"): (
                        "Contract revocation data is immutable after revocation."
                    )
                    for field in changed
                }
            )


class ImmutableContractVersionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Published CSA contract versions are immutable.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Published CSA contract versions are immutable.")

    def delete(self):
        raise ValidationError("Published CSA contract versions cannot be deleted.")


class CSAContractVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    contract = models.ForeignKey(CSAContract, related_name="versions", on_delete=models.PROTECT)
    version_number = models.PositiveIntegerField()
    version_reason = models.CharField(max_length=24, choices=CSAContractVersionReason.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="csa_contract_versions_created",
    )
    created_by_name_snapshot = models.CharField(max_length=254, blank=True)
    contract_status = models.CharField(max_length=12, choices=CSAContractStatus.choices)
    membership_status = models.CharField(max_length=12, choices=MembershipStatus.choices)
    issued_at = models.DateTimeField()
    start_date = models.DateField()
    end_date = models.DateField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.TextField(blank=True)
    revoked_by_name_snapshot = models.CharField(max_length=254, blank=True)
    package_name_snapshot = models.CharField(max_length=200)
    price_option_name_snapshot = models.CharField(max_length=120)
    duration_months_snapshot = models.PositiveSmallIntegerField()
    amount_snapshot = models.DecimalField(max_digits=19, decimal_places=0)
    currency_snapshot = models.CharField(max_length=3, default="VND")
    payment_type = models.CharField(max_length=12, choices=MembershipPaymentType.choices)
    pricing_snapshot = models.JSONField(null=True, blank=True, editable=False)
    total_payment_amount = models.DecimalField(max_digits=19, decimal_places=0)
    paid_amount = models.DecimalField(max_digits=19, decimal_places=0)
    remaining_amount = models.DecimalField(max_digits=19, decimal_places=0)
    confirmed_installment_count = models.PositiveSmallIntegerField()
    installment_count = models.PositiveSmallIntegerField()
    paid_cycles = models.PositiveSmallIntegerField()
    total_cycles = models.PositiveSmallIntegerField()
    payment_schedule_snapshot = models.JSONField()
    pdf_vi_storage_key = models.CharField(max_length=500)
    pdf_en_storage_key = models.CharField(max_length=500)
    pdf_vi_sha256 = models.CharField(max_length=64)
    pdf_en_sha256 = models.CharField(max_length=64)
    pdf_generated_at = models.DateTimeField()

    objects = ImmutableContractVersionQuerySet.as_manager()

    class Meta:
        ordering = ["version_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["contract", "version_number"],
                name="memberships_contract_version_unique",
            ),
            models.CheckConstraint(
                condition=Q(version_number__gt=0), name="memberships_contract_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(version_reason__in=CSAContractVersionReason.values),
                name="memberships_contract_version_reason_valid",
            ),
            models.CheckConstraint(
                condition=Q(contract_status__in=CSAContractStatus.values),
                name="memberships_contract_version_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(payment_type__in=MembershipPaymentType.values),
                name="memberships_contract_version_payment_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(amount_snapshot__gt=0)
                & Q(total_payment_amount__gte=0)
                & Q(paid_amount__gte=0)
                & Q(remaining_amount__gte=0)
                & Q(total_payment_amount=models.F("paid_amount") + models.F("remaining_amount")),
                name="memberships_contract_version_amounts_valid",
            ),
            models.CheckConstraint(
                condition=Q(installment_count__gt=0)
                & Q(total_cycles__gt=0)
                & Q(confirmed_installment_count__lte=models.F("installment_count"))
                & Q(paid_cycles__lte=models.F("total_cycles")),
                name="memberships_contract_version_cycles_valid",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Published CSA contract versions are immutable.")
        self.full_clean(validate_unique=False, validate_constraints=False)
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Published CSA contract versions cannot be deleted.")


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
    customer = models.ForeignKey(CSACustomer, related_name="usage_orders", on_delete=models.PROTECT)
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
            models.Index(fields=["customer", "-created_at"]),
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
