from collections import defaultdict
from datetime import datetime
from uuid import uuid5

from django.db import migrations
from django.utils import timezone

from farms.migrations._lifecycle_rollback import validate_lifecycle_rollback

CLASSIFICATION_ACTION = "farm.product.lifecycle_backfill"
CLASSIFICATION_BATCH_SIZE = 500


def classify_disabled_sources(apps, schema_editor):
    from farms.migrations._lifecycle_preflight import validate_lifecycle_preflight

    connection = schema_editor.connection
    now = timezone.now()
    validate_lifecycle_preflight(apps, connection, now=now)

    Source = apps.get_model("farms", "FarmProduct")
    AuditLog = apps.get_model("content", "ContentAuditLog")
    sources = Source.objects.using(connection.alias)
    audit = AuditLog.objects.using(connection.alias)
    # Generation and version were initialized on expansion. Only the legacy
    # disabled state needs a synthetic timestamp and documented provenance.
    unclassified = sources.filter(is_active=False, removed_at__isnull=True).order_by("pk")
    previous_pk = None
    while True:
        candidates = (
            unclassified if previous_pk is None else unclassified.filter(pk__gt=previous_pk)
        )
        rows = list(
            candidates.values_list("pk", "farm_id", "product_id")[:CLASSIFICATION_BATCH_SIZE]
        )
        if not rows:
            break
        event_ids = {source_id: uuid5(source_id, CLASSIFICATION_ACTION) for source_id, _, _ in rows}
        # Keep the original classification timestamp if a safe rollback removed
        # the additive fields and the operator is now reapplying this migration.
        prior_events = audit.in_bulk(event_ids.values())
        events = []
        sources_by_timestamp = defaultdict(list)
        for source_id, farm_id, product_id in rows:
            event_id = event_ids[source_id]
            prior = prior_events.get(event_id)
            if prior is not None:
                classified_at = datetime.fromisoformat(prior.context["classified_at"])
            else:
                classified_at = now
                events.append(
                    AuditLog(
                        id=event_id,
                        action=CLASSIFICATION_ACTION,
                        entity_type="farmproduct",
                        entity_id=str(source_id),
                        context={
                            "farm_id": str(farm_id),
                            "product_id": str(product_id),
                            "supply_generation": 1,
                            "classified_at": now.isoformat(),
                            "provenance": "legacy_disabled_link",
                        },
                    )
                )
            sources_by_timestamp[classified_at].append(source_id)
        audit.bulk_create(events)
        for classified_at, source_ids in sources_by_timestamp.items():
            sources.filter(pk__in=source_ids).update(removed_at=classified_at)
        previous_pk = rows[-1][0]


class Migration(migrations.Migration):
    dependencies = [("farms", "0005_farm_product_lifecycle_expand")]

    operations = [
        migrations.RunPython(classify_disabled_sources, validate_lifecycle_rollback, atomic=True),
    ]
