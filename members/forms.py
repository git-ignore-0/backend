import re

from django import forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from members.administrative_units import province_choices, ward_choices

PHONE_PATTERN = re.compile(r"^[0-9+().\-\s]*$")


class MemberProfileForm(forms.Form):
    name = forms.CharField(
        max_length=150,
        label=_("Full name"),
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    phone_number = forms.CharField(
        max_length=32,
        label=_("Phone number"),
        widget=forms.TelInput(
            attrs={
                "autocomplete": "tel",
                "data-phone-input": "",
                "inputmode": "tel",
            }
        ),
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
    province_code = forms.ChoiceField(
        label=_("Province or city"),
        widget=forms.Select(
            attrs={
                "autocomplete": "address-level1",
                "data-province-select": "",
            }
        ),
    )
    ward_code = forms.ChoiceField(
        label=_("Ward, commune or special zone"),
        widget=forms.Select(
            attrs={
                "autocomplete": "address-level2",
                "data-ward-select": "",
            }
        ),
    )
    address = forms.CharField(
        max_length=500,
        label=_("Detailed address"),
        help_text=_("House number, street or hamlet only."),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "address-line1",
                "placeholder": _("For example: No. 12, Alley 5"),
            }
        ),
    )

    def __init__(self, *args, language_code="vi", **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_of_birth"].widget.attrs["max"] = timezone.localdate().isoformat()
        self.fields["province_code"].choices = [
            ("", _("Select a province or city")),
            *province_choices(language_code),
        ]

        province_code = ""
        if self.is_bound:
            province_code = self.data.get(self.add_prefix("province_code"), "")
        elif self.initial:
            province_code = self.initial.get("province_code", "")

        self.fields["ward_code"].choices = [
            ("", _("Select a ward, commune or special zone")),
            *ward_choices(province_code, language_code),
        ]
        if not province_code:
            self.fields["ward_code"].widget.attrs["disabled"] = True

    def clean_phone_number(self):
        phone_number = self.cleaned_data["phone_number"].strip()
        if phone_number and not PHONE_PATTERN.fullmatch(phone_number):
            raise forms.ValidationError(_("Enter a valid phone number."))
        return phone_number

    def clean_date_of_birth(self):
        date_of_birth = self.cleaned_data["date_of_birth"]
        if date_of_birth > timezone.localdate():
            raise forms.ValidationError(_("Date of birth cannot be in the future."))
        return date_of_birth
