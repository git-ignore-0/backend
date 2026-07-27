from django.urls import reverse

from identity.navigation import (
    core_login_url,
    public_login_url,
    public_site_url,
)


def account_navigation(request):
    return {
        "core_login_url": core_login_url(request),
        "password_reset_url": reverse("account_reset_password"),
        "public_login_url": public_login_url(request),
        "public_site_url": public_site_url(request),
    }
