from rest_framework import serializers

from commerce.models import ProductStatus
from commerce.serializers import StrictSerializer
from config.phones import InvalidPhoneNumber, normalize_vietnam_phone
from content.models import TRACKER_FARM_MAX_PHOTOS, TRACKER_SIGNUP_COUNT_MAX


class JSONUUIDField(serializers.UUIDField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid", value=data)
        return super().to_internal_value(data)


class FarmWriteSerializer(StrictSerializer):
    contact_phone = serializers.CharField(
        max_length=13, required=False, allow_blank=True, default=""
    )

    def validate_contact_phone(self, value):
        if not value:
            return ""
        try:
            return normalize_vietnam_phone(value)
        except InvalidPhoneNumber as error:
            raise serializers.ValidationError(str(error)) from error

    name_en = serializers.CharField(max_length=240)
    name_vi = serializers.CharField(max_length=240)
    location_en = serializers.CharField(max_length=240)
    location_vi = serializers.CharField(max_length=240)
    description_en = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default=""
    )
    description_vi = serializers.CharField(
        required=False, allow_blank=True, allow_null=True, default=""
    )
    photo_ids = serializers.ListField(
        child=serializers.UUIDField(), required=False, max_length=TRACKER_FARM_MAX_PHOTOS
    )
    is_active = serializers.BooleanField(required=False, default=True)

    def validate_photo_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Photo IDs must be unique.")
        return value

    def validate_description_en(self, value):
        return value or ""

    def validate_description_vi(self, value):
        return value or ""


class FarmUpdateSerializer(FarmWriteSerializer):
    version = serializers.IntegerField(min_value=1)

    def validate(self, data):
        data = super().validate(data)
        if "version" not in data:
            raise serializers.ValidationError({"version": "This field is required."})
        return data


class FarmQuerySerializer(StrictSerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")
    is_active = serializers.BooleanField(required=False)
    page = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)


class StockBatchWriteSerializer(StrictSerializer):
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    quantity_units = serializers.IntegerField(min_value=1, max_value=2_147_483_647)


class FarmProductLinkSerializer(StrictSerializer):
    idempotency_key = JSONUUIDField()
    product_ids = serializers.ListField(child=JSONUUIDField(), allow_empty=False, max_length=50)

    def validate_product_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Product IDs must be unique.")
        return value


class FarmProductQuerySerializer(StrictSerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")
    page = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)


class FarmSupplyListQuerySerializer(FarmProductQuerySerializer):
    product_status = serializers.ChoiceField(choices=ProductStatus.choices, required=False)
    supply_status = serializers.ChoiceField(choices=("supplying", "removed", "all"), default="all")


class FarmProductRevisionSerializer(StrictSerializer):
    expected_link_id = serializers.UUIDField()
    expected_version = serializers.IntegerField(min_value=1)


class FarmProductReaddSerializer(FarmProductRevisionSerializer):
    expected_link_id = JSONUUIDField()
    idempotency_key = JSONUUIDField()


class VersionedStockBatchWriteSerializer(StockBatchWriteSerializer, FarmProductRevisionSerializer):
    pass


class FarmProductHistoryQuerySerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1, max_value=10000, default=1)


class StockBatchUpdateSerializer(StrictSerializer):
    version = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    quantity_units = serializers.IntegerField(min_value=1, max_value=2_147_483_647, required=False)

    def validate(self, data):
        data = super().validate(data)
        if not any(field in data for field in ("start_date", "end_date", "quantity_units")):
            raise serializers.ValidationError("Submit at least one stock batch field.")
        return data


class StockBatchCancelSerializer(StrictSerializer):
    version = serializers.IntegerField(min_value=1)


class StockBatchQuerySerializer(StrictSerializer):
    status = serializers.ChoiceField(
        choices=("upcoming", "active", "expired", "cancelled"), required=False
    )
    scope = serializers.ChoiceField(choices=("current", "history", "all"), default="all")
    page = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)


class GrowthTrackerWriteSerializer(StrictSerializer):
    signup_count = serializers.IntegerField(
        min_value=0, max_value=TRACKER_SIGNUP_COUNT_MAX, required=False, default=0
    )
    one_month_signup_count = serializers.IntegerField(
        min_value=0, max_value=TRACKER_SIGNUP_COUNT_MAX, required=False, default=0
    )
    is_visible = serializers.BooleanField(required=False, default=True)
    sort_order = serializers.IntegerField(min_value=0, required=False)


class GrowthTrackerUpdateSerializer(GrowthTrackerWriteSerializer):
    version = serializers.IntegerField(min_value=1)

    def validate(self, data):
        data = super().validate(data)
        if "version" not in data:
            raise serializers.ValidationError({"version": "This field is required."})
        return data


# Match the existing Store multi-line write bound; reject, never split a transaction.
BULK_STOCK_BATCH_MAX_ITEMS = 50


class BulkStockQuantityField(serializers.IntegerField):
    def to_internal_value(self, data):
        # DRF's IntegerField also accepts integral decimals such as 1.0.
        if type(data) is not int:
            raise serializers.ValidationError("A valid integer is required.", code="invalid")
        return super().to_internal_value(data)


class BulkStockBatchItemSerializer(FarmProductRevisionSerializer):
    product_id = serializers.UUIDField()
    quantity_units = BulkStockQuantityField(min_value=1, max_value=2_147_483_647)


class BulkStockBatchWriteSerializer(StrictSerializer):
    idempotency_key = serializers.UUIDField()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    items = BulkStockBatchItemSerializer(
        many=True, allow_empty=False, max_length=BULK_STOCK_BATCH_MAX_ITEMS
    )

    def validate_items(self, value):
        ids = [item["product_id"] for item in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Product IDs must be unique.")
        return value


class FarmCreationRecoverySerializer(StrictSerializer):
    creation_key = serializers.UUIDField()
