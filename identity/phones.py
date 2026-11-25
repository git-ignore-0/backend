"""Login identifiers only; normalization does not verify subscriber ownership."""

import re

from django.core.exceptions import ValidationError

from config.phones import (
    VIETNAM_COUNTRY_PREFIX,
    VIETNAM_LOCAL_PREFIX,
    strip_phone_presentation,
    vietnam_national_number,
)

# Vietnamese mobile (9 national digits) or geographic landline (10).
_NATIONAL = re.compile(r"(?:[35789][0-9]{8}|2[0-9]{9})")
_PHONE_CHARACTERS = re.compile(r"[0-9+]+")
_INVALID_LOGIN_PHONE = "Enter a valid Vietnamese login phone number."


def _canonical_login_phone(national: str) -> str:
    if not _NATIONAL.fullmatch(national):
        raise ValidationError(_INVALID_LOGIN_PHONE)
    return f"{VIETNAM_COUNTRY_PREFIX}{national}"


def normalize_login_phone(value: str) -> str:
    """Normalize login identifiers received through internal services and APIs."""
    if not isinstance(value, str) or not value:
        raise ValidationError(_INVALID_LOGIN_PHONE)
    compact = strip_phone_presentation(value)
    if compact and not _PHONE_CHARACTERS.fullmatch(compact):
        raise ValidationError(_INVALID_LOGIN_PHONE)
    national = vietnam_national_number(compact)
    if national is None:
        raise ValidationError("Use a Vietnamese phone number starting with 0, 84, or +84.")
    return _canonical_login_phone(national)


def validate_local_login_phone(value: str) -> str:
    """Validate the unchanged form input and return the identifier used by allauth."""
    if not isinstance(value, str) or not value.startswith(VIETNAM_LOCAL_PREFIX):
        raise ValidationError(_INVALID_LOGIN_PHONE)
    return _canonical_login_phone(value.removeprefix(VIETNAM_LOCAL_PREFIX))
