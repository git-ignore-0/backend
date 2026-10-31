from django.utils import timezone
from rest_framework import serializers

from commerce.serializers import StrictSerializer
from content.models import TRACKER_FARM_MAX_PHOTOS, TRACKER_SIGNUP_COUNT_MAX


class FarmWriteSerializer(StrictSerializer):
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

    def validate(self, data):
        data = super().validate(data)
        if data["end_date"] < data["start_date"]:
            raise serializers.ValidationError({"end_date": "End date must not precede start date."})
        if data["end_date"] < timezone.localdate():
            raise serializers.ValidationError(
                {"end_date": "An expired stock batch cannot be created."}
            )
        return data


class FarmProductWriteSerializer(StockBatchWriteSerializer):
    product_id = serializers.UUIDField()


class FarmProductQuerySerializer(StrictSerializer):
    search = serializers.CharField(max_length=200, allow_blank=True, required=False, default="")
    page = serializers.IntegerField(min_value=1, max_value=10000, required=False, default=1)


class FarmProductUpdateSerializer(StrictSerializer):
    is_active = serializers.BooleanField()


class StockBatchUpdateSerializer(StrictSerializer):
    version = serializers.IntegerField(min_value=1)
    start_date = serializers.DateField(required=False)
    end_date = serializers.DateField(required=False)
    quantity_units = serializers.IntegerField(
        min_value=1, max_value=2_147_483_647, required=False
    )

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
