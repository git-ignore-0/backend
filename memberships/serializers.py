from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from memberships.models import (
    MembershipRequestStatus,
    Product,
    QuotaPolicy,
)

MAX_PAGE = 10_000


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {field: "This field is not allowed." for field in unknown}
                )
        return super().to_internal_value(data)


class PageQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, max_value=MAX_PAGE, default=1)


class AdminProductQuerySerializer(PageQuerySerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, default="")
    active = serializers.BooleanField(required=False)


class AdminRequestQuerySerializer(PageQuerySerializer):
    status = serializers.ChoiceField(
        choices=MembershipRequestStatus.choices, allow_blank=True, default=""
    )
    search = serializers.CharField(max_length=254, allow_blank=True, default="")


class AdminMembershipQuerySerializer(PageQuerySerializer):
    search = serializers.CharField(max_length=254, allow_blank=True, default="")


class ProductWriteSerializer(StrictSerializer):
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    short_description = serializers.CharField(
        max_length=800, allow_blank=True, required=False, default="", trim_whitespace=True
    )
    unit_size = serializers.DecimalField(
        max_digits=10, decimal_places=3, min_value=Decimal("0.001")
    )
    unit_label_vi = serializers.CharField(max_length=40, trim_whitespace=True)
    unit_label_en = serializers.CharField(max_length=40, trim_whitespace=True)
    is_active = serializers.BooleanField(required=False, default=True)

class PackageItemWriteSerializer(StrictSerializer):
    product_id = serializers.UUIDField()
    quota_units = serializers.IntegerField(min_value=1)

    def validate_product_id(self, value):
        try:
            return Product.objects.get(pk=value)
        except Product.DoesNotExist as error:
            raise serializers.ValidationError("Product does not exist.") from error


class PackageWriteSerializer(StrictSerializer):
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    description = serializers.CharField(
        allow_blank=True, required=False, default="", trim_whitespace=True
    )
    upfront_price = serializers.DecimalField(
        max_digits=14, decimal_places=0, min_value=Decimal("1")
    )
    duration_months = serializers.IntegerField(min_value=1, max_value=120)
    quota_policy = serializers.ChoiceField(choices=QuotaPolicy.choices)
    is_active = serializers.BooleanField(required=False, default=True)
    items = PackageItemWriteSerializer(many=True, allow_empty=False)

    def validate_items(self, value):
        products = [item["product_id"] for item in value]
        ids = [item.pk for item in products]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Product IDs must be unique.")
        if any(not product.is_active for product in products):
            raise serializers.ValidationError("Package items must use active products.")
        return value


class PaymentSettingsWriteSerializer(StrictSerializer):
    version = serializers.IntegerField(min_value=0, write_only=True)
    direct_transfer_enabled = serializers.BooleanField()
    bank_bin = serializers.RegexField(r"^\d{6}$", max_length=6, allow_blank=True)
    account_number = serializers.RegexField(r"^\d{6,19}$", max_length=19, allow_blank=True)
    account_name = serializers.CharField(max_length=160, allow_blank=True, trim_whitespace=True)

    def validate(self, attrs):
        current = self.instance
        enabled = attrs.get(
            "direct_transfer_enabled",
            current.direct_transfer_enabled if current else False,
        )
        if not enabled:
            return attrs

        values = {
            field: attrs.get(field, getattr(current, field, "")).strip()
            for field in ("bank_bin", "account_number", "account_name")
        }
        errors = {}
        for field, value in values.items():
            if not value:
                errors[field] = "This field is required when direct transfer is enabled."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class MembershipRequestCreateSerializer(StrictSerializer):
    package_id = serializers.UUIDField()
    flow = serializers.ChoiceField(choices=["consultation", "direct_transfer"])
    phone = serializers.CharField(max_length=32, trim_whitespace=True)

    def validate_phone(self, value):
        from memberships.validation import normalize_membership_phone

        try:
            return normalize_membership_phone(value)
        except ValueError as error:
            raise serializers.ValidationError(str(error)) from error


class RequestActionSerializer(StrictSerializer):
    pass


class ReasonSerializer(StrictSerializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)


class ApproveSerializer(StrictSerializer):
    start_date = serializers.DateField()

    def validate_start_date(self, value):
        if value < timezone.localdate():
            raise serializers.ValidationError("Start date cannot be in the past.")
        return value


class UsageLineSerializer(StrictSerializer):
    product_id = serializers.UUIDField()
    units = serializers.IntegerField(min_value=1)


class UsageCreateSerializer(StrictSerializer):
    membership_id = serializers.UUIDField()
    idempotency_key = serializers.UUIDField()
    lines = UsageLineSerializer(many=True, allow_empty=False)
    note = serializers.CharField(
        max_length=500, allow_blank=True, required=False, default="", trim_whitespace=True
    )

    def validate_lines(self, value):
        ids = [item["product_id"] for item in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Product IDs must be unique.")
        return value


class UsageUndoSerializer(ReasonSerializer):
    pass
