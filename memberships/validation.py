import re

PHONE_ALLOWED_PATTERN = re.compile(r"^[0-9+().\-\s]+$")


def normalize_membership_phone(value: str) -> str:
    phone = value.strip()
    if not phone or not PHONE_ALLOWED_PATTERN.fullmatch(phone):
        raise ValueError("Enter a valid phone number.")
    digit_count = sum(character.isdigit() for character in phone)
    if not 8 <= digit_count <= 15:
        raise ValueError("Phone number must contain 8 to 15 digits.")
    return phone
