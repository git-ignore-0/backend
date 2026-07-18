from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from identity.oauth import access_token_from_request


class CoreBearerAuthentication(BaseAuthentication):
    def authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        if not authorization:
            return None
        token = access_token_from_request(request)
        if token is None or not token.user.can_access_core():
            raise AuthenticationFailed("Invalid or inactive access token.")
        return token.user, token

    def authenticate_header(self, request):
        return "Bearer"
