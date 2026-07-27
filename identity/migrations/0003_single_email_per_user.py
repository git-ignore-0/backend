from django.db import migrations

INDEX_NAME = "identity_one_email_per_user"


def keep_one_email_per_user(apps, schema_editor):
    user_model = apps.get_model("identity", "User")
    email_address = apps.get_model("account", "EmailAddress")

    for user in user_model.objects.iterator():
        addresses = list(email_address.objects.filter(user_id=user.pk).order_by("pk"))
        matching = next(
            (address for address in addresses if address.email.casefold() == user.email.casefold()),
            None,
        )

        if matching is None:
            email_address.objects.filter(user_id=user.pk).delete()
            email_address.objects.create(
                user_id=user.pk,
                email=user.email,
                primary=True,
                verified=user.status != "pending_verification",
            )
            continue

        email_address.objects.filter(user_id=user.pk).exclude(pk=matching.pk).delete()
        if not matching.primary:
            matching.primary = True
            matching.save(update_fields=["primary"])


def create_unique_email_index(apps, schema_editor):
    table = schema_editor.quote_name("account_emailaddress")
    index = schema_editor.quote_name(INDEX_NAME)
    schema_editor.execute(f"CREATE UNIQUE INDEX {index} ON {table} (user_id)")


def drop_unique_email_index(apps, schema_editor):
    table = schema_editor.quote_name("account_emailaddress")
    index = schema_editor.quote_name(INDEX_NAME)
    if schema_editor.connection.vendor == "mysql":
        schema_editor.execute(f"DROP INDEX {index} ON {table}")
    else:
        schema_editor.execute(f"DROP INDEX {index}")


class Migration(migrations.Migration):
    dependencies = [
        ("account", "0009_emailaddress_unique_primary_email"),
        ("identity", "0002_user_identity_user_status_matches_is_active"),
    ]

    operations = [
        migrations.RunPython(keep_one_email_per_user, migrations.RunPython.noop),
        migrations.RunPython(create_unique_email_index, drop_unique_email_index),
    ]
