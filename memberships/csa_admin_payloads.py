"""Admin-only CSA metadata. Member and Store round payloads stay independent."""

from django.db.models import Count, Exists, OuterRef
from django.utils import timezone

from memberships.csa_payloads import round_payload
from memberships.csa_rounds import can_edit_round
from memberships.models import CSARound, CSASelection


def admin_round_queryset():
    return CSARound.objects.select_related("configuration").annotate(
        has_csa_selections=Exists(CSASelection.objects.filter(round_id=OuterRef("pk")))
    )


def admin_round_payload(round_id):
    round_ = admin_round_queryset().get(pk=round_id)
    return admin_round_payloads([round_])[0]


def admin_round_payloads(rows, *, now=None, participants=False):
    rows = list(rows)
    now = now or timezone.now()
    weekly_ids = {row.configuration_id for row in rows if row.configuration.recurrence_mode}
    weekly_rounds = CSARound.objects.filter(configuration_id__in=weekly_ids)
    first_rounds = dict(
        weekly_rounds.filter(recurrence_week=0).values_list("configuration_id", "pk")
    )
    counts = dict(
        weekly_rounds.values("configuration_id")
        .annotate(total=Count("pk"))
        .values_list("configuration_id", "total")
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
    for row in rows:
        configuration = row.configuration
        payload = {
            **round_payload(row, now=now),
            "configuration_id": str(configuration.pk),
            "configuration_name": configuration.name,
            "can_edit": can_edit_round(row),
            "recurrence": None,
        }
        if configuration.recurrence_mode:
            payload["recurrence"] = {
                "root_round_id": str(first_rounds[configuration.pk]),
                "configuration_name": configuration.name,
                "mode": configuration.recurrence_mode,
                "status": "active" if configuration.next_create_at else "stopped",
                "next_create_at": configuration.next_create_at.isoformat()
                if configuration.next_create_at
                else None,
                "occurrence_count": counts[configuration.pk],
            }
        if participants:
            payload["participant_count"] = participant_counts.get(row.pk, 0)
        result.append(payload)
    return result
