from rest_framework import serializers

from referrals.models import PointDirection, Referral


class PageQuerySerializer(serializers.Serializer):
    page = serializers.IntegerField(min_value=1, default=1)


class AdminUserQuerySerializer(PageQuerySerializer):
    search = serializers.CharField(max_length=254, allow_blank=True, default="")


class ReferralCodeSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=12, trim_whitespace=True)


class PointTransactionWriteSerializer(serializers.Serializer):
    direction = serializers.ChoiceField(choices=PointDirection.choices)
    amount = serializers.IntegerField(min_value=1)
    message = serializers.CharField(max_length=240, trim_whitespace=True)
    related_referral_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_related_referral_id(self, value):
        if value is None:
            return None
        try:
            return Referral.objects.select_related("referrer", "referee").get(pk=value)
        except Referral.DoesNotExist as error:
            raise serializers.ValidationError("Referral does not exist.") from error

    def validate_message(self, value):
        if not value:
            raise serializers.ValidationError("Message is required.")
        return value
