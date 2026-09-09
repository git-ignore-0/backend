import re


class InvalidPhoneNumber(ValueError):
    pass


_VIETNAM_NATIONAL_NUMBER = re.compile(r"[1-9][0-9]{8,9}")


def normalize_vietnam_phone(value: str) -> str:
    """Return an unambiguous Vietnamese phone number in E.164 form."""
    raw = value.strip()
    if raw.startswith("+84"):
        national = raw[3:]
    elif raw.startswith("84"):
        national = raw[2:]
    elif raw.startswith("0"):
        national = raw[1:]
    else:
        raise InvalidPhoneNumber("Enter a Vietnamese phone number starting with 0, 84, or +84.")
    if not _VIETNAM_NATIONAL_NUMBER.fullmatch(national):
        raise InvalidPhoneNumber("Enter a valid Vietnamese phone number.")
    return f"+84{national}"
