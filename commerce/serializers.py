from decimal import Decimal

from django.utils.text import slugify
from rest_framework import serializers

from members.administrative_units import province_exists, ward_choices
from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone

MAX_POSITIVE_INTEGER = 2_147_483_647
MAX_POSITIVE_BIG_INTEGER = 9_223_372_036_854_775_807


class StrictBooleanField(serializers.BooleanField):
    def to_internal_value(self, data):
        if data not in {"true", "false"}:
            raise serializers.ValidationError("Must be exactly true or false.")
        return data == "true"


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {field: "This field is not allowed." for field in unknown}
                )
        return super().to_internal_value(data)


def validate_administrative_codes(*, province_code, ward_code):
    if not province_exists(province_code):
        raise serializers.ValidationError({"province_code": "Province code is invalid."})
    valid_wards = {code for code, _name in ward_choices(province_code, "vi")}
    if ward_code not in valid_wards:
        raise serializers.ValidationError({"ward_code": "Ward code does not belong to province."})


class ShippingAreaOverrideSerializer(StrictSerializer):
    ward_code = serializers.CharField(max_length=5, trim_whitespace=True)
    shipping_fee_vnd = serializers.IntegerField(
        min_value=0, max_value=MAX_POSITIVE_BIG_INTEGER, allow_null=True
    )
    is_active = serializers.BooleanField()


class ShippingSettingsSerializer(StrictSerializer):
    default_shipping_fee_vnd = serializers.IntegerField(
        min_value=0, max_value=MAX_POSITIVE_BIG_INTEGER
    )
    is_active = serializers.BooleanField()
    overrides = ShippingAreaOverrideSerializer(many=True)

    def validate_overrides(self, value):
        ward_codes = [item["ward_code"] for item in value]
        if len(ward_codes) != len(set(ward_codes)):
            raise serializers.ValidationError("Ward codes must be unique.")
        valid_wards = {code for code, _name in ward_choices(self.context["province_code"], "vi")}
        if any(code not in valid_wards for code in ward_codes):
            raise serializers.ValidationError(
                "Every ward code must belong to the selected province."
            )
        return value


class ShippingLocaleSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=["vi", "en"], required=False, default="vi")


class ShippingQuoteSerializer(StrictSerializer):
    province_code = serializers.CharField(max_length=2, trim_whitespace=True)
    ward_code = serializers.CharField(max_length=5, trim_whitespace=True)

    def validate(self, attrs):
        validate_administrative_codes(**attrs)
        return attrs


class ShippingAddressSerializer(StrictSerializer):
    recipient_name = serializers.CharField(max_length=200, trim_whitespace=True)
    phone_number = serializers.CharField(max_length=32, trim_whitespace=True)
    province_code = serializers.CharField(max_length=2, trim_whitespace=True)
    ward_code = serializers.CharField(max_length=5, trim_whitespace=True)
    address = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_phone_number(self, value):
        try:
            return normalize_vietnam_phone(value)
        except InvalidPhoneNumber as error:
            raise serializers.ValidationError("Phone number is invalid.") from error

    def validate(self, attrs):
        validate_administrative_codes(
            province_code=attrs["province_code"], ward_code=attrs["ward_code"]
        )
        return attrs


class TranslationSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=["vi", "en"])
    name = serializers.CharField(max_length=200, trim_whitespace=True)
    description = serializers.CharField(allow_blank=True, required=False, default="")


def validate_unique_locales(translations):
    locales = [item["locale"] for item in translations]
    if len(locales) != len(set(locales)):
        raise serializers.ValidationError("Locale must be unique.")
    return translations


class CategoryWriteSerializer(StrictSerializer):
    slug = serializers.SlugField(max_length=180, required=False)
    parent_id = serializers.UUIDField(allow_null=True, required=False)
    sort_order = serializers.IntegerField(
        min_value=0, max_value=MAX_POSITIVE_INTEGER, required=False
    )
    is_active = serializers.BooleanField(required=False)
    translations = TranslationSerializer(many=True, required=False)

    def validate_translations(self, value):
        return validate_unique_locales(value)


class CategoryQuerySerializer(StrictSerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")
    is_active = serializers.BooleanField(required=False)


class ProductTranslationSerializer(TranslationSerializer):
    short_description = serializers.CharField(
        max_length=800, allow_blank=True, required=False, default=""
    )


class ProductWriteSerializer(StrictSerializer):
    sku = serializers.CharField(max_length=80, required=False, trim_whitespace=True)
    slug = serializers.SlugField(max_length=180, required=False)
    category_id = serializers.UUIDField(required=False)
    unit_size = serializers.DecimalField(
        max_digits=10, decimal_places=3, min_value=Decimal("0.001"), required=False
    )
    unit_label_vi = serializers.CharField(max_length=40, required=False, trim_whitespace=True)
    unit_label_en = serializers.CharField(max_length=40, required=False, trim_whitespace=True)
    retail_price_vnd = serializers.IntegerField(
        min_value=1, max_value=MAX_POSITIVE_BIG_INTEGER, required=False
    )
    csa_quota_group_id = serializers.UUIDField(allow_null=True, required=False)
    csa_quota_cost = serializers.IntegerField(
        min_value=0, max_value=MAX_POSITIVE_INTEGER, required=False
    )
    primary_image_id = serializers.UUIDField(allow_null=True, required=False)
    is_active = serializers.BooleanField(required=False)
    is_published = serializers.BooleanField(required=False)
    translations = ProductTranslationSerializer(many=True, required=False)

    def validate_sku(self, value):
        if not slugify(value.replace("đ", "d").replace("Đ", "D")):
            raise serializers.ValidationError("SKU must contain letters or numbers.")
        return value

    def validate_translations(self, value):
        return validate_unique_locales(value)


class ProductQuerySerializer(StrictSerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")
    category_id = serializers.UUIDField(required=False)
    is_active = serializers.BooleanField(required=False)
    is_published = serializers.BooleanField(required=False)
    csa_eligible = serializers.BooleanField(required=False)
    page = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)


class PublicStoreLocaleSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=["vi", "en"])


class PublicStoreProductQuerySerializer(PublicStoreLocaleSerializer):
    search = serializers.CharField(
        max_length=100, allow_blank=True, trim_whitespace=True, required=False, default=""
    )
    category_id = serializers.UUIDField(required=False)
    csa_eligible = StrictBooleanField(required=False)
    price_min = serializers.IntegerField(min_value=0, max_value=1_000_000_000, required=False)
    price_max = serializers.IntegerField(min_value=0, max_value=1_000_000_000, required=False)
    sort = serializers.ChoiceField(
        choices=["default", "price_asc", "price_desc", "name"],
        required=False,
        default="default",
    )
    page = serializers.IntegerField(min_value=1, max_value=10_000, required=False, default=1)
    page_size = serializers.IntegerField(min_value=1, max_value=48, required=False, default=12)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get("price_min", 0) > attrs.get("price_max", 1_000_000_000):
            raise serializers.ValidationError(
                {"price_max": "Must be greater than or equal to price_min."}
            )
        return attrs
