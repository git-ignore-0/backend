import os
from pathlib import Path
from urllib.parse import urlsplit

from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TRUE_VALUES = {"1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ImproperlyConfigured(f"{name} must be a boolean value")


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} is required")
    return value


def parse_origins(name: str, value: str) -> tuple[str, ...]:
    origins: list[str] = []
    for item in value.split(","):
        candidate = item.strip()
        if not candidate:
            continue
        parsed = urlsplit(candidate)
        try:
            _port = parsed.port
        except ValueError as error:
            raise ImproperlyConfigured(
                f"{name} must contain comma-separated HTTP(S) origins without paths"
            ) from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ImproperlyConfigured(
                f"{name} must contain comma-separated HTTP(S) origins without paths"
            )
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if origin not in origins:
            origins.append(origin)
    return tuple(origins)


DEBUG = env_bool("DJANGO_DEBUG", True)
SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "unsafe-local-development-key"
    else:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DJANGO_DEBUG=false")

allowed_hosts_value = os.getenv("DJANGO_ALLOWED_HOSTS", "").strip()
if not allowed_hosts_value:
    if DEBUG:
        allowed_hosts_value = "localhost,127.0.0.1,testserver"
    else:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS is required when DJANGO_DEBUG=false")
ALLOWED_HOSTS = [item.strip() for item in allowed_hosts_value.split(",") if item.strip()]
CSRF_TRUSTED_ORIGINS = [
    item.strip() for item in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if item.strip()
]
public_site_origins_value = os.getenv("PUBLIC_SITE_ORIGINS", "").strip()
if not public_site_origins_value and DEBUG:
    public_site_origins_value = "http://localhost:3000"
PUBLIC_SITE_ORIGINS = parse_origins("PUBLIC_SITE_ORIGINS", public_site_origins_value)
if not PUBLIC_SITE_ORIGINS:
    raise ImproperlyConfigured("PUBLIC_SITE_ORIGINS is required when DJANGO_DEBUG=false")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "oauth2_provider",
    "rest_framework",
    "content.apps.ContentConfig",
    "identity.apps.IdentityConfig",
    "members.apps.MembersConfig",
]

MIDDLEWARE = ["django.middleware.security.SecurityMiddleware"]
if not DEBUG:
    MIDDLEWARE.append("whitenoise.middleware.WhiteNoiseMiddleware")
MIDDLEWARE += [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "identity.middleware.AccountNavigationMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "identity.middleware.UserLocaleMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "identity.context_processors.account_navigation",
            ],
        },
    },
]

if os.getenv("DB_ENGINE", "sqlite") == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "config.db.backends.mysql_connector",
            "NAME": required_env("DB_NAME"),
            "USER": required_env("DB_USER"),
            "PASSWORD": required_env("DB_PASSWORD"),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            "CONN_MAX_AGE": 60,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    database_name = os.getenv("DB_NAME", "db.sqlite3")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / database_name,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "vi"
LANGUAGES = [("vi", "Tiếng Việt"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
LANGUAGE_COOKIE_NAME = "nfv_core_language"
LANGUAGE_COOKIE_AGE = 365 * 24 * 60 * 60
TIME_ZONE = os.getenv("DJANGO_TIME_ZONE", "Asia/Ho_Chi_Minh")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
CONTENT_MAX_UPLOAD_BYTES = int(os.getenv("CONTENT_MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))
CONTENT_MAX_IMAGE_PIXELS = int(os.getenv("CONTENT_MAX_IMAGE_PIXELS", "16000000"))
CONTENT_SERVE_MEDIA = env_bool("CONTENT_SERVE_MEDIA", True)
CONTENT_FRONTEND_REVALIDATE_URL = os.getenv("CONTENT_FRONTEND_REVALIDATE_URL", "").strip()
CONTENT_FRONTEND_REVALIDATE_SECRET = os.getenv("CONTENT_FRONTEND_REVALIDATE_SECRET", "").strip()
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_USER_MODEL = "identity.User"
AUTHENTICATION_BACKENDS = [
    "identity.backends.CoreAuthenticationBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]
LOGIN_REDIRECT_URL = "/account"
LOGOUT_REDIRECT_URL = "/login"

ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_MAX_EMAIL_ADDRESSES = 1
ACCOUNT_EMAIL_SUBJECT_PREFIX = "[Natural Farming Vietnam] "
ACCOUNT_EMAIL_CONFIRMATION_EXPIRE_DAYS = 1
ACCOUNT_CONFIRM_EMAIL_ON_GET = False
ACCOUNT_LOGIN_ON_EMAIL_CONFIRMATION = True
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_FORMS = {
    "login": "identity.forms.CoreLoginForm",
    "signup": "identity.forms.CoreSignupForm",
}
ACCOUNT_ADAPTER = "identity.adapters.CoreAccountAdapter"

OAUTH2_PROVIDER = {
    "OAUTH2_VALIDATOR_CLASS": "identity.oauth.CoreOAuth2Validator",
    "SCOPES": {
        "profile": _("Read your stable identity and public profile"),
        "email": _("Read your email address and verification state"),
        "content:manage": _("Manage website settings and workshop content"),
    },
    "DEFAULT_SCOPES": ["profile", "email"],
    "ALLOWED_REDIRECT_URI_SCHEMES": ["http", "https"] if DEBUG else ["https"],
    "PKCE_REQUIRED": True,
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": 60,
    "ACCESS_TOKEN_EXPIRE_SECONDS": 3600,
    "REFRESH_TOKEN_EXPIRE_SECONDS": 2592000,
    "ROTATE_REFRESH_TOKEN": True,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "ERROR_RESPONSE_WITH_SCOPES": True,
}

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ImproperlyConfigured("EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be true")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL",
    "Natural Farming Vietnam <no-reply@naturalfarmingvietnam.com>",
)

SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", False)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", False)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = env_bool("DJANGO_SECURE_HSTS_PRELOAD", False)
if env_bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_NAME = "nfv_core_sessionid"
CSRF_COOKIE_NAME = "nfv_core_csrftoken"
LANGUAGE_COOKIE_SECURE = SESSION_COOKIE_SECURE
LANGUAGE_COOKIE_HTTPONLY = True
LANGUAGE_COOKIE_SAMESITE = "Lax"
PASSWORD_RESET_TIMEOUT = 3600
SECURE_CONTENT_TYPE_NOSNIFF = True
cross_origin_opener_policy = os.getenv("DJANGO_SECURE_CROSS_ORIGIN_OPENER_POLICY", "").strip()
SECURE_CROSS_ORIGIN_OPENER_POLICY = cross_origin_opener_policy or None

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["content.authentication.CoreBearerAuthentication"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
}
X_FRAME_OPTIONS = "DENY"

FLARUM_LIFECYCLE_URL = os.getenv("FLARUM_LIFECYCLE_URL", "").strip()
FLARUM_LIFECYCLE_SECRET = os.getenv("FLARUM_LIFECYCLE_SECRET", "").strip()
FLARUM_LIFECYCLE_TIMEOUT = int(os.getenv("FLARUM_LIFECYCLE_TIMEOUT", "10"))
FLARUM_LIFECYCLE_MAX_ATTEMPTS = int(os.getenv("FLARUM_LIFECYCLE_MAX_ATTEMPTS", "8"))
