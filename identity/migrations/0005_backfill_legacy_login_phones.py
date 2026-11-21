"""Backfill unique legacy contact numbers while preserving profile history."""

import re
from collections import Counter

from django.db import IntegrityError, migrations, transaction
from django.db.models import Q

# Freeze the Vietnamese login-phone rules in effect when creating this migration.
PRESENTATION = str.maketrans("", "", " ()-.")
ALLOWED = re.compile(r"[0-9+ ().-]+")
NATIONAL = re.compile(r"(?:[35789][0-9]{8}|2[0-9]{9})")


def normalize_phone(value):
    if not value or not ALLOWED.fullmatch(value):
        return None
    compact = value.translate(PRESENTATION)
    for prefix in ("+84", "84", "0"):
        if compact.startswith(prefix):
            national = compact.removeprefix(prefix)
            if NATIONAL.fullmatch(national):
                return f"+84{national}"
            return None
    return None


def backfill_legacy_login_phones(apps, schema_editor):
    alias = schema_editor.connection.alias
    users = apps.get_model("identity", "User").objects.using(alias)
    profiles = apps.get_model("members", "MemberProfile").objects.using(alias)
    assigned = set()
    for value in (
        users.exclude(login_phone_number=None)
        .values_list("login_phone_number", flat=True)
        .iterator()
    ):
        if phone := normalize_phone(value):
            assigned.add(phone)

    occurrences = Counter()
    candidates = []
    legacy_rows = profiles.exclude(phone_number="").values_list(
        "user_id", "phone_number", "user__login_phone_number", "user__status"
    )
    for user_id, value, login_phone, status in legacy_rows.iterator():
        if phone := normalize_phone(value):
            # All historical numbers still count toward collisions.
            occurrences[phone] += 1
            if not login_phone and status != "anonymized":
                candidates.append((user_id, phone))

    eligible_users = users.exclude(status="anonymized").filter(
        Q(login_phone_number__isnull=True) | Q(login_phone_number="")
    )
    migrated = conflicts = 0
    conflicting_numbers = set()
    for user_id, phone in candidates:
        if phone not in assigned and occurrences[phone] == 1:
            try:
                with transaction.atomic(using=alias):
                    migrated += eligible_users.filter(pk=user_id).update(login_phone_number=phone)
            except IntegrityError:
                # The unique constraint also protects against a concurrent claim.
                if not users.filter(login_phone_number=phone).exclude(pk=user_id).exists():
                    raise
            else:
                continue
        conflicts += 1
        conflicting_numbers.add(phone)

    print(
        f"Legacy login phone backfill migrated={migrated} "
        f"conflicts={conflicts} conflicting_numbers={len(conflicting_numbers)}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0004_phone_identity_activation"),
        ("members", "0002_member_profile_administrative_codes"),
    ]

    operations = [
        migrations.RunPython(backfill_legacy_login_phones, migrations.RunPython.noop, atomic=True),
    ]
