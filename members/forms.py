from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class MemberProfileForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        label=_("Full name"),
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    date_of_birth = forms.DateField(
        label=_("Date of birth"),
        widget=forms.DateInput(
            attrs={
                "autocomplete": "bday",
                "type": "date",
            },
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_of_birth"].widget.attrs["max"] = timezone.localdate().isoformat()

    def clean_date_of_birth(self):
        date_of_birth = self.cleaned_data["date_of_birth"]
        if date_of_birth > timezone.localdate():
            raise forms.ValidationError(_("Date of birth cannot be in the future."))
        return date_of_birth
