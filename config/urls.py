from allauth.account.views import LoginView
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from oauth2_provider.views.base import TokenView

from identity import completion, views
from identity.oauth_views import CoreAuthorizationView, resolve_signup_account
from identity.recovery import (
    CorePasswordResetFromKeyDoneView,
    CorePasswordResetFromKeyView,
    CorePasswordResetView,
)
from identity.signup import CoreSignupView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/administrative-units/", include("members.urls")),
    path("api/v1/", include("content.urls")),
    path("api/v1/", include("commerce.urls")),
    path("api/v1/", include("farms.urls")),
    path("api/v1/", include("referrals.urls")),
    path("api/v1/", include("memberships.urls")),
    re_path(r"^register/?$", CoreSignupView.as_view(), name="account_signup_alias"),
    re_path(r"^login/?$", LoginView.as_view(), name="account_login_alias"),
    path("logout/public/", views.public_logout, name="public-logout"),
    path("accounts/email/", views.fixed_account_email, name="fixed-account-email"),
    path(
        "accounts/phone/change/",
        completion.phone_management_disabled,
        name="account_phone_management_disabled",
    ),
    path("accounts/phone/verify/", completion.phone_management_disabled),
    path("accounts/signup/", CoreSignupView.as_view(), name="account_signup"),
    path(
        "accounts/password/reset/", CorePasswordResetView.as_view(), name="account_reset_password"
    ),
    path(
        "accounts/password/reset/key/done/",
        CorePasswordResetFromKeyDoneView.as_view(),
        name="account_reset_password_from_key_done",
    ),
    re_path(
        r"^accounts/password/reset/key/(?P<uidb36>[0-9A-Za-z]+)-(?P<key>.+)/$",
        CorePasswordResetFromKeyView.as_view(),
        name="account_reset_password_from_key",
    ),
    path("accounts/", include("allauth.urls")),
    path("language/", views.set_language, name="set-language"),
    re_path(r"^oauth/authorize/?$", CoreAuthorizationView.as_view(), name="authorize"),
    path(
        "oauth/signup-account/",
        resolve_signup_account,
        name="resolve-signup-account",
    ),
    re_path(r"^oauth/token/?$", TokenView.as_view(), name="token"),
    re_path(r"^api/v1/me/?$", views.me, name="api-me"),
    path("health", views.health, name="health"),
    path("account", views.account, name="account"),
    path("account/complete", completion.complete, name="account_complete"),
    path("account/email/add", completion.add_email, name="account_email_add"),
    path("account/phone/add", completion.add_phone, name="account_phone_add"),
    path("account/email/resend", completion.resend_email, name="account_email_resend"),
    path("", views.home, name="home"),
]

if settings.DEBUG or settings.CONTENT_SERVE_MEDIA:
    urlpatterns += [re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT})]
