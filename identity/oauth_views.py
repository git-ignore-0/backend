from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import logout as django_logout
from django.http import HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.views.decorators.http import require_POST
from oauth2_provider.views.base import AuthorizationView

from identity.navigation import safe_oauth_signup_next
from identity.signup import SIGNUP_REFERRAL_SESSION_KEY
from referrals.services import is_referral_code_format, normalize_referral_code


class CoreAuthorizationView(AuthorizationView):
    def dispatch(self, request, *args, **kwargs):
        requested_locale = request.GET.get("ui_locales", "").split(" ", 1)[0]
        supported_locales = {code for code, _name in settings.LANGUAGES}
        if requested_locale in supported_locales:
            translation.activate(requested_locale)
            request.LANGUAGE_CODE = requested_locale

        signup_requested = request.GET.get("screen_hint") == "signup"
        if request.user.is_authenticated and signup_requested:
            response = render(
                request,
                "oauth2_provider/signup_account_conflict.html",
                {
                    "account_email": request.user.email,
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
        if request.user.is_authenticated:
            email_verified = request.user.email_verified
            if not email_verified:
                return redirect("account_email_verification_sent")
            if not request.user.can_access_core(email_verified=email_verified):
                return render(request, "account/account_inactive.html", status=403)
        return super().dispatch(request, *args, **kwargs)


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
