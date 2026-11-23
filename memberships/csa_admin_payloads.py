"""Admin-only CSA metadata. Member and Store round payloads stay independent."""

from django.db.models import Count, Exists, OuterRef
from django.utils import timezone

from memberships.csa_payloads import round_payload
from memberships.csa_rounds import can_edit_round, recurrence_status
from memberships.models import CSARound, CSASelection


def admin_round_queryset():
    return CSARound.objects.select_related("recurrence", "recurrence_schedule").annotate(
        has_csa_selections=Exists(CSASelection.objects.filter(round_id=OuterRef("pk")))
    )


def admin_round_payload(round_id):
    round_ = admin_round_queryset().get(pk=round_id)
    return admin_round_payloads([round_])[0]


def admin_round_payloads(rows, *, now=None, configurations=False, participants=False):
    rows = list(rows)
    now = now or timezone.now()
    schedules = [
        row.recurrence if row.recurrence_id else getattr(row, "recurrence_schedule", None)
        for row in rows
    ]
    schedule_ids = {schedule.pk for schedule in schedules if schedule is not None}
    counts = (
        dict(
            CSARound.objects.filter(recurrence_id__in=schedule_ids)
            .values("recurrence_id")
            .annotate(total=Count("pk"))
            .values_list("recurrence_id", "total")
        )
        if schedule_ids
        else {}
    )
    participant_counts = (
        dict(
            CSASelection.objects.filter(round_id__in=[row.pk for row in rows])
            .values("round_id")
            .annotate(total=Count("pk"))
            .values_list("round_id", "total")
        )
        if participants
        else {}
    )
    result = []
    for row, schedule in zip(rows, schedules, strict=True):
        payload = {
            **round_payload(row, now=now),
            "can_edit": can_edit_round(row),
            "recurrence": None,
        }
        if schedule is not None:
            status = recurrence_status(schedule, row)
            payload["recurrence"] = {
                "root_round_id": str(schedule.root_round_id),
                "mode": schedule.mode,
                "status": status,
                "next_create_at": schedule.next_create_at.isoformat()
                if status == "active" and schedule.next_create_at
                else None,
                "occurrence_count": counts.get(schedule.pk, 0) + 1,
            }
        if configurations:
            payload["type"] = "weekly" if schedule is not None else "standalone"
            if schedule is not None and schedule.anchor_name:
                payload["name"] = schedule.anchor_name
        if participants:
            payload["participant_count"] = participant_counts.get(row.pk, 0)
        result.append(payload)
    return result
