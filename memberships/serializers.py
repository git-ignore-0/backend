from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from memberships.models import (
    MembershipContentLocale,
    Product,
    QuotaPolicy,
)
from memberships.pricing import MAX_POSITIVE_BIG_INTEGER, calculate_total_price_vnd

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


class PageQuerySerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1, max_value=MAX_PAGE, default=1)


class LocaleQuerySerializer(StrictSerializer):
    locale = serializers.ChoiceField(
        choices=MembershipContentLocale.choices,
        default=MembershipContentLocale.VI,
    )


class LocalePageQuerySerializer(LocaleQuerySerializer):
    page = serializers.IntegerField(min_value=1, max_value=MAX_PAGE, default=1)


class AdminProductQuerySerializer(PageQuerySerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, default="")
    active = serializers.BooleanField(required=False)


class AdminPackageQuerySerializer(AdminProductQuerySerializer):
    pass


class PublicPackageQuerySerializer(LocalePageQuerySerializer):
    pass


class AdminMembershipQuerySerializer(LocalePageQuerySerializer):
    search = serializers.CharField(max_length=254, allow_blank=True, default="")


class AdminCSACustomerQuerySerializer(PageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=30)
    search = serializers.CharField(max_length=150, allow_blank=True, default="")
    linked = serializers.ChoiceField(choices=("all", "linked", "managed"), default="all")
    membership_status = serializers.ChoiceField(
        choices=("scheduled", "active", "ended", "revoked"), required=False
    )


class CSACustomerCreateSerializer(StrictSerializer):
    user_id = serializers.UUIDField(required=False)
    name = serializers.CharField(
        max_length=150, trim_whitespace=True, allow_blank=True, required=False
    )
    phone = serializers.CharField(
        max_length=32, trim_whitespace=True, allow_blank=True, allow_null=True, required=False
    )
    province_code = serializers.CharField(max_length=2, allow_blank=True, required=False)
    ward_code = serializers.CharField(max_length=5, allow_blank=True, required=False)
    address = serializers.CharField(max_length=500, allow_blank=True, required=False)


class CSACustomerUpdateSerializer(StrictSerializer):
    name = serializers.CharField(
        max_length=150, trim_whitespace=True, allow_blank=True, required=False
    )
    phone = serializers.CharField(
        max_length=32, trim_whitespace=True, allow_blank=True, allow_null=True, required=False
    )
    province_code = serializers.CharField(max_length=2, allow_blank=True, required=False)
    ward_code = serializers.CharField(max_length=5, allow_blank=True, required=False)
    address = serializers.CharField(max_length=500, allow_blank=True, required=False)


class CSACustomerLinkUserSerializer(StrictSerializer):
    user_id = serializers.UUIDField()


class ProductTranslationWriteSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=MembershipContentLocale.choices)
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    short_description = serializers.CharField(
        max_length=800, allow_blank=True, required=False, default="", trim_whitespace=True
    )


def validate_bilingual_translations(value):
    locales = [item["locale"] for item in value]
    if len(locales) != len(set(locales)):
        raise serializers.ValidationError("Locale must be unique.")
    if set(locales) != set(MembershipContentLocale.values):
        raise serializers.ValidationError("Vietnamese and English translations are required.")
    return value


class ProductWriteSerializer(StrictSerializer):
    translations = ProductTranslationWriteSerializer(many=True, required=False)
    unit_size = serializers.DecimalField(
        max_digits=10, decimal_places=3, min_value=Decimal("0.001")
    )
    unit_label_vi = serializers.CharField(max_length=40, trim_whitespace=True)
    unit_label_en = serializers.CharField(max_length=40, trim_whitespace=True)
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_translations(self, value):
        return validate_bilingual_translations(value)

    def validate(self, attrs):
        translations = attrs.get("translations")
        existing_locales = (
            set(self.instance.translations.values_list("locale", flat=True))
            if self.instance is not None
            else set()
        )
        if translations is None and existing_locales != set(MembershipContentLocale.values):
            raise serializers.ValidationError(
                {"translations": "Vietnamese and English translations are required."}
            )
        return attrs


class PackageTranslationWriteSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=MembershipContentLocale.choices)
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    description = serializers.CharField(
        allow_blank=True, required=False, default="", trim_whitespace=True
    )


class PackageItemWriteSerializer(StrictSerializer):
    product_id = serializers.UUIDField()
    quota_units = serializers.IntegerField(min_value=1)

    def validate_product_id(self, value):
        try:
            return Product.objects.get(pk=value)
        except Product.DoesNotExist as error:
            raise serializers.ValidationError("Product does not exist.") from error


class PackagePriceOptionWriteSerializer(StrictSerializer):
    id = serializers.UUIDField(required=False)
    duration_months = serializers.IntegerField(min_value=1, max_value=120)
    monthly_price_vnd = serializers.IntegerField(min_value=1, max_value=MAX_POSITIVE_BIG_INTEGER)
    is_active = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(min_value=0, required=False, default=0)

    def validate(self, attrs):
        try:
            calculate_total_price_vnd(
                duration_months=attrs["duration_months"],
                monthly_price_vnd=attrs["monthly_price_vnd"],
            )
        except ValueError as error:
            raise serializers.ValidationError({"monthly_price_vnd": str(error)}) from error
        return attrs


class PackageWriteSerializer(StrictSerializer):
    translations = PackageTranslationWriteSerializer(many=True, required=False)
    quota_policy = serializers.ChoiceField(choices=QuotaPolicy.choices)
    is_active = serializers.BooleanField(required=False, default=True)
    items = PackageItemWriteSerializer(many=True, allow_empty=False)
    price_options = PackagePriceOptionWriteSerializer(many=True, allow_empty=False, required=False)

    def validate_translations(self, value):
        return validate_bilingual_translations(value)

    def validate_items(self, value):
        products = [item["product_id"] for item in value]
        ids = [item.pk for item in products]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Product IDs must be unique.")
        if any(not product.is_active for product in products):
            raise serializers.ValidationError("Package items must use active products.")
        return value

    def validate_price_options(self, value):
        active_durations = [item["duration_months"] for item in value if item["is_active"]]
        if len(active_durations) != len(set(active_durations)):
            raise serializers.ValidationError(
                "Active duration months must be unique within a package."
            )
        option_ids = [item["id"] for item in value if item.get("id")]
        if len(option_ids) != len(set(option_ids)):
            raise serializers.ValidationError("Price option IDs must be unique.")
        if not any(item["is_active"] for item in value):
            raise serializers.ValidationError("At least one active price option is required.")
        return value

    def validate(self, attrs):
        translations = attrs.get("translations")
        existing_locales = (
            set(self.instance.translations.values_list("locale", flat=True))
            if self.instance is not None
            else set()
        )
        if translations is None and existing_locales != set(MembershipContentLocale.values):
            raise serializers.ValidationError(
                {"translations": "Vietnamese and English translations are required."}
            )
        if self.instance is None and "price_options" not in attrs:
            raise serializers.ValidationError(
                {"price_options": "At least one active price option is required."}
            )
        return attrs


class ReasonSerializer(StrictSerializer):
    reason = serializers.CharField(max_length=1000, trim_whitespace=True)


class MembershipAssignSerializer(StrictSerializer):
    customer_id = serializers.UUIDField(required=False)
    package_id = serializers.UUIDField()
    price_option_id = serializers.UUIDField()
    start_date = serializers.DateField(required=False, allow_null=True, default=None)
    farmbrite_order_reference = serializers.CharField(max_length=120, trim_whitespace=True)

    def validate_start_date(self, value):
        if value is not None and value < timezone.localdate():
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


class MembershipRevokeSerializer(StrictSerializer):
    reason = serializers.CharField(
        max_length=500,
        min_length=1,
        trim_whitespace=True,
        error_messages={"blank": "Revoke reason is required."},
    )
