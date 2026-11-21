"""Phone identity foundation; never infer verification or copy contact phones."""

import re
from uuid import UUID

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError


def _email_collations(schema_editor):
    if schema_editor.connection.vendor != "mysql":
        return {}
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, COLLATION_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_NAME = 'email' "
            "AND TABLE_NAME IN ('identity_user', 'account_emailaddress')"
        )
        collations = dict(cursor.fetchall())
    if set(collations) != {"identity_user", "account_emailaddress"}:
        raise RuntimeError("Cannot inspect email collations before identity migration")
    return collations


def _collation_conflicts(changed, tables, schema_editor):
    """Read-only checks using Python-canonical values and each real DB collation.

    Existing canonical rows use their indexes. Pairwise bounded batches are only
    needed for dirty rows, not the entire user population.
    """
    if not changed:
        return set()
    conflicts = set()
    chunks = [changed[start : start + 256] for start in range(0, len(changed), 256)]
    for table, collation in _email_collations(schema_editor).items():
        if not re.fullmatch(r"[a-zA-Z0-9_]+", collation):
            raise RuntimeError("Cannot safely inspect email collation")
        quoted_collation = schema_editor.quote_name(collation)
        value_sql = "%s"
        if schema_editor.connection.vendor == "mysql":
            # Bind in the column charset even if it differs from the connection.
            charset = collation.split("_", 1)[0]
            value_sql = f"CONVERT(%s USING {charset})"

        def derived(rows):
            sql = " UNION ALL ".join(
                f"SELECT %s AS user_id, {value_sql} COLLATE {quoted_collation} AS email"
                for _row in rows
            )
            return sql, [value for row in rows for value in row]

        with schema_editor.connection.cursor() as cursor:
            # Detect changed -> unchanged collisions using the existing column/index.
            for chunk in chunks:
                sql, params = derived(chunk)
                owner_column = schema_editor.quote_name(tables[table])
                quoted_table = schema_editor.quote_name(table)
                cursor.execute(
                    f"SELECT c.user_id, t.{owner_column}, t.email FROM ({sql}) c "
                    f"JOIN {quoted_table} t ON t.email COLLATE {quoted_collation} = c.email",
                    params,
                )
                for candidate, owner, raw in cursor.fetchall():
                    owner = str(UUID(str(owner)))
                    if candidate != owner and raw and raw == raw.strip().lower():
                        conflicts.update([candidate, owner])
            # Detect changed -> changed collisions without unbounded SQL/parameter lists.
            for index, left in enumerate(chunks):
                left_sql, left_params = derived(left)
                for right in chunks[index:]:
                    right_sql, right_params = derived(right)
                    cursor.execute(
                        f"SELECT a.user_id, b.user_id FROM ({left_sql}) a "
                        f"JOIN ({right_sql}) b ON a.email = b.email AND a.user_id <> b.user_id",
                        left_params + right_params,
                    )
                    for first, second in cursor.fetchall():
                        conflicts.update([first, second])
    return conflicts


def check_email_normalization(apps, schema_editor):
    alias = schema_editor.connection.alias
    owners = {}
    changed = set()
    too_long = set()
    tables = {}
    for model_name, owner_field in [("identity.User", "id"), ("account.EmailAddress", "user_id")]:
        app, model = model_name.split(".")
        model = apps.get_model(app, model)
        tables[model._meta.db_table] = model._meta.get_field(owner_field).column
        max_length = model._meta.get_field("email").max_length
        rows = model.objects.using(alias).values_list(owner_field, "email")
        for owner, email in rows.iterator():
            normalized = (email or "").strip().lower()
            if normalized:
                owner = str(owner)
                # Some Unicode characters expand when lowercased.
                if len(normalized) > max_length:
                    too_long.add(owner)
                owners.setdefault(normalized, set()).add(owner)
                if normalized != email:
                    changed.add((owner, normalized))
    if too_long:
        raise RuntimeError(
            "Normalized email exceeds the field length for Core UUIDs: "
            + ", ".join(sorted(too_long))
        )
    conflicts = {owner for ids in owners.values() if len(ids) > 1 for owner in ids}
    conflicts.update(_collation_conflicts(sorted(changed), tables, schema_editor))
    if conflicts:
        # UUIDs only: no personal email addresses in migration error/logs.
        raise RuntimeError(
            "Normalized email conflicts for Core UUIDs: " + ", ".join(sorted(conflicts))
        )


def normalize_emails(apps, schema_editor):
    alias = schema_editor.connection.alias
    for app, model, nullable in [("identity", "User", True), ("account", "EmailAddress", False)]:
        manager = apps.get_model(app, model).objects.using(alias)
        for pk, email in manager.values_list("pk", "email").iterator():
            normalized = (email or "").strip().lower()
            if nullable:
                normalized = normalized or None
            elif not normalized:
                # Preserve invalid historical address rows/verification verbatim;
                # collapsing them to '' could violate allauth's verified uniqueness.
                continue
            if normalized != email:
                manager.filter(pk=pk).update(email=normalized)


def rename_pending(apps, schema_editor):
    apps.get_model("identity", "User").objects.using(schema_editor.connection.alias).filter(
        status="pending_verification"
    ).update(status="pending_activation")


def restore_pending(apps, schema_editor):
    apps.get_model("identity", "User").objects.using(schema_editor.connection.alias).filter(
        status="pending_activation"
    ).update(status="pending_verification")


def guard_reverse(apps, schema_editor):
    users = apps.get_model("identity", "User").objects.using(schema_editor.connection.alias)
    if users.filter(
        models.Q(email__isnull=True) | models.Q(login_phone_number__isnull=False)
    ).exists():
        raise IrreversibleError(
            "Cannot drop phone identity or restore required email with new identity data"
        )


class Migration(migrations.Migration):
    dependencies = [("identity", "0003_single_email_per_user")]

    operations = [
        # Must run before any DDL, including on databases without transactional DDL.
        migrations.RunPython(check_email_normalization, migrations.RunPython.noop),
        migrations.AddField(
            model_name="user",
            name="login_phone_number",
            field=models.CharField(blank=True, max_length=16, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="user",
            name="email",
            field=models.EmailField(blank=True, max_length=254, null=True, unique=True),
        ),
        migrations.RunPython(normalize_emails, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="user",
            name="identity_user_status_matches_is_active",
        ),
        migrations.RunPython(rename_pending, restore_pending),
        migrations.AlterField(
            model_name="user",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending_activation", "Pending activation"),
                    ("active", "Active"),
                    ("suspended", "Suspended"),
                    ("deletion_requested", "Deletion requested"),
                    ("anonymized", "Anonymized"),
                ],
                db_index=True,
                default="pending_activation",
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(status__in=["pending_activation", "active"], is_active=True)
                    | models.Q(
                        status__in=["suspended", "deletion_requested", "anonymized"],
                        is_active=False,
                    )
                ),
                name="identity_user_status_matches_is_active",
            ),
        ),
        # The first operation on reverse: refuse before removing a constraint/column.
        migrations.RunPython(migrations.RunPython.noop, guard_reverse),
    ]
