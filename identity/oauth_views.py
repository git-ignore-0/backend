from urllib.parse import urlencode

from django.conf import settings
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from oauth2_provider.views.base import AuthorizationView


class CoreAuthorizationView(AuthorizationView):
    def dispatch(self, request, *args, **kwargs):
        requested_locale = request.GET.get("ui_locales", "").split(" ", 1)[0]
        supported_locales = {code for code, _name in settings.LANGUAGES}
        if requested_locale in supported_locales:
            translation.activate(requested_locale)
            request.LANGUAGE_CODE = requested_locale

        if (
            not request.user.is_authenticated
            and request.method == "GET"
            and request.GET.get("screen_hint") == "signup"
        ):
            signup_url = reverse("account_signup")
            response = redirect(f"{signup_url}?{urlencode({'next': request.get_full_path()})}")
        else:
            response = self._dispatch_authorization(request, *args, **kwargs)

        if requested_locale in supported_locales:
            response.set_cookie(
                settings.LANGUAGE_COOKIE_NAME,
                requested_locale,
                max_age=settings.LANGUAGE_COOKIE_AGE,
                path=settings.LANGUAGE_COOKIE_PATH,
                secure=settings.LANGUAGE_COOKIE_SECURE,
                httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
                samesite=settings.LANGUAGE_COOKIE_SAMESITE,
            )
        return response

    def _dispatch_authorization(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            email_verified = request.user.email_verified
            if not email_verified:
                return redirect("account_email_verification_sent")
            if not request.user.can_access_core(email_verified=email_verified):
                return render(request, "account/account_inactive.html", status=403)
        return super().dispatch(request, *args, **kwargs)
