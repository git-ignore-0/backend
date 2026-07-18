from rest_framework.permissions import BasePermission


class IsWebsiteAdmin(BasePermission):
    message = "Website administrator access is required."

    def has_permission(self, request, view):
        token = request.auth
        if not request.user or not request.user.is_authenticated or token is None:
            return False
        scopes = set(token.scope.split())
        return (
            "content:manage" in scopes and request.user.groups.filter(name="website_admin").exists()
        )
