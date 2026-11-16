"""Convert actual delivery instants to Vietnam calendar dates without editing snapshots."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.db import migrations, models
from django.db.migrations.exceptions import IrreversibleError
from django.db.models.functions import TruncDate

VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")


def preflight(apps, schema_editor):
    Round = apps.get_model("memberships", "CSARound")
    Selection = apps.get_model("memberships", "CSASelection")
    alias = schema_editor.connection.alias
    conflicts = []
    for row in Round.objects.using(alias).all().iterator():
        day = row.delivery_at.astimezone(VIETNAM).date()
        start = datetime.combine(day, time.min, tzinfo=VIETNAM)
        if row.closes_at >= start:
            conflicts.append(f"round={row.pk} closes_at={row.closes_at.isoformat()}")
        if row.companion_delivery_cutoff and row.companion_delivery_cutoff >= start:
            conflicts.append(
                f"round={row.pk} companion_delivery_cutoff="
                f"{row.companion_delivery_cutoff.isoformat()}"
            )
        for selected in (
            Selection.objects.using(alias)
            .filter(round_id=row.pk, state="reselecting", reopen_until__gte=start)
            .iterator()
        ):
            conflicts.append(
                f"selection={selected.pk} reopen_until={selected.reopen_until.isoformat()}"
            )
    if conflicts:
        raise RuntimeError(
            "CSA delivery_date conversion blocked: existing deadlines enter the Vietnam "
            "delivery day. No deadlines were shortened or selections cancelled. Resolve "
            "with an explicit operational decision before retrying migration: "
            + "; ".join(conflicts)
        )


def convert_dates(apps, schema_editor):
    Round = apps.get_model("memberships", "CSARound")
    for row in Round.objects.using(schema_editor.connection.alias).all().iterator():
        row.delivery_date = row.delivery_at.astimezone(VIETNAM).date()
        row.save(update_fields=["delivery_date"], using=schema_editor.connection.alias)


def refuse_invented_times(apps, schema_editor):
    Round = apps.get_model("memberships", "CSARound")
    if Round.objects.using(schema_editor.connection.alias).exists():
        raise IrreversibleError(
            "CSA date-only delivery cannot be reversed into invented delivery times; "
            "restore a pre-migration backup for rollback."
        )


class Migration(migrations.Migration):
    dependencies = [("memberships", "0037_csaround_companion_delivery_cutoff_and_more")]

    operations = [
        # Validate before the first schema mutation, also on non-transactional MySQL DDL.
        migrations.RunPython(preflight, migrations.RunPython.noop),
        migrations.RemoveConstraint("csaround", "csa_round_close_before_delivery"),
        migrations.RemoveConstraint("csaround", "csa_companion_cutoff_before_delivery"),
        migrations.AddField("csaround", "delivery_date", models.DateField(null=True)),
        migrations.RunPython(convert_dates, migrations.RunPython.noop),
        migrations.RemoveField("csaround", "delivery_at"),
        migrations.AlterField("csaround", "delivery_date", models.DateField()),
        migrations.AddConstraint(
            "csaround",
            models.CheckConstraint(
                condition=models.Q(
                    delivery_date__gt=TruncDate(
                        models.F("closes_at") + timedelta(hours=7), tzinfo=UTC
                    )
                ),
                name="csa_round_close_before_delivery",
            ),
        ),
        migrations.AddConstraint(
            "csaround",
            models.CheckConstraint(
                condition=models.Q(companion_delivery_cutoff__isnull=True)
                | models.Q(
                    delivery_date__gt=TruncDate(
                        models.F("companion_delivery_cutoff") + timedelta(hours=7), tzinfo=UTC
                    )
                ),
                name="csa_companion_cutoff_before_delivery",
            ),
        ),
        migrations.RunPython(migrations.RunPython.noop, refuse_invented_times),
    ]
