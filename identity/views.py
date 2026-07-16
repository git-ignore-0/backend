from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from identity.oauth import access_token_from_request, has_me_scopes


def no_store(response: JsonResponse) -> JsonResponse:
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def oauth_error(code: str, status: int) -> JsonResponse:
    response = no_store(JsonResponse({"code": code}, status=status))
    if status == 401:
        response["WWW-Authenticate"] = 'Bearer error="invalid_token"'
    elif code == "insufficient_scope":
        response["WWW-Authenticate"] = 'Bearer error="insufficient_scope", scope="profile email"'
    return response


def home(request):
    return redirect("account" if request.user.is_authenticated else "account_login_alias")


@login_required
def account(request):
    email_verified = request.user.email_verified
    if not email_verified:
        return redirect("account_email_verification_sent")
    if not request.user.can_access_core(email_verified=email_verified):
        return render(request, "account/account_inactive.html", status=403)
    return render(request, "account/dashboard.html")


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@require_GET
def me(request):
    token = access_token_from_request(request)
    if token is None:
        return oauth_error("invalid_token", 401)
    if not has_me_scopes(token):
        return oauth_error("insufficient_scope", 403)

    user = token.user
    email_verified = user.email_verified
    if not user.can_access_core(email_verified=email_verified):
        return oauth_error("account_inactive", 403)

    return no_store(
        JsonResponse(
            {
                "data": {
                    "sub": str(user.pk),
                    "name": user.name,
                    "email": user.email,
                    "email_verified": email_verified,
                    "locale": user.locale,
                    "status": user.status,
                    "roles": list(user.groups.order_by("name").values_list("name", flat=True)),
                }
            }
        )
    )
