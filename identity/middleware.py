from django.conf import settings
from django.utils import translation

from identity.navigation import (
    LOGIN_NEXT_SESSION_KEY,
    PUBLIC_RETURN_TO_SESSION_KEY,
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
