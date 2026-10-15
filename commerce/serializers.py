from decimal import Decimal

from django.utils.text import slugify
from rest_framework import serializers

MAX_POSITIVE_INTEGER = 2_147_483_647
MAX_POSITIVE_BIG_INTEGER = 9_223_372_036_854_775_807


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {field: "This field is not allowed." for field in unknown}
                )
        return super().to_internal_value(data)


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
