def validate_lifecycle_rollback(apps, schema_editor):
    """Preserve lifecycle metadata once writers have transitioned a source."""
    alias = schema_editor.connection.alias
    sources = apps.get_model("farms", "FarmProduct").objects.using(alias)
    batches = apps.get_model("farms", "FarmProductStockBatch").objects.using(alias)
    audit = apps.get_model("content", "ContentAuditLog").objects.using(alias)
    if (
        sources.exclude(version=1, supply_generation=1, removed_by__isnull=True).exists()
        or batches.exclude(supply_generation=1).exists()
        or audit.filter(
            entity_type="farmproduct", action__in=("farm.product.remove", "farm.product.readd")
        ).exists()
    ):
        raise RuntimeError(
            "FarmProduct lifecycle history already exists. Keep the schema and use a compatible "
            "backend or roll forward."
        )
