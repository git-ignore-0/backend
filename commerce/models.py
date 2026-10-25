import uuid

from django.db import models
from django.db.models import Q

PUBLIC_STORE_MAX_PRICE_VND = 1_000_000_000


class CatalogTreeLock(models.Model):
    """The shared row serialized by catalog mutations that depend on the category tree."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)

    def __str__(self):
        return "commerce-category-tree"


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
    csa_quota_cost = models.PositiveIntegerField(default=0)
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
