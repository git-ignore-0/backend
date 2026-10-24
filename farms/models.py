import uuid

from django.db import models
from django.db.models import Q


class Farm(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name_en = models.CharField(max_length=240)
    name_vi = models.CharField(max_length=240)
    location_en = models.CharField(max_length=240)
    location_vi = models.CharField(max_length=240)
    description_en = models.TextField(blank=True, default="")
    description_vi = models.TextField(blank=True, default="")
    image = models.ForeignKey(
        "content.MediaAsset",
        related_name="farms",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    is_active = models.BooleanField(default=True)
    legacy_tracker_owned = models.BooleanField(default=False)
    tracker_profile_revision = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name_en", "id"]

    def __str__(self):
        return self.name_en


class FarmPhoto(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey(Farm, related_name="photos", on_delete=models.CASCADE)
    image = models.ForeignKey(
        "content.MediaAsset", related_name="farm_photos", on_delete=models.PROTECT
    )
    sort_order = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "created_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["farm", "sort_order"], name="farms_photo_order_unique"),
            models.UniqueConstraint(fields=["farm", "image"], name="farms_photo_image_unique"),
        ]

    def __str__(self):
        return f"{self.farm_id}:{self.sort_order}"


class FarmProduct(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm = models.ForeignKey(Farm, related_name="products", on_delete=models.PROTECT)
    product = models.ForeignKey(
        "commerce.Product", related_name="farm_offers", on_delete=models.PROTECT
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["product__sku", "id"]
        constraints = [
            models.UniqueConstraint(fields=["farm", "product"], name="farms_product_unique"),
        ]

    def __str__(self):
        return f"{self.farm_id}:{self.product_id}"


class FarmWeeklySupply(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    farm_product = models.ForeignKey(
        FarmProduct, related_name="weekly_supplies", on_delete=models.PROTECT
    )
    week_start = models.DateField()
    quantity_units = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-week_start", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["farm_product", "week_start"], name="farms_weekly_supply_unique"
            ),
            models.CheckConstraint(
                condition=Q(quantity_units__gte=0), name="farms_weekly_supply_nonnegative"
            ),
        ]

    def __str__(self):
        return f"{self.farm_product_id}:{self.week_start}"
