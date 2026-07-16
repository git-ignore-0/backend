import hashlib
from datetime import timedelta

from django.utils import timezone
from oauth2_provider.models import AccessToken
from oauth2_provider.oauth2_validators import OAuth2Validator
from oauth2_provider.settings import oauth2_settings

REQUIRED_ME_SCOPES = {"profile", "email"}


class CoreOAuth2Validator(OAuth2Validator):
    def validate_code(self, client_id, code, client, request, *args, **kwargs):
        valid = super().validate_code(client_id, code, client, request, *args, **kwargs)
        return valid and request.user.can_access_core()

    def validate_refresh_token(self, refresh_token, client, request, *args, **kwargs):
        valid = super().validate_refresh_token(refresh_token, client, request, *args, **kwargs)
        if not valid or not request.user.can_access_core():
            return False

        lifetime = oauth2_settings.REFRESH_TOKEN_EXPIRE_SECONDS
        if lifetime is None:
            return True
        if not isinstance(lifetime, timedelta):
            lifetime = timedelta(seconds=lifetime)
        return request.refresh_token_instance.created + lifetime > timezone.now()

    def validate_bearer_token(self, token, scopes, request):
        valid = super().validate_bearer_token(token, scopes, request)
        return valid and request.user.can_access_core()


def access_token_from_request(request):
    authorization = request.headers.get("Authorization", "")
    scheme, separator, raw_token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not raw_token.strip():
        return None

    try:
        raw_token = raw_token.strip()
        checksum = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token = AccessToken.objects.select_related("user").get(token_checksum=checksum)
    except AccessToken.DoesNotExist:
        return None
    return token if token.is_valid() else None


def has_me_scopes(token) -> bool:
    return REQUIRED_ME_SCOPES.issubset(set(token.scope.split()))
