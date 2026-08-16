from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.urls import reverse

PUBLIC_RETURN_TO_SESSION_KEY = "nfv_public_return_to"
LOGIN_NEXT_SESSION_KEY = "nfv_login_next"


def safe_public_return_to(value: str | None) -> str | None:
    if not value or "\r" in value or "\n" in value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        return None
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if origin not in settings.PUBLIC_SITE_ORIGINS:
        return None
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            parsed.query,
            parsed.fragment,
        )
    )


def safe_local_next(value: str | None) -> str | None:
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\r" in value
        or "\n" in value
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    return urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))


def safe_oauth_signup_next(value: str | None) -> str | None:
    next_url = safe_local_next(value)
    if not next_url:
        return None
    parsed = urlsplit(next_url)
    if parsed.path.rstrip("/") != "/oauth/authorize":
        return None
    query = parse_qs(parsed.query)
    if query.get("screen_hint", [""])[0] != "signup":
        return None
    return next_url


def public_return_from_oauth_next(value: str | None) -> str | None:
    next_url = safe_local_next(value)
    if not next_url:
        return None
    parsed = urlsplit(next_url)
    if parsed.path.rstrip("/") != "/oauth/authorize":
        return None
    redirect_uri = parse_qs(parsed.query).get("redirect_uri", [None])[0]
    if not redirect_uri:
        return None
    try:
        callback = urlsplit(redirect_uri)
    except ValueError:
        return None
    if not callback.scheme or not callback.netloc:
        return None
    return safe_public_return_to(urlunsplit((callback.scheme, callback.netloc, "/", "", "")))


def default_public_site_url() -> str:
    return f"{settings.PUBLIC_SITE_ORIGINS[0]}/"


def public_site_url(request) -> str:
    stored = safe_public_return_to(request.session.get(PUBLIC_RETURN_TO_SESSION_KEY))
    return stored or default_public_site_url()


def core_login_url(request) -> str:
    login_url = reverse("account_login")
    next_url = safe_local_next(request.session.get(LOGIN_NEXT_SESSION_KEY))
    if not next_url:
        return login_url
    return f"{login_url}?{urlencode({'next': next_url})}"


def public_login_url(request) -> str:
    return_url = public_site_url(request)
    parsed = urlsplit(return_url)
    return_path = urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    query = urlencode(
        {
            "locale": getattr(request, "LANGUAGE_CODE", settings.LANGUAGE_CODE),
            "returnTo": return_path,
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/auth/login", query, ""))
