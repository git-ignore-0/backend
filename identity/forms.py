from allauth.account.forms import LoginForm, SignupForm
from django import forms
from django.conf import settings
from django.utils import translation
from django.utils.translation import gettext_lazy as _


class CoreLoginForm(LoginForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["remember"].label = _("Remember me")
        self.fields["password"].help_text = ""


class CoreSignupForm(SignupForm):
    name = forms.CharField(max_length=150, label=_("Full name"))
    referral_code = forms.CharField(max_length=12, required=False, label=_("Referral code"))

    def clean_referral_code(self):
        from referrals.services import normalize_referral_code, referral_code_exists

        code = normalize_referral_code(self.cleaned_data.get("referral_code", ""))
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
        return user
