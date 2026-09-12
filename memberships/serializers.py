import re
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from members.administrative_units import province_exists, ward_choices
from memberships.models import (
    CSAPurchaseRequestStatus,
    MembershipContentLocale,
    Product,
    QuotaPolicy,
)
from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone
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


class TermsAcceptedField(serializers.Field):
    default_error_messages = {"required": "CSA terms must be accepted."}

    def to_internal_value(self, data):
        if data is not True:
            self.fail("required")
        return True

    def to_representation(self, value):
        return bool(value)


class CSAPurchaseRequestCreateSerializer(StrictSerializer):
    package_id = serializers.UUIDField()
    price_option_id = serializers.UUIDField()
    name = serializers.CharField(max_length=150, trim_whitespace=True)
    phone = serializers.CharField(max_length=32, trim_whitespace=True)
    province_code = serializers.CharField(max_length=2, trim_whitespace=True)
    ward_code = serializers.CharField(max_length=5, trim_whitespace=True)
    address = serializers.CharField(max_length=500, trim_whitespace=True)
    email = serializers.EmailField(
        max_length=254, allow_blank=True, allow_null=True, required=False
    )
    zalo_whatsapp = serializers.CharField(
        max_length=64, allow_blank=True, allow_null=True, required=False, trim_whitespace=True
    )
    terms_accepted = TermsAcceptedField()
    terms_locale = serializers.ChoiceField(
        choices=MembershipContentLocale.choices,
        default=MembershipContentLocale.VI,
    )
    amount = serializers.JSONField(required=False, write_only=True)

    def validate_phone(self, value):
        try:
            return normalize_vietnam_phone(value)
        except InvalidPhoneNumber as error:
            raise serializers.ValidationError(str(error)) from error

    def validate(self, attrs):
        province_code = attrs["province_code"]
        ward_code = attrs["ward_code"]
        if not province_exists(province_code):
            raise serializers.ValidationError({"province_code": "Province code is invalid."})
        valid_wards = {code for code, _name in ward_choices(province_code, "vi")}
        if ward_code not in valid_wards:
            raise serializers.ValidationError(
                {"ward_code": "Ward code does not belong to province."}
            )
        return attrs


class AuthenticatedCSAPurchaseRequestCreateSerializer(StrictSerializer):
    package_id = serializers.UUIDField()
    price_option_id = serializers.UUIDField()
    zalo_whatsapp = serializers.CharField(
        max_length=64, allow_blank=True, allow_null=True, required=False, trim_whitespace=True
    )
    terms_accepted = TermsAcceptedField()
    terms_locale = serializers.ChoiceField(
        choices=MembershipContentLocale.choices,
        default=MembershipContentLocale.VI,
    )
    amount = serializers.JSONField(required=False, write_only=True)
    name = serializers.JSONField(required=False, write_only=True)
    phone = serializers.JSONField(required=False, write_only=True)
    province_code = serializers.JSONField(required=False, write_only=True)
    ward_code = serializers.JSONField(required=False, write_only=True)
    address = serializers.JSONField(required=False, write_only=True)
    email = serializers.JSONField(required=False, write_only=True)


class CSAPurchasePaymentConfirmSerializer(StrictSerializer):
    guest_confirmation_token = serializers.CharField(
        max_length=128,
        trim_whitespace=True,
        required=False,
    )


class AdminMembershipQuerySerializer(LocalePageQuerySerializer):
    search = serializers.CharField(max_length=254, allow_blank=True, default="")


class AdminCSACustomerQuerySerializer(PageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=30)
    search = serializers.CharField(max_length=150, allow_blank=True, default="")
    linked = serializers.ChoiceField(choices=("all", "linked", "managed"), default="all")
    membership_status = serializers.ChoiceField(
        choices=("scheduled", "active", "ended", "revoked"), required=False
    )


class AdminCSAPurchaseRequestQuerySerializer(PageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=30)
    search = serializers.CharField(max_length=200, allow_blank=True, default="")
    status = serializers.ChoiceField(
        choices=CSAPurchaseRequestStatus.choices,
        required=False,
    )


class CSAPurchaseRequestApproveSerializer(StrictSerializer):
    start_date = serializers.DateField()
    customer_resolution = serializers.ChoiceField(
        choices=("keep_existing", "use_request_data", "reject_conflict"),
        required=False,
    )
    rejection_reason = serializers.CharField(max_length=1000, trim_whitespace=True, required=False)

    def validate(self, attrs):
        if attrs.get("customer_resolution") == "reject_conflict" and not attrs.get(
            "rejection_reason"
        ):
            raise serializers.ValidationError(
                {"rejection_reason": "Rejection reason is required for conflict rejection."}
            )
        return attrs


class CSAPurchaseRequestRejectSerializer(StrictSerializer):
    rejection_reason = serializers.CharField(max_length=1000, trim_whitespace=True)


class CSAPurchaseRequestRefundSerializer(StrictSerializer):
    refund_note = serializers.CharField(
        max_length=1000,
        trim_whitespace=True,
        allow_blank=True,
        required=False,
        default="",
    )


class CSATrackerLookupSerializer(StrictSerializer):
    reference_code = serializers.CharField(max_length=20, trim_whitespace=True)
    phone = serializers.CharField(max_length=32, trim_whitespace=True)

    def validate_reference_code(self, value):
        return value.upper()

    def validate_phone(self, value):
        try:
            return normalize_vietnam_phone(value)
        except InvalidPhoneNumber as error:
            raise serializers.ValidationError(str(error)) from error


class CSAContractVerificationSerializer(StrictSerializer):
    reference_code = serializers.CharField(max_length=20, trim_whitespace=True)

    def validate_reference_code(self, value):
        normalized = value.upper()
        if not re.fullmatch(
            r"CSA-(?:[A-F0-9]{12}|\d{6}-[A-HJ-NP-Z2-9]{6})",
            normalized,
        ):
            raise serializers.ValidationError("Contract reference is unavailable.")
        return normalized


class MembershipPaymentSettingsWriteSerializer(StrictSerializer):
    bank_bin = serializers.RegexField(r"^\d{6}$", max_length=6)
    bank_name = serializers.CharField(max_length=160, trim_whitespace=True)
    account_number = serializers.RegexField(r"^\d{6,19}$", max_length=19)
    account_name = serializers.CharField(max_length=160, trim_whitespace=True)
    direct_transfer_enabled = serializers.BooleanField()


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
