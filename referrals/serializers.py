from rest_framework import serializers

from content.models import MediaAsset
from referrals.models import PointDirection, RedemptionStatus, Referral


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
    page = serializers.IntegerField(min_value=1, default=1)


class AdminUserQuerySerializer(PageQuerySerializer):
    search = serializers.CharField(max_length=254, allow_blank=True, default="")


class LocalePageQuerySerializer(PageQuerySerializer):
    locale = serializers.ChoiceField(choices=["vi", "en"], default="vi")


class AdminRedemptionQuerySerializer(PageQuerySerializer):
    search = serializers.CharField(max_length=254, allow_blank=True, default="")
    status = serializers.ChoiceField(
        choices=RedemptionStatus.choices,
        allow_blank=True,
        default="",
    )


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


class RewardTranslationWriteSerializer(StrictSerializer):
    locale = serializers.ChoiceField(choices=["vi", "en"])
    name = serializers.CharField(max_length=240, trim_whitespace=True)
    short_description = serializers.CharField(
        max_length=800,
        allow_blank=True,
        required=False,
        default="",
        trim_whitespace=True,
    )


class RewardWriteSerializer(StrictSerializer):
    point_cost = serializers.IntegerField(min_value=1)
    image_id = serializers.UUIDField()
    is_active = serializers.BooleanField(required=False, default=True)
    translations = RewardTranslationWriteSerializer(many=True)

    def validate_image_id(self, value):
        try:
            return MediaAsset.objects.get(pk=value)
        except MediaAsset.DoesNotExist as error:
            raise serializers.ValidationError("Media asset does not exist.") from error

    def validate_translations(self, value):
        locales = [item["locale"] for item in value]
        if len(locales) != len(set(locales)):
            raise serializers.ValidationError("Locale must be unique.")
        if set(locales) != {"vi", "en"}:
            raise serializers.ValidationError("Vietnamese and English translations are required.")
        return value


class RewardReorderSerializer(StrictSerializer):
    page = serializers.IntegerField(min_value=1)
    reward_ids = serializers.ListField(
        child=serializers.UUIDField(),
        min_length=1,
        max_length=30,
    )

    def validate_reward_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Reward IDs must be unique.")
        return value


class RedemptionCreateSerializer(StrictSerializer):
    reward_id = serializers.UUIDField()
    idempotency_key = serializers.UUIDField()


class RedemptionActionSerializer(StrictSerializer):
    internal_note = serializers.CharField(
        allow_blank=True,
        required=False,
        trim_whitespace=True,
    )


class RedemptionRejectSerializer(RedemptionActionSerializer):
    rejection_message = serializers.CharField(
        max_length=800,
        trim_whitespace=True,
    )
