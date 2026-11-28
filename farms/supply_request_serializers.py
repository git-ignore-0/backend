import re

from rest_framework import serializers

from commerce.serializers import StrictSerializer
from farms.models import FarmSupplyRequest
from farms.serializers import (
    FARM_BULK_MAX_ITEMS,
    BulkStockBatchWriteSerializer,
    FarmProductHistoryQuerySerializer,
    FarmProductQuerySerializer,
    JSONIntegerField,
    JSONUUIDField,
)


class ISODateField(serializers.DateField):
    def to_internal_value(self, data):
        if not isinstance(data, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", data):
            self.fail("invalid", format="YYYY-MM-DD")
        return super().to_internal_value(data)


class ProductIdentityField(serializers.RegexField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class FarmerSupplyItemSerializer(StrictSerializer):
    product_id = JSONUUIDField()
    expected_link_id = JSONUUIDField()
    expected_generation = JSONIntegerField(min_value=1, max_value=2_147_483_647)
    # A missing value is accepted only for a verified replay of a Task 03 payload.
    expected_product_identity = ProductIdentityField(
        regex=r"\A[0-9a-f]{64}\Z", trim_whitespace=False, required=False
    )
    quantity_units = JSONIntegerField(min_value=1, max_value=2_147_483_647)


class FarmerSupplyWriteSerializer(BulkStockBatchWriteSerializer):
    idempotency_key = JSONUUIDField()
    start_date = ISODateField()
    end_date = ISODateField()
    items = FarmerSupplyItemSerializer(many=True, allow_empty=False, max_length=FARM_BULK_MAX_ITEMS)


class QueryPageField(serializers.IntegerField):
    def to_internal_value(self, data):
        if not isinstance(data, str) or not re.fullmatch(r"[0-9]+", data):
            self.fail("invalid")
        return super().to_internal_value(data)


class FarmerProductListQuerySerializer(FarmProductQuerySerializer):
    page = QueryPageField(min_value=1, max_value=10000, default=1)


class FarmerHistoryQuerySerializer(FarmProductHistoryQuerySerializer):
    page = QueryPageField(min_value=1, max_value=10000, default=1)


class SupplyRequestQuerySerializer(FarmerProductListQuerySerializer):
    farm_id = JSONUUIDField(required=False)
    status = serializers.ChoiceField(choices=FarmSupplyRequest.Status.choices, required=False)


class SupplyDecisionSerializer(StrictSerializer):
    idempotency_key = JSONUUIDField()


class SupplyRejectSerializer(SupplyDecisionSerializer):
    reason = serializers.CharField(max_length=500)

    def validate_reason(self, value):
        # CharField accepts numbers by coercion, which is inappropriate for this command.
        if not isinstance(self.initial_data["reason"], str):
            raise serializers.ValidationError("A valid string is required.", code="invalid")
        return value
