from django.db import migrations, models


MATCHES_STATUS = (
    models.Q(status="draft", is_active=True, is_published=False)
    | models.Q(status="active", is_active=True, is_published=True)
    | models.Q(status="discontinued", is_active=False, is_published=False)
)


def check_legacy_flags(apps, schema_editor):
    product = apps.get_model("commerce", "Product")
    mismatches = list(
        product.objects.using(schema_editor.connection.alias)
        .exclude(MATCHES_STATUS)
        .order_by("pk")
        .values_list("pk", flat=True)[:10]
    )
    if mismatches:
        examples = ", ".join(str(product_id) for product_id in mismatches)
        raise RuntimeError(
            "Commerce Product status and legacy flags disagree. "
            f"Review these product IDs before applying the constraint: {examples}"
        )


class Migration(migrations.Migration):
    dependencies = [("commerce", "0014_product_status_required")]

    operations = [
        migrations.RunPython(check_legacy_flags, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="product",
            constraint=models.CheckConstraint(
                condition=MATCHES_STATUS,
                name="commerce_product_status_legacy_flags_match",
            ),
        ),
    ]
