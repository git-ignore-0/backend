from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as django_logout
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from django.views.i18n import set_language as django_set_language

from identity.forms import AddPhoneForm
from identity.navigation import PUBLIC_RETURN_TO_SESSION_KEY, safe_local_next, safe_public_return_to
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
    return redirect(
        "account"
        if request.user.can_access_core() and request.user.email_verified
        else "account_complete"
    )


@require_POST
def set_language(request):
    language = request.POST.get("language")
    if language in {code for code, _name in settings.LANGUAGES} and request.user.is_authenticated:
        if request.user.locale != language:
            request.user.locale = language
            request.user.save(update_fields=["locale", "updated_at"])
    next_url = safe_local_next(request.POST.get("next"))
    if next_url:
        next_path = urlsplit(next_url).path
        if next_path in {
            reverse("account_email_add"),
            reverse("account_phone_add"),
            reverse("account_email_resend"),
        }:
            destination = "account_complete"
            if (
                next_path == reverse("account_phone_add")
                and request.user.is_authenticated
                and request.user.can_access_core()
            ):
                destination = "account"
            request.POST = request.POST.copy()
            request.POST["next"] = reverse(destination)
    return django_set_language(request)


def _account_profile_form(user, data=None):
    profile = MemberProfile.objects.filter(user=user).first()
    initial = {
        "name": user.name,
        "date_of_birth": profile.date_of_birth if profile else None,
    }
    return MemberProfileForm(data, initial=initial)


def render_account_dashboard(request, *, account_user, profile_form=None, phone_form=None):
    if profile_form is None:
        profile_form = _account_profile_form(account_user)
    if phone_form is None and not account_user.login_phone_number:
        phone_form = AddPhoneForm()
    return render(
        request,
        "account/dashboard.html",
        {
            "account_user": account_user,
            "profile_form": profile_form,
            "phone_form": phone_form,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def account(request):
    if not request.user.can_access_core():
        if request.user.can_authenticate():
            return redirect("account_complete")
        return render(request, "account/account_inactive.html", status=403)
    form = _account_profile_form(request.user, request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            profile_changed = update_member_profile(user=request.user, data=form.cleaned_data)
        except PermissionDenied:
            return render(request, "account/account_inactive.html", status=403)
        return_to = safe_public_return_to(request.session.pop(PUBLIC_RETURN_TO_SESSION_KEY, None))
        if return_to:
            return redirect(return_to)
        if profile_changed:
            messages.success(request, _("Changes saved"))
        return redirect("account")
    return render_account_dashboard(request, account_user=request.user, profile_form=form)


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
    if not user.can_access_core():
        return oauth_error("account_inactive", 403)
    profile = MemberProfile.objects.filter(user=user).first()

    return no_store(
        JsonResponse(
            {
                "data": {
                    "sub": str(user.pk),
                    "name": user.name,
                    "email": user.email or None,
                    "email_verified": user.email_verified,
                    "login_phone_number": user.login_phone_number,
                    "locale": user.locale,
                    "status": user.status,
                    "roles": list(user.groups.order_by("name").values_list("name", flat=True)),
                    "phone_number": user.login_phone_number or "",
                    "date_of_birth": (
                        profile.date_of_birth.isoformat()
                        if profile and profile.date_of_birth
                        else None
                    ),
                }
            }
        )
    )
