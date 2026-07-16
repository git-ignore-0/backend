from django.shortcuts import redirect, render
from oauth2_provider.views.base import AuthorizationView


class CoreAuthorizationView(AuthorizationView):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            email_verified = request.user.email_verified
            if not email_verified:
                return redirect("account_email_verification_sent")
            if not request.user.can_access_core(email_verified=email_verified):
                return render(request, "account/account_inactive.html", status=403)
        return super().dispatch(request, *args, **kwargs)
