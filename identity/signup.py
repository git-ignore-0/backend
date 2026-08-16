from urllib.parse import parse_qs, urlsplit

from allauth.account.views import SignupView

from identity.navigation import safe_oauth_signup_next
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
