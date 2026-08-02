from rest_framework.permissions import BasePermission


class HasMembershipSelfScopes(BasePermission):
    def has_permission(self, request, view):
        token = request.auth
        return bool(
            request.user
            and request.user.is_authenticated
            and token
            and {"profile", "email"}.issubset(set(token.scope.split()))
        )
