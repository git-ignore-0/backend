"""Login identifiers only; normalization does not verify subscriber ownership."""

import re

from django.core.exceptions import ValidationError

# Explicit presentation whitelist: ASCII space, parentheses, hyphen and dot.
_PRESENTATION = str.maketrans("", "", " ()-.")
_ALLOWED = re.compile(r"[0-9+ ().-]+")
# Vietnamese mobile (9 national digits) or geographic landline (10).
_NATIONAL = re.compile(r"(?:[35789][0-9]{8}|2[0-9]{9})")


def normalize_login_phone(value: str) -> str:
    """Normalize a Vietnamese 0/84/+84 number, rejecting other characters."""
    if not isinstance(value, str) or not _ALLOWED.fullmatch(value):
        raise ValidationError("Enter a valid Vietnamese login phone number.")
    compact = value.translate(_PRESENTATION)
    if compact.startswith("+84"):
        national = compact[3:]
    elif compact.startswith("84"):
        national = compact[2:]
    elif compact.startswith("0"):
        national = compact[1:]
    else:
        raise ValidationError("Use a Vietnamese phone number starting with 0, 84, or +84.")
    if not _NATIONAL.fullmatch(national):
        raise ValidationError("Enter a valid Vietnamese login phone number.")
    return f"+84{national}"
