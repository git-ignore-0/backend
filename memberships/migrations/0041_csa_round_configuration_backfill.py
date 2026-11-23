from django.db import migrations
from django.db.models import Max


def forwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    configuration_model = apps.get_model("memberships", "CSARoundConfiguration")
    round_model = apps.get_model("memberships", "CSARound")
    selection_model = apps.get_model("memberships", "CSASelection")
    configurations = configuration_model.objects.using(alias)
    rounds = round_model.objects.using(alias)
    roots = rounds.filter(recurrence__isnull=True).select_related("recurrence_schedule")
    for root in roots.iterator():
        schedule = getattr(root, "recurrence_schedule", None)
        anchored = schedule is not None and schedule.anchor_opens_at is not None
        draft = root.published_at is None and root.cancelled_at is None
        if draft and (
            selection_model.objects.using(alias).filter(round_id=root.pk).exists()
            or (schedule and rounds.filter(recurrence_id=schedule.pk).exists())
        ):
            raise RuntimeError(f"CSA configuration migration blocked by linked draft {root.pk}")
        configuration = configurations.create(
            id=root.pk,
            name=schedule.anchor_name if anchored else root.name,
            opens_at=schedule.anchor_opens_at if anchored else root.opens_at,
            closes_at=schedule.anchor_closes_at if anchored else root.closes_at,
            delivery_date=schedule.anchor_delivery_date if anchored else root.delivery_date,
            draft_companion_delivery_enabled=draft and root.companion_delivery_enabled,
            draft_companion_delivery_cutoff=root.companion_delivery_cutoff if draft else None,
            recurrence_mode=schedule.mode if schedule else None,
            published_at=root.published_at,
            cancelled_at=(schedule.stopped_at if schedule else None)
            or (root.cancelled_at if root.published_at is None else None),
            next_week=schedule.next_week if schedule else 1,
            next_create_at=schedule.next_create_at if schedule else None,
            created_by_id=root.created_by_id,
        )
        configurations.filter(pk=configuration.pk).update(created_at=root.created_at)
        if draft:
            rounds.filter(pk=root.pk).delete()
        else:
            rounds.filter(pk=root.pk).update(configuration_id=configuration.pk, recurrence_week=0)
            if schedule:
                rounds.filter(recurrence_id=schedule.pk).update(configuration_id=configuration.pk)

    unmapped = list(rounds.filter(configuration__isnull=True).values_list("pk", flat=True)[:10])
    if unmapped:
        raise RuntimeError(f"CSA configuration migration could not link rounds: {unmapped}")


def backwards(apps, schema_editor):
    alias = schema_editor.connection.alias
    configuration_model = apps.get_model("memberships", "CSARoundConfiguration")
    round_model = apps.get_model("memberships", "CSARound")
    schedule_model = apps.get_model("memberships", "CSARoundRecurrence")
    configurations = configuration_model.objects.using(alias)
    rounds = round_model.objects.using(alias)
    schedules = schedule_model.objects.using(alias)
    sequence = rounds.aggregate(value=Max("sequence"))["value"] or 0
    for config in configurations.iterator():
        root = rounds.filter(configuration_id=config.pk, recurrence_week=0).first()
        if root is None:
            sequence += 1
            root = rounds.create(
                id=config.pk,
                sequence=sequence,
                name=config.name,
                opens_at=config.opens_at,
                closes_at=config.closes_at,
                delivery_date=config.delivery_date,
                companion_delivery_enabled=config.draft_companion_delivery_enabled,
                companion_delivery_cutoff=config.draft_companion_delivery_cutoff,
                published_at=config.published_at,
                cancelled_at=config.cancelled_at if config.published_at is None else None,
                created_by_id=config.created_by_id,
                configuration_id=config.pk,
            )
            rounds.filter(pk=root.pk).update(created_at=config.created_at)
        rounds.filter(pk=root.pk).update(recurrence_id=None, recurrence_week=None)
        if config.recurrence_mode is not None:
            active = config.published_at is not None
            schedule, _ = schedules.update_or_create(
                root_round_id=root.pk,
                defaults={
                    "mode": config.recurrence_mode,
                    "anchor_name": config.name if active else None,
                    "anchor_opens_at": config.opens_at if active else None,
                    "anchor_closes_at": config.closes_at if active else None,
                    "anchor_delivery_date": config.delivery_date if active else None,
                    "next_week": config.next_week if active else 1,
                    "next_create_at": config.next_create_at if active else None,
                    "stopped_at": (config.cancelled_at or config.published_at)
                    if active and config.next_create_at is None
                    else None,
                },
            )
            rounds.filter(configuration_id=config.pk, recurrence_week__gt=0).update(
                recurrence_id=schedule.pk
            )
    rounds.update(configuration_id=None)
    configurations.all().delete()


class Migration(migrations.Migration):
    dependencies = [("memberships", "0040_csa_round_configuration_schema")]
    operations = [migrations.RunPython(forwards, backwards)]
