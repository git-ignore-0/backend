from django.db import migrations


def backfill_status(apps, schema_editor):
    product = apps.get_model("commerce", "Product")
    connection = schema_editor.connection
    rows = product.objects.using(connection.alias)
    with connection.cursor() as cursor:
        columns = {
            column.name
            for column in connection.introspection.get_table_description(
                cursor, product._meta.db_table
            )
        }
    if not {"is_active", "is_published", "status"} <= columns:
        raise RuntimeError("Commerce Product schema does not match the status migration baseline.")
    invalid_ids = list(
        rows.filter(is_active=False, is_published=True)
        .order_by("pk")
        .values_list("pk", flat=True)[:10]
    )
    if invalid_ids:
        examples = ", ".join(str(product_id) for product_id in invalid_ids)
        raise RuntimeError(f"Published inactive products found before cutover: {examples}")
    for active, published, status in (
        (True, False, "draft"),
        (True, True, "active"),
        (False, False, "discontinued"),
    ):
        rows.filter(is_active=active, is_published=published).update(status=status)
    if rows.filter(status__isnull=True).exists():
        raise RuntimeError("Product status backfill did not cover every row.")


class Migration(migrations.Migration):
    dependencies = [("commerce", "0012_product_status_expand")]
    operations = [migrations.RunPython(backfill_status, migrations.RunPython.noop)]
