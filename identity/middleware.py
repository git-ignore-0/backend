from django.conf import settings
from django.utils import translation

from identity.navigation import (
    LOGIN_NEXT_SESSION_KEY,
    PUBLIC_RETURN_TO_SESSION_KEY,
    account_destination,
    public_return_from_oauth_next,
    safe_local_next,
    safe_public_return_to,
)


class AccountNavigationMiddleware:
    """Keep validated navigation context while the user moves through Core account pages."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return_to = safe_public_return_to(request.GET.get("return_to"))
        if return_to:
            request.session[PUBLIC_RETURN_TO_SESSION_KEY] = return_to

        next_url = safe_local_next(request.GET.get("next"))
        if next_url:
            request.session[LOGIN_NEXT_SESSION_KEY] = next_url
            oauth_return_to = public_return_from_oauth_next(next_url)
            if not return_to and oauth_return_to:
                request.session[PUBLIC_RETURN_TO_SESSION_KEY] = oauth_return_to

        return self.get_response(request)


class UserLocaleMiddleware:
    """Use the saved account locale when the current browser has no preference."""

    def __init__(self, get_response):
        self.get_response = get_response
        self.supported_languages = {code for code, _name in settings.LANGUAGES}

    def __call__(self, request):
        has_language_cookie = settings.LANGUAGE_COOKIE_NAME in request.COOKIES
        if request.user.is_authenticated and not has_language_cookie:
            language = request.user.locale
            if language in self.supported_languages:
                translation.activate(language)
                request.LANGUAGE_CODE = language

        response = self.get_response(request)

        if request.user.is_authenticated and not has_language_cookie:
            language = request.user.locale
            if language in self.supported_languages:
                response.set_cookie(
                    settings.LANGUAGE_COOKIE_NAME,
                    language,
                    max_age=settings.LANGUAGE_COOKIE_AGE,
                    path=settings.LANGUAGE_COOKIE_PATH,
                    secure=settings.LANGUAGE_COOKIE_SECURE,
                    httponly=settings.LANGUAGE_COOKIE_HTTPONLY,
                    samesite=settings.LANGUAGE_COOKIE_SAMESITE,
                )
        return response


class RestrictedAccountMiddleware:
    """Restrict authenticated pending accounts only on protected HTML routes."""

    allowed = {
        "account_complete",
        "account_email_add",
        "account_phone_add",
        "account_email_resend",
        "account_confirm_email",
        "account_email_verification_sent",
        "fixed-account-email",
        "account_logout",
        "public-logout",
        "set-language",
        "account_login",
        "account_login_alias",
        "account_reset_password",
        "account_reset_password_done",
        "account_reset_password_from_key",
        "account_reset_password_from_key_done",
        "account_phone_management_disabled",
    }

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, view_func, view_args, view_kwargs):
        from django.contrib.auth import logout
        from django.shortcuts import redirect

        user = request.user
        if not user.is_authenticated:
            return None
        path = request.path_info
        protected = path.rstrip("/") in {"/account", "/oauth/authorize"} or path.startswith(
            ("/account/", "/accounts/", "/admin/")
        )
        if not protected:
            return None
        if not user.can_authenticate():
            logout(request)
            return redirect("account_login")
        if user.can_access_core() or request.resolver_match.url_name in self.allowed:
            return None
        if path.rstrip("/") == "/oauth/authorize":
            return redirect(
                account_destination(request, user=user, candidate=request.get_full_path())
            )
        return redirect("account_complete")


class CoreCredentialLockMiddleware:
    """Serialize native code/refresh exchanges with Core password revocation.

    This changes no OAuth validation, eligibility, scopes or response payload.
    The User lock is acquired before native validation, in the same order as reset.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method != "POST" or request.path_info.rstrip("/") != "/oauth/token":
            return self.get_response(request)
        # This boundary runs before native TokenView marks its sensitive fields.
        request.sensitive_post_parameters = "__ALL__"
        from hashlib import sha256

        from django.db import transaction
        from oauth2_provider.models import get_grant_model, get_refresh_token_model

        from identity.models import User

        owners = set()
        grants = request.POST.getlist("grant_type")
        if "refresh_token" in grants:
            checksums = [
                sha256(token.encode("utf-8")).hexdigest()
                for token in request.POST.getlist("refresh_token")
                if token
            ]
            owners.update(
                get_refresh_token_model()
                .objects.filter(token_checksum__in=checksums)
                .values_list("user_id", flat=True)
            )
        if "authorization_code" in grants:
            owners.update(
                get_grant_model()
                .objects.filter(code__in=request.POST.getlist("code"))
                .values_list("user_id", flat=True)
            )
        owners.discard(None)
        if not owners:
            return self.get_response(request)
        with transaction.atomic():
            list(User.objects.select_for_update().filter(pk__in=owners).order_by("pk"))
            return self.get_response(request)
