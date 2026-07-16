from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET

from identity.oauth import access_token_from_request, has_me_scopes


def home(request):
    return redirect("account" if request.user.is_authenticated else "account_login_alias")


@login_required
def account(request):
    if not request.user.email_verified:
        return redirect("account_email_verification_sent")
    if not request.user.can_access_core():
        return render(request, "account/inactive.html", status=403)
    return render(request, "account/dashboard.html")


@require_GET
def health(request):
    return JsonResponse({"status": "ok"})


@require_GET
def me(request):
    token = access_token_from_request(request)
    if token is None:
        return JsonResponse({"code": "invalid_token"}, status=401)
    if not has_me_scopes(token):
        return JsonResponse({"code": "insufficient_scope"}, status=403)

    user = token.user
    if not user.can_access_core():
        return JsonResponse({"code": "account_inactive"}, status=403)

    return JsonResponse(
        {
            "data": {
                "sub": str(user.pk),
                "name": user.name,
                "email": user.email,
                "email_verified": user.email_verified,
                "locale": user.locale,
                "status": user.status,
                "roles": list(user.groups.order_by("name").values_list("name", flat=True)),
            }
        }
    )
