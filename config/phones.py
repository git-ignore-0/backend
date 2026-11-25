"""Vietnamese phone formats shared by the backend domains."""

import re

from django.db.models import F, Q, Value
from django.db.models.functions import Replace
from django.db.models.lookups import Regex

VIETNAM_COUNTRY_PREFIX = "+84"
VIETNAM_LOCAL_PREFIX = "0"
_STORED_PREFIXES = (VIETNAM_COUNTRY_PREFIX, VIETNAM_COUNTRY_PREFIX[1:], VIETNAM_LOCAL_PREFIX)
_PRESENTATION_SEPARATORS = " ()-."
_PRESENTATION = str.maketrans("", "", _PRESENTATION_SEPARATORS)
_STORED_PREFIX_PATTERN = "(?:" + "|".join(map(re.escape, _STORED_PREFIXES)) + ")"
_CONTACT_NATIONAL = re.compile(r"[1-9][0-9]{8,9}")


class InvalidPhoneNumber(ValueError):
    pass


def vietnam_national_number(value: str) -> str | None:
    for prefix in _STORED_PREFIXES:
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return None


def strip_phone_presentation(value: str) -> str:
    return value.translate(_PRESENTATION)


def normalize_vietnam_phone(value: str) -> str:
    """Normalize a CSA or Store contact using the existing contact-number rules."""
    national = vietnam_national_number(value.strip())
    if national is None:
        raise InvalidPhoneNumber("Enter a Vietnamese phone number starting with 0, 84, or +84.")
    if not _CONTACT_NATIONAL.fullmatch(national):
        raise InvalidPhoneNumber("Enter a valid Vietnamese phone number.")
    return f"{VIETNAM_COUNTRY_PREFIX}{national}"


def format_vietnam_phone(value: str | None) -> str | None:
    """Display Vietnamese numbers domestically and preserve other stored values."""
    if not value:
        return value
    national = vietnam_national_number(strip_phone_presentation(value))
    if national is None or not _CONTACT_NATIONAL.fullmatch(national):
        return value
    return f"{VIETNAM_LOCAL_PREFIX}{national}"


def phone_search(query: str, field: str, *, legacy_presentation: bool = False) -> Q:
    """Match domestic prefixes and national digits without searching the calling code."""
    if len(query) > 11 or not (query.isascii() and query.isdigit()):
        return Q(pk__in=[])

    if query.startswith(VIETNAM_LOCAL_PREFIX):
        national = query[1:]
        # Domestic landlines start with 02 and have one more digit than mobiles.
        full_length = 11 if national.startswith("2") else 10
        exact = len(query) >= full_length
        if not legacy_presentation and national:
            candidates = [prefix + national for prefix in _STORED_PREFIXES]
            if exact:
                return Q(**{f"{field}__in": candidates})
            return Q.create(
                [(f"{field}__startswith", candidate) for candidate in candidates], connector=Q.OR
            )
        pattern = f"^{_STORED_PREFIX_PATTERN}{national}"
        if not exact:
            pattern += "[0-9]*" if national else "[0-9]+"
        pattern += "$"
    else:
        # Evaluate the fragment after consuming the entire stored prefix.
        pattern = f"^{_STORED_PREFIX_PATTERN}[0-9]*{query}[0-9]*$"

    if not legacy_presentation:
        return Q(**{f"{field}__regex": pattern})
    phone = F(field)
    for separator in _PRESENTATION_SEPARATORS:
        phone = Replace(phone, Value(separator))
    return Q(Regex(phone, pattern))
