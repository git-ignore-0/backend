from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.urls import reverse

PUBLIC_RETURN_TO_SESSION_KEY = "nfv_public_return_to"
LOGIN_NEXT_SESSION_KEY = "nfv_login_next"
_PENDING_OAUTH_SESSION_KEY = "nfv_pending_completion_oauth"


def safe_public_return_to(value: str | None) -> str | None:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
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
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc:
        return None
    target = urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    return target if target.startswith("/") and not target.startswith("//") else None


def safe_oauth_signup_next(value: str | None) -> str | None:
    next_url = safe_local_next(value)
    if not next_url:
        return None
    parsed = urlsplit(next_url)
    if parsed.path.rstrip("/") != reverse("authorize").rstrip("/"):
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
    if parsed.path.rstrip("/") != reverse("authorize").rstrip("/"):
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


def password_reset_url(request) -> str:
    reset_url = reverse("account_reset_password")
    match = request.resolver_match
    if match is None or match.url_name not in {"account_login", "account_login_alias"}:
        return reset_url
    return_url = safe_public_return_to(request.GET.get("return_to"))
    if not return_url:
        return_url = public_return_from_oauth_next(
            request.POST.get("next") or request.GET.get("next")
        )
    return f"{reset_url}?{urlencode({'return_to': return_url})}" if return_url else reset_url


def password_reset_login_url(request, return_to) -> str:
    return_url = safe_public_return_to(return_to)
    if not return_url:
        return reverse("account_login")

    parsed = urlsplit(return_url)
    return_path = urlunsplit(("", "", parsed.path or "/", parsed.query, parsed.fragment))
    query = urlencode(
        {
            "locale": getattr(request, "LANGUAGE_CODE", settings.LANGUAGE_CODE),
            "returnTo": return_path,
        }
    )
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/auth/login", query, ""))


def pending_oauth_destination(request, *, user) -> str | None:
    if not user.can_access_core():
        return None
    pending_user, next_url = request.session.pop(_PENDING_OAUTH_SESSION_KEY, (None, None))
    if pending_user == str(user.pk):
        return account_destination(request, user=user, candidate=next_url)
    return None


def account_destination(request, *, user=None, candidate=None, consume=True) -> str:
    """Retain safe continuation through the restricted session, consume on access."""
    from identity.models import User

    user = user or request.user
    if user.is_authenticated:
        user = User.objects.get(pk=user.pk)
    requested_next = candidate or request.POST.get("next") or request.GET.get("next")
    next_url = safe_local_next(requested_next or request.session.get(LOGIN_NEXT_SESSION_KEY))
    is_oauth_next = False
    if next_url:
        parsed = urlsplit(next_url)
        path = parsed.path.rstrip("/")
        if path in {
            reverse(name).rstrip("/")
            for name in (
                "account_login_alias",
                "account_signup_alias",
                "account_login",
                "account_signup",
                "account_complete",
            )
        }:
            next_url = None
        else:
            is_oauth_next = path == reverse("authorize").rstrip("/")
            request.session[LOGIN_NEXT_SESSION_KEY] = next_url
    if not user.can_access_core():
        if requested_next and is_oauth_next and user.can_authenticate():
            request.session[_PENDING_OAUTH_SESSION_KEY] = [str(user.pk), next_url]
        return reverse("account_complete")
    if is_oauth_next:
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key not in {"screen_hint", "referral_code"}
        ]
        next_url = urlunsplit(("", "", parsed.path, urlencode(query), parsed.fragment))
    if consume:
        request.session.pop(LOGIN_NEXT_SESSION_KEY, None)
        if is_oauth_next:
            request.session.pop(_PENDING_OAUTH_SESSION_KEY, None)
    return next_url or reverse("account")
