from allauth.account.views import LoginView, SignupView
from django.contrib import admin
from django.urls import include, path, re_path
from oauth2_provider.views.base import TokenView

from identity import views
from identity.oauth_views import CoreAuthorizationView

urlpatterns = [
    path("admin/", admin.site.urls),
    re_path(r"^register/?$", SignupView.as_view(), name="account_signup_alias"),
    re_path(r"^login/?$", LoginView.as_view(), name="account_login_alias"),
    path("accounts/", include("allauth.urls")),
    path("language/", views.set_language, name="set-language"),
    re_path(r"^oauth/authorize/?$", CoreAuthorizationView.as_view(), name="authorize"),
    re_path(r"^oauth/token/?$", TokenView.as_view(), name="token"),
    re_path(r"^api/v1/me/?$", views.me, name="api-me"),
    path("health", views.health, name="health"),
    path("account", views.account, name="account"),
    path("", views.home, name="home"),
]
