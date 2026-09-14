from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.i18n import set_language as django_set_language

from identity.navigation import PUBLIC_RETURN_TO_SESSION_KEY, safe_public_return_to
from identity.oauth import access_token_from_request, has_me_scopes
from members.forms import MemberProfileForm
from members.models import MemberProfile
from members.services import update_member_profile


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


@require_GET
def public_logout(request):
    requested_locale = request.GET.get("locale")
    supported_locales = {code for code, _name in settings.LANGUAGES}
    locale = requested_locale if requested_locale in supported_locales else settings.LANGUAGE_CODE
    return_to = safe_public_return_to(request.GET.get("return_to"))
    if not return_to:
        return_to = f"{settings.PUBLIC_SITE_ORIGINS[0]}/{locale}"
    django_logout(request)
    response = redirect(return_to)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


@login_required
@require_http_methods(["GET", "POST"])
def fixed_account_email(request):
    return redirect("account")


@require_POST
def set_language(request):
    language = request.POST.get("language")
    if language in {code for code, _name in settings.LANGUAGES} and request.user.is_authenticated:
        if request.user.locale != language:
            request.user.locale = language
            request.user.save(update_fields=["locale", "updated_at"])
    return django_set_language(request)


@login_required
@require_http_methods(["GET", "POST"])
def account(request):
    email_verified = request.user.email_verified
    if not email_verified:
        return redirect("account_email_verification_sent")
    if not request.user.can_access_core(email_verified=email_verified):
        return render(request, "account/account_inactive.html", status=403)
    profile = MemberProfile.objects.filter(user=request.user).first()
    initial = {
        "name": request.user.name,
        "phone_number": profile.phone_number if profile else "",
        "date_of_birth": profile.date_of_birth if profile else None,
        "province_code": profile.province_code if profile else "",
        "ward_code": profile.ward_code if profile else "",
        "address": profile.address if profile else "",
    }
    form = MemberProfileForm(
        request.POST or None,
        initial=initial,
        language_code=request.LANGUAGE_CODE,
    )
    if request.method == "POST" and form.is_valid():
        update_member_profile(user=request.user, data=form.cleaned_data)
        messages.success(request, _("Profile updated."))
        return_to = safe_public_return_to(request.session.pop(PUBLIC_RETURN_TO_SESSION_KEY, None))
        if return_to:
            return redirect(return_to)
        return redirect("account")
    return render(request, "account/dashboard.html", {"profile_form": form})


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
