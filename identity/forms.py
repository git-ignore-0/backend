from allauth.account.forms import SignupForm
from django import forms


class CoreSignupForm(SignupForm):
    name = forms.CharField(max_length=150, label="Họ và tên")
    locale = forms.ChoiceField(
        choices=(("vi", "Tiếng Việt"), ("en", "English")),
        initial="vi",
        label="Ngôn ngữ",
    )

    def save(self, request):
        user = super().save(request)
        user.name = self.cleaned_data["name"].strip()
        user.locale = self.cleaned_data["locale"]
        user.save(update_fields=["name", "locale", "updated_at"])
        return user
