from urllib.parse import parse_qs, urlencode, urlsplit

from allauth.account import signals
from allauth.account.models import EmailAddress
from allauth.account.views import SignupView
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.translation import gettext as _

from identity.models import User
from identity.navigation import LOGIN_NEXT_SESSION_KEY, safe_local_next, safe_oauth_signup_next
from identity.verification import send_account_verification
from referrals.services import is_referral_code_format, normalize_referral_code

SIGNUP_REFERRAL_SESSION_KEY = "identity_signup_referral_code"


def referral_code_from_oauth_continuation(request):
    next_url = safe_oauth_signup_next(request.POST.get("next") or request.GET.get("next"))
    if not next_url:
        return ""
    query = parse_qs(urlsplit(next_url).query)
    code = normalize_referral_code(query.get("referral_code", [""])[0])
    return code if is_referral_code_format(code) else ""


class CoreSignupView(SignupView):
    def dispatch(self, request, *args, **kwargs):
        continuation_code = referral_code_from_oauth_continuation(request)
        session_code = normalize_referral_code(request.session.get(SIGNUP_REFERRAL_SESSION_KEY, ""))
        if not continuation_code or continuation_code != session_code:
            request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["request"] = self.request
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if self.request.method == "POST":
            oauth_next = safe_oauth_signup_next(self.request.POST.get("next"))
            if oauth_next:
                context["language_next"] = f"{self.request.path}?{urlencode({'next': oauth_next})}"
        return context

    def form_valid(self, form):
        from referrals.services import ReferralAlreadyAssigned, ReferralCodeInvalid

        self.user = None
        response = None
        if not form.account_already_exists:
            try:
                with transaction.atomic():
                    self.user, response = form.try_save(self.request)
            except IntegrityError:
                # Check after rollback so an unrelated write failure isn't treated as a duplicate.
                phone = form.cleaned_data["phone"]
                email = form.cleaned_data.get("email")
                if not User.objects.filter(login_phone_number=phone).exists() and not (
                    email
                    and (
                        User.objects.filter(email__iexact=email).exists()
                        or EmailAddress.objects.filter(email__iexact=email).exists()
                    )
                ):
                    raise
            except (ValidationError, ReferralCodeInvalid, ReferralAlreadyAssigned):
                form.add_error(
                    None,
                    _(
                        "Account details changed or are already in use. "
                        "Check your details and try again."
                    ),
                )
                return self.form_invalid(form)
        if response:
            return response
        if self.user is not None:
            signals.user_signed_up.send(
                sender=self.user.__class__, request=self.request, user=self.user
            )
            if self.user.email:
                address = self.user.emailaddress_set.get(email=self.user.email)
                send_account_verification(self.request, address, signup=True)
        message = _("You can sign in to continue.")
        if form.cleaned_data.get("email"):
            message += " " + _(
                "If you provided an email address, check your inbox for a verification link."
            )
        messages.success(self.request, message, extra_tags="toast-manual")
        next_url = safe_local_next(
            self.request.POST.get("next") or self.request.session.get(LOGIN_NEXT_SESSION_KEY)
        )
        if next_url:
            self.request.session[LOGIN_NEXT_SESSION_KEY] = next_url
        url = reverse("account_login")
        if next_url:
            url += "?" + urlencode({"next": next_url})
        return redirect(url)
