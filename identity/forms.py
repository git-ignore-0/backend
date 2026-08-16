from allauth.account.forms import LoginForm, SignupForm
from django import forms
from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext_lazy as _

from identity.signup import SIGNUP_REFERRAL_SESSION_KEY


class CoreLoginForm(LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["remember"].label = _("Remember me")
        self.fields["password"].help_text = ""

    def login(self, request, redirect_url=None):
        response = super().login(request, redirect_url=redirect_url)
        if request.user.is_authenticated:
            request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
        return response


class CoreSignupForm(SignupForm):
    name = forms.CharField(max_length=150, label=_("Full name"))
    referral_code = forms.CharField(max_length=12, required=False, label=_("Referral code"))

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        self.locked_referral_code = ""
        if request is not None:
            from referrals.services import is_referral_code_format, normalize_referral_code

            session_code = normalize_referral_code(
                request.session.get(SIGNUP_REFERRAL_SESSION_KEY, "")
            )
            if is_referral_code_format(session_code):
                self.locked_referral_code = session_code

        if self.locked_referral_code:
            if args and args[0] is not None:
                bound_data = args[0].copy()
                bound_data["referral_code"] = self.locked_referral_code
                args = (bound_data, *args[1:])
            elif kwargs.get("data") is not None:
                bound_data = kwargs["data"].copy()
                bound_data["referral_code"] = self.locked_referral_code
                kwargs["data"] = bound_data

        super().__init__(*args, **kwargs)

        if self.locked_referral_code:
            field = self.fields["referral_code"]
            field.initial = self.locked_referral_code
            self.initial["referral_code"] = self.locked_referral_code
            field.widget.attrs["readonly"] = True
            field.help_text = _(
                "This referral code was applied from your invitation link and cannot be changed."
            )

    def clean_referral_code(self):
        from referrals.services import normalize_referral_code, referral_code_exists

        code = self.locked_referral_code or normalize_referral_code(
            self.cleaned_data.get("referral_code", "")
        )
        if code and not referral_code_exists(code):
            raise forms.ValidationError(_("Enter a valid referral code."))
        return code

    def save(self, request):
        user = super().save(request)
        current_language = translation.get_language()
        supported_languages = {code for code, _name in settings.LANGUAGES}
        user.name = self.cleaned_data["name"].strip()
        user.locale = (
            current_language if current_language in supported_languages else settings.LANGUAGE_CODE
        )
        user.save(update_fields=["name", "locale", "updated_at"])
        referral_code = self.cleaned_data.get("referral_code")
        if referral_code:
            from referrals.services import assign_referrer

            assign_referrer(referee=user, code=referral_code)
        if self.locked_referral_code:
            request.session.pop(SIGNUP_REFERRAL_SESSION_KEY, None)
        return user
