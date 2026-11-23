from django.db import connection

from identity.models import User


def lock_user_for_shared_read(user_id):
    """Keep the User stable during a transaction without blocking other FK readers."""
    if connection.vendor not in {"mysql", "postgresql"}:
        User.objects.only("pk").get(pk=user_id)
        return

    pk = User._meta.pk
    quote = connection.ops.quote_name
    table, column = quote(User._meta.db_table), quote(pk.column)
    clause = "LOCK IN SHARE MODE" if connection.vendor == "mysql" else "FOR SHARE"
    value = pk.get_db_prep_value(user_id, connection)
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {column} FROM {table} WHERE {column} = %s {clause}", [value])
        if cursor.fetchone() is None:
            raise User.DoesNotExist
