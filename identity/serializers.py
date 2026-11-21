from collections.abc import Mapping

from django.core.exceptions import ValidationError
from rest_framework import serializers
from rest_framework.settings import api_settings

from identity.models import User
from identity.phones import normalize_login_phone


class AccountActionSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, Mapping):
            if data.keys() - self.fields.keys():
                raise serializers.ValidationError(
                    {api_settings.NON_FIELD_ERRORS_KEY: "Unexpected fields."}
                )
            if any(value is not None and not isinstance(value, str) for value in data.values()):
                raise serializers.ValidationError(
                    {api_settings.NON_FIELD_ERRORS_KEY: "Expected string values."}
                )
        return super().to_internal_value(data)


class AccountPasswordSerializer(AccountActionSerializer):
    password1 = serializers.CharField(write_only=True, trim_whitespace=False)
    password2 = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate(self, attrs):
        if attrs["password1"] != attrs["password2"]:
            raise serializers.ValidationError({"password2": "Passwords do not match."})
        return attrs


class AccountCreateSerializer(AccountPasswordSerializer):
    name = serializers.CharField(max_length=User._meta.get_field("name").max_length)
    login_phone_number = serializers.CharField(trim_whitespace=False)
    email = serializers.CharField(required=False, allow_null=True, allow_blank=True)
    locale = serializers.ChoiceField(choices=("vi", "en"), required=False)

    def validate_login_phone_number(self, value):
        try:
            return normalize_login_phone(value)
        except ValidationError as error:
            raise serializers.ValidationError(error.messages) from error

    def validate_email(self, value):
        email = User.objects.normalize_email(value)
        if email is None:
            return None
        field = User._meta.get_field("email")
        if len(email) > field.max_length:
            raise serializers.ValidationError("The normalized email exceeds the maximum length.")
        try:
            return field.clean(email, None)
        except ValidationError as error:
            raise serializers.ValidationError(error.messages) from error


class AccountSummarySerializer(serializers.ModelSerializer):
    email_verified = serializers.BooleanField(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "name",
            "login_phone_number",
            "email",
            "email_verified",
            "status",
            "locale",
            "date_joined",
        )
        read_only_fields = fields
