from django.conf import settings
from django.utils import translation


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
