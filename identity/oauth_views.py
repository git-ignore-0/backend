from django.shortcuts import redirect, render
from oauth2_provider.views.base import AuthorizationView


class CoreAuthorizationView(AuthorizationView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if not request.user.email_verified:
                return redirect("account_email_verification_sent")
            if not request.user.can_access_core():
                return render(request, "account/inactive.html", status=403)
        return super().dispatch(request, *args, **kwargs)
