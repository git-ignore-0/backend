from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY
from django.contrib.auth import logout as django_logout
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_POST
from oauth2_provider.models import get_application_model
from oauth2_provider.views.base import AuthorizationView

from identity.models import User
from identity.navigation import (
    PUBLIC_RETURN_TO_SESSION_KEY,
    safe_oauth_signup_next,
    safe_public_return_to,
)
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY
from referrals.services import is_referral_code_format, normalize_referral_code


class _AuthorizationIdentityChanged(Exception):
    def __init__(self, *, session_invalid: bool):
        self.session_invalid = session_invalid


class CoreAuthorizationView(AuthorizationView):
    login_url = "account_login"

    def dispatch(self, request, *args, **kwargs):
        requested_locale = request.GET.get("ui_locales", "").split(" ", 1)[0]
        supported_locales = {code for code, _name in settings.LANGUAGES}
        if requested_locale in supported_locales:
            translation.activate(requested_locale)
            request.LANGUAGE_CODE = requested_locale

        signup_requested = request.GET.get("screen_hint") == "signup"
        if request.user.is_authenticated and signup_requested:
            user = request.user
            account_label = user.email or user.login_phone_number or user.name
            response = render(
                request,
                "oauth2_provider/signup_account_conflict.html",
                {
                    "account_label": account_label,
                    "oauth_next": request.get_full_path(),
                },
            )
            response["Cache-Control"] = "no-store"
        elif not request.user.is_authenticated and request.method == "GET" and signup_requested:
            referral_code = normalize_referral_code(request.GET.get("referral_code", ""))
            if is_referral_code_format(referral_code):
                request.session[SIGNUP_REFERRAL_SESSION_KEY] = referral_code
            else:
                request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
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
        if request.user.is_authenticated and not request.user.can_access_core():
            return render(request, "account/account_inactive.html", status=403)
        try:
            return super().dispatch(request, *args, **kwargs)
        except _AuthorizationIdentityChanged as error:
            if error.session_invalid:
                django_logout(request)
                return self.handle_no_permission()
            return render(request, "account/account_inactive.html", status=403)

    def create_authorization_response(self, request, scopes, credentials, allow):
        if not allow:
            return super().create_authorization_response(request, scopes, credentials, allow)

        # Password reset holds this User lock through revocation. Both native consent and
        # automatic approval must recheck the browser session before writing a new grant.
        with transaction.atomic():
            user = User.objects.select_for_update().filter(pk=request.user.pk).first()
            auth_hash = request.session.get(HASH_SESSION_KEY, "")
            if user is None or not constant_time_compare(auth_hash, user.get_session_auth_hash()):
                raise _AuthorizationIdentityChanged(session_invalid=True)
            if not user.can_access_core():
                raise _AuthorizationIdentityChanged(session_invalid=not user.can_authenticate())
            request.user = user
            return super().create_authorization_response(request, scopes, credentials, allow)

    def render_to_response(self, context, **response_kwargs):
        if context.get("error"):
            context["oauth_return_url"] = self._error_return_url()
        return super().render_to_response(context, **response_kwargs)

    def _error_return_url(self):
        return_url = safe_public_return_to(self.request.session.get(PUBLIC_RETURN_TO_SESSION_KEY))
        if not return_url:
            return None

        params = self.request.POST if self.request.method == "POST" else self.request.GET
        redirect_uri = params.get("redirect_uri")
        callback_url = safe_public_return_to(redirect_uri)
        if not callback_url or urlsplit(callback_url)[:2] != urlsplit(return_url)[:2]:
            return None

        application = (
            get_application_model().objects.filter(client_id=params.get("client_id")).first()
        )
        if application and application.redirect_uri_allowed(redirect_uri):
            return return_url
        return None


@require_POST
def resolve_signup_account(request):
    oauth_next = safe_oauth_signup_next(request.POST.get("next"))
    action = request.POST.get("action")
    if not oauth_next or action not in {"continue", "logout"}:
        return HttpResponseBadRequest()

    request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
    if action == "logout":
        django_logout(request)
        return redirect(oauth_next)

    if not request.user.is_authenticated:
        return redirect(oauth_next)

    parsed = urlsplit(oauth_next)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"screen_hint", "referral_code"}
    ]
    return redirect(urlunsplit(("", "", parsed.path, urlencode(query), parsed.fragment)))
