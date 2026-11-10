import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q

PUBLIC_STORE_MAX_PRICE_VND = 1_000_000_000


class CatalogTreeLock(models.Model):
    """The shared row serialized by catalog mutations that depend on the category tree."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    def __str__(self):
        return "commerce-category-tree"


class ShippingProvinceRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    province_code = models.CharField(max_length=2, unique=True)
    default_shipping_fee_vnd = models.PositiveBigIntegerField(default=0)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["province_code", "id"]

    def __str__(self):
        return self.province_code


class ShippingArea(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    province_code = models.CharField(max_length=2)
    ward_code = models.CharField(max_length=5, unique=True)
    shipping_fee_vnd = models.PositiveBigIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["province_code", "ward_code", "id"]

    def __str__(self):
        return self.ward_code


class ShippingAddress(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="shipping_address",
        on_delete=models.CASCADE,
    )
    recipient_name = models.CharField(max_length=200)
    phone_number = models.CharField(max_length=32)
    province_code = models.CharField(max_length=2)
    ward_code = models.CharField(max_length=5)
    address = models.CharField(max_length=500)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user_id"]

    def __str__(self):
        return f"{self.user_id}: {self.ward_code}"


class Category(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    parent = models.ForeignKey(
        "self", null=True, blank=True, related_name="children", on_delete=models.PROTECT
    )
    slug = models.SlugField(max_length=180, unique=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "slug", "id"]
        constraints = [
            models.CheckConstraint(
                condition=~Q(id=models.F("parent_id")), name="commerce_category_not_self_parent"
            ),
        ]

    def __str__(self):
        return self.slug


class CategoryTranslation(models.Model):
    category = models.ForeignKey(Category, related_name="translations", on_delete=models.CASCADE)
    locale = models.CharField(max_length=2, choices=[("vi", "Vietnamese"), ("en", "English")])
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["locale", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["category", "locale"], name="commerce_category_translation_unique"
            ),
            models.CheckConstraint(
                condition=Q(locale__in=["vi", "en"]),
                name="commerce_category_translation_locale_valid",
            ),
        ]

    def __str__(self):
        return f"{self.category.slug} ({self.locale})"


class Product(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    category = models.ForeignKey(Category, related_name="products", on_delete=models.PROTECT)
    csa_quota_group = models.ForeignKey(
        "memberships.Product",
        null=True,
        blank=True,
        related_name="store_products",
        on_delete=models.PROTECT,
    )
    sku = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=180, unique=True)
    unit_size = models.DecimalField(max_digits=10, decimal_places=3)
    unit_label_vi = models.CharField(max_length=40)
    unit_label_en = models.CharField(max_length=40)
    retail_price_vnd = models.PositiveBigIntegerField()
    csa_quota_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    primary_image = models.ForeignKey(
        "content.MediaAsset",
        null=True,
        blank=True,
        related_name="store_products",
        on_delete=models.PROTECT,
    )
    is_active = models.BooleanField(default=True)
    is_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(unit_size__gt=0), name="commerce_product_unit_positive"
            ),
            models.CheckConstraint(
                condition=Q(retail_price_vnd__gt=0), name="commerce_product_price_positive"
            ),
            models.CheckConstraint(
                condition=(
                    Q(csa_quota_group__isnull=True, csa_quota_cost=0)
                    | Q(csa_quota_group__isnull=False, csa_quota_cost__gt=0)
                ),
                name="commerce_product_quota_consistent",
            ),
            models.CheckConstraint(
                condition=Q(is_published=False) | Q(is_active=True),
                name="commerce_product_published_active",
            ),
        ]

    def __str__(self):
        return self.sku


class ProductTranslation(models.Model):
    product = models.ForeignKey(Product, related_name="translations", on_delete=models.CASCADE)
    locale = models.CharField(max_length=2, choices=[("vi", "Vietnamese"), ("en", "English")])
    name = models.CharField(max_length=200)
    short_description = models.CharField(max_length=800, blank=True)
    description = models.TextField(blank=True)
    description_document = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["locale", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "locale"], name="commerce_product_translation_unique"
            ),
            models.CheckConstraint(
                condition=Q(locale__in=["vi", "en"]),
                name="commerce_product_translation_locale_valid",
            ),
        ]

    def __str__(self):
        return f"{self.product.sku} ({self.locale})"


class StoreOrderStatus(models.TextChoices):
    PAYMENT_PENDING = "payment_pending", "Payment pending"
    PENDING_REVIEW = "pending_review", "Pending review"
    PENDING_DELIVERY = "pending_delivery", "Pending delivery"
    DELIVERED = "delivered", "Delivered"
    CANCELLED = "cancelled", "Cancelled"


class StorePaymentStatus(models.TextChoices):
    NOT_REQUIRED = "not_required", "Not required"
    UNCONFIRMED = "unconfirmed", "Unconfirmed"
    RECEIVED = "received", "Received"
    REFUND_REQUIRED = "refund_required", "Refund required"
    REFUNDED = "refunded", "Refunded"


class StoreStockMovementKind(models.TextChoices):
    SALE = "sale", "Sale"
    RESTOCK = "restock", "Restock"


class StoreOrder(models.Model):
    referral_rewards_eligible = models.BooleanField(default=False, editable=False)
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reference_code = models.CharField(max_length=24, unique=True, editable=False)
    buyer_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_orders",
        on_delete=models.PROTECT,
    )
    create_idempotency_key = models.UUIDField(unique=True, editable=False)
    request_fingerprint = models.CharField(max_length=64, editable=False)
    locale = models.CharField(max_length=2, choices=[("vi", "Vietnamese"), ("en", "English")])
    recipient_name_snapshot = models.CharField(max_length=200)
    phone_e164_snapshot = models.CharField(max_length=16)
    province_code_snapshot = models.CharField(max_length=2)
    ward_code_snapshot = models.CharField(max_length=5)
    address_snapshot = models.CharField(max_length=500)
    status = models.CharField(
        max_length=20,
        choices=StoreOrderStatus.choices,
        default=StoreOrderStatus.PAYMENT_PENDING,
    )
    payment_status = models.CharField(
        max_length=20,
        choices=StorePaymentStatus.choices,
        default=StorePaymentStatus.UNCONFIRMED,
    )
    coupon = models.ForeignKey(
        "memberships.CSACoupon", null=True, blank=True, editable=False, on_delete=models.PROTECT
    )
    coupon_snapshot = models.JSONField(null=True, blank=True, editable=False)
    discount_amount_vnd = models.PositiveBigIntegerField(default=0, editable=False)
    subtotal_vnd = models.PositiveBigIntegerField()
    shipping_fee_vnd = models.PositiveBigIntegerField()
    total_vnd = models.PositiveBigIntegerField()
    shipping_discount_reason = models.CharField(max_length=80, blank=True)
    bank_bin_snapshot = models.CharField(max_length=6, blank=True)
    bank_name_snapshot = models.CharField(max_length=160, blank=True)
    account_number_snapshot = models.CharField(max_length=19, blank=True)
    account_name_snapshot = models.CharField(max_length=160, blank=True)
    payment_config_version = models.PositiveIntegerField(null=True, blank=True)
    transfer_content = models.CharField(max_length=80, unique=True, null=True, blank=True)
    guest_access_token_hash = models.CharField(max_length=64, blank=True, editable=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    transfer_reported_at = models.DateTimeField(null=True, blank=True)
    payment_received_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_orders_approved",
        on_delete=models.SET_NULL,
    )
    delivered_at = models.DateTimeField(null=True, blank=True)
    delivered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_orders_delivered",
        on_delete=models.SET_NULL,
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_orders_cancelled",
        on_delete=models.SET_NULL,
    )
    cancellation_reason = models.TextField(blank=True)
    reconciliation_note = models.TextField(blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refunded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_orders_refunded",
        on_delete=models.SET_NULL,
    )
    refund_amount_vnd = models.PositiveBigIntegerField(null=True, blank=True)
    refund_note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "expires_at"], name="commerce_order_expiry_idx"),
            models.Index(fields=["buyer_user", "-created_at"], name="commerce_order_buyer_idx"),
            models.Index(fields=["reference_code"], name="commerce_order_reference_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(status__in=StoreOrderStatus.values),
                name="commerce_store_order_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(payment_status__in=StorePaymentStatus.values),
                name="commerce_store_payment_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    total_vnd=models.F("subtotal_vnd")
                    - models.F("discount_amount_vnd")
                    + models.F("shipping_fee_vnd")
                ),
                name="commerce_store_order_total_exact",
            ),
            models.CheckConstraint(
                condition=Q(
                    discount_amount_vnd__gte=0, discount_amount_vnd__lte=models.F("subtotal_vnd")
                ),
                name="store_discount_within_subtotal",
            ),
            models.CheckConstraint(
                condition=(
                    Q(coupon__isnull=True, coupon_snapshot__isnull=True, discount_amount_vnd=0)
                    | Q(coupon__isnull=False, coupon_snapshot__isnull=False)
                ),
                name="store_coupon_snapshot_complete",
            ),
        ]

    def __str__(self):
        return self.reference_code


class StoreOrderLine(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(StoreOrder, related_name="lines", on_delete=models.PROTECT)
    product = models.ForeignKey(Product, related_name="store_order_lines", on_delete=models.PROTECT)
    position = models.PositiveSmallIntegerField()
    sku_snapshot = models.CharField(max_length=80)
    name_snapshot = models.CharField(max_length=200)
    unit_size_snapshot = models.DecimalField(max_digits=10, decimal_places=3)
    unit_label_snapshot = models.CharField(max_length=40)
    unit_price_vnd_snapshot = models.PositiveBigIntegerField()
    quantity = models.PositiveIntegerField()
    quota_group = models.ForeignKey(
        "memberships.Product",
        null=True,
        blank=True,
        related_name="store_order_lines",
        on_delete=models.PROTECT,
    )
    quota_cost_snapshot = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    quota_quantity = models.PositiveIntegerField(default=0)
    quota_units = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    cash_quantity = models.PositiveIntegerField()
    cash_subtotal_vnd = models.PositiveBigIntegerField()

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "position"], name="commerce_order_line_pos_unique"
            ),
            models.UniqueConstraint(
                fields=["order", "product"], name="commerce_order_product_unique"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="commerce_order_line_qty_positive"
            ),
            models.CheckConstraint(
                condition=Q(quota_cost_snapshot__gte=0, quota_units__gte=0),
                name="commerce_order_line_quota_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(cash_quantity=models.F("quantity") - models.F("quota_quantity")),
                name="commerce_order_line_split_exact",
            ),
        ]

    def __str__(self):
        return f"{self.order.reference_code}:{self.position}"


class StoreStockReservation(models.Model):
    line = models.ForeignKey(
        StoreOrderLine, related_name="stock_reservations", on_delete=models.PROTECT
    )
    product = models.ForeignKey(
        Product, related_name="store_stock_reservations", on_delete=models.PROTECT
    )
    quantity = models.PositiveIntegerField()
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["product", "released_at"], name="commerce_stock_reserve_idx")
        ]
        constraints = [
            models.UniqueConstraint(fields=["line"], name="commerce_stock_reserve_line_unique"),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="commerce_stock_reserve_positive"
            ),
        ]

    def __str__(self):
        return f"{self.line_id}:{self.quantity}"


class StoreQuotaReservation(models.Model):
    line = models.ForeignKey(
        StoreOrderLine, related_name="quota_reservations", on_delete=models.PROTECT
    )
    cycle_quota = models.ForeignKey(
        "memberships.MembershipCycleQuota",
        related_name="store_reservations",
        on_delete=models.PROTECT,
    )
    units = models.DecimalField(max_digits=14, decimal_places=2)
    released_at = models.DateTimeField(null=True, blank=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "cycle_quota"], name="commerce_quota_reserve_unique"
            ),
            models.CheckConstraint(
                condition=Q(units__gt=0), name="commerce_quota_reserve_positive"
            ),
        ]

    def __str__(self):
        return f"{self.line_id}:{self.cycle_quota_id}:{self.units}"


class StoreStockAllocation(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    line = models.ForeignKey(
        StoreOrderLine, related_name="stock_allocations", on_delete=models.PROTECT
    )
    batch = models.ForeignKey(
        "farms.FarmProductStockBatch", related_name="store_allocations", on_delete=models.PROTECT
    )
    quantity = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "batch"], name="commerce_stock_allocate_unique"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="commerce_stock_allocate_positive"
            ),
        ]

    def __str__(self):
        return f"{self.line_id}:{self.batch_id}:{self.quantity}"


class StoreStockMovement(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    allocation = models.ForeignKey(
        StoreStockAllocation, related_name="movements", on_delete=models.PROTECT
    )
    batch = models.ForeignKey(
        "farms.FarmProductStockBatch",
        related_name="store_stock_movements",
        on_delete=models.PROTECT,
    )
    line = models.ForeignKey(
        StoreOrderLine, related_name="stock_movements", on_delete=models.PROTECT
    )
    kind = models.CharField(max_length=12, choices=StoreStockMovementKind.choices)
    quantity = models.PositiveIntegerField()
    idempotency_key = models.UUIDField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_stock_movements",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["allocation", "kind", "idempotency_key"],
                name="commerce_stock_move_idempotent",
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="commerce_stock_move_positive"
            ),
        ]

    def __str__(self):
        return f"{self.kind}:{self.batch_id}:{self.quantity}"


class StoreOrderAction(models.Model):
    order = models.ForeignKey(StoreOrder, related_name="actions", on_delete=models.PROTECT)
    action = models.CharField(max_length=32)
    idempotency_key = models.UUIDField()
    request_fingerprint = models.CharField(max_length=64)
    context = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="store_order_actions",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["order", "action", "idempotency_key"],
                name="commerce_store_action_idempotent",
            )
        ]

    def __str__(self):
        return f"{self.order_id}:{self.action}:{self.idempotency_key}"


class StoreGuestLookupSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    order = models.ForeignKey(
        StoreOrder, related_name="guest_lookup_sessions", on_delete=models.CASCADE
    )
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["token_hash", "expires_at"], name="commerce_guest_lookup_idx")
        ]

    def __str__(self):
        return f"{self.order_id}:{self.expires_at.isoformat()}"
