from identity.navigation import (
    core_login_url,
    password_reset_url,
    public_site_url,
)


def account_navigation(request):
    return {
        "core_login_url": core_login_url(request),
        "password_reset_url": password_reset_url(request),
        "public_site_url": public_site_url(request),
    }
