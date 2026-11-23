"""The Admin configuration API groups real CSA occurrences without changing the member API."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from memberships import csa_rounds
from memberships.csa_configuration_views import occurrence_search
from memberships.models import CSARound
from tests.test_csa_round_api_fixes import admin_client as admin_client
from tests.test_csa_rounds import context as context
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db

ROUNDS = "/api/v1/admin/csa-rounds"
CONFIGURATIONS = "/api/v1/admin/csa-round-configurations"


def create_body(context, **extra):
    now = context[4]
    return {
        "name": "Weekly harvest",
        "opens_at": now.isoformat(),
        "closes_at": (now + timedelta(hours=2)).isoformat(),
        "delivery_date": (timezone.localdate(now) + timedelta(days=1)).isoformat(),
        **extra,
    }


def test_lists_configurations_and_their_occurrences(context, admin_client, monkeypatch):
    response = admin_client.post(
        CONFIGURATIONS, create_body(context, recurrence_mode="draft"), format="json"
    )
    assert response.status_code == 201, response.json()
    configuration = response.json()["data"]
    configuration_id = configuration["id"]
    path = f"{CONFIGURATIONS}/{configuration_id}"
    assert admin_client.post(f"{path}/publish", {}, format="json").status_code == 200
    [first] = admin_client.get(f"{path}/rounds").json()["data"]
    assert first["id"] != configuration_id

    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=6))
    call_command("process_csa_rounds")
    response = admin_client.get(f"{CONFIGURATIONS}?type=weekly&page_size=1")
    assert response.status_code == 200, response.json()
    assert response["Cache-Control"] == "private, no-store"
    assert response.json()["meta"] == {"page": 1, "page_size": 1, "total": 1}
    assert response.json()["data"] == [
        {
            "id": configuration_id,
            "name": configuration["name"],
            "type": "weekly",
            "status": "published",
            "created_at": configuration["created_at"],
        }
    ]

    [child, initial] = admin_client.get(f"{path}/rounds").json()["data"]
    assert initial["id"] == first["id"]
    assert child["status"] == "draft"
    assert child["configuration_id"] == configuration_id
    assert child["recurrence"]["root_round_id"] == first["id"]
    assert child["configuration_name"] == configuration["name"]
    assert child["participant_count"] == 0
    assert child["can_edit"] is True
    assert admin_client.get(f"{CONFIGURATIONS}/{child['id']}").status_code == 404
    assert (
        admin_client.get(f"{ROUNDS}/{child['id']}").json()["data"]["recurrence"]
        == child["recurrence"]
    )

    [standalone] = admin_client.get(f"{CONFIGURATIONS}?type=standalone").json()["data"]
    assert standalone["id"] == str(context[3].configuration_id)
    [only_round] = admin_client.get(f"{CONFIGURATIONS}/{standalone['id']}/rounds").json()["data"]
    assert only_round["id"] == str(context[3].pk)


def test_stops_configuration_without_removing_occurrences(context, admin_client, monkeypatch):
    config = admin_client.post(
        CONFIGURATIONS, create_body(context, recurrence_mode="auto"), format="json"
    ).json()["data"]
    path = f"{CONFIGURATIONS}/{config['id']}"
    assert admin_client.post(f"{path}/stop", {}, format="json").status_code == 409
    assert admin_client.post(f"{path}/publish", {}, format="json").status_code == 200
    [first] = admin_client.get(f"{path}/rounds").json()["data"]
    assert (
        admin_client.post(
            f"{ROUNDS}/{first['id']}/cancel", {"reason": "No harvest"}, format="json"
        ).status_code
        == 200
    )
    assert admin_client.get(path).json()["data"]["status"] == "published"

    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=7))
    call_command("process_csa_rounds")
    before = admin_client.get(f"{path}/rounds").json()["data"]
    assert len(before) == 2
    for _ in range(2):
        response = admin_client.post(f"{path}/stop", {}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["data"]["status"] == "cancelled"
    assert (
        admin_client.get(f"{CONFIGURATIONS}?status=cancelled").json()["data"][0]["id"]
        == config["id"]
    )
    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=14))
    call_command("process_csa_rounds")
    after = admin_client.get(f"{path}/rounds").json()["data"]
    assert [(row["id"], row["cancelled_by_id"]) for row in after] == [
        (row["id"], row["cancelled_by_id"]) for row in before
    ]


def test_configuration_filters_are_strict_and_admin_scoped(context, admin_client):
    assert admin_client.get(f"{CONFIGURATIONS}?type=monthly").status_code == 400
    assert admin_client.get(f"{CONFIGURATIONS}?status=open&status=closed").status_code == 400
    assert admin_client.get(f"{CONFIGURATIONS}?search=anything").status_code == 400
    assert admin_client.get(f"{CONFIGURATIONS}?status=open").status_code == 400
    assert (
        admin_client.post(
            f"{CONFIGURATIONS}/{context[3].configuration_id}/stop", {}, format="json"
        ).status_code
        == 409
    )
    assert admin_client.post(ROUNDS, create_body(context), format="json").status_code == 405
    assert (
        admin_client.post(
            CONFIGURATIONS, create_body(context, recurrence_mode="monthly"), format="json"
        ).status_code
        == 400
    )
    member = APIClient()
    member.force_authenticate(context[0], token=SimpleNamespace(scope="profile email"))
    assert member.get(CONFIGURATIONS).status_code == 403
    assert "recurrence" not in member.get("/api/v1/csa-rounds").json()["data"][0]


def test_generated_occurrence_search_filters_before_paging(context, admin_client, monkeypatch):
    root = admin_client.post(
        CONFIGURATIONS, create_body(context, recurrence_mode="auto"), format="json"
    ).json()["data"]
    root_id = root["id"]
    assert (
        admin_client.post(f"{CONFIGURATIONS}/{root_id}/publish", {}, format="json").status_code
        == 200
    )
    path = f"{CONFIGURATIONS}/{root_id}/rounds"
    assert admin_client.get(path).json()["meta"]["total"] == 1
    assert admin_client.get(f"{path}?generated_only=true").json()["meta"]["total"] == 0

    for days in (7, 14):
        monkeypatch.setattr(timezone, "now", lambda days=days: context[4] + timedelta(days=days))
        call_command("process_csa_rounds")
    response = admin_client.get(f"{path}?generated_only=true&search=harvest&page_size=1")
    assert response.status_code == 200, response.json()
    assert response.json()["meta"] == {"page": 1, "page_size": 1, "total": 2}
    [child] = response.json()["data"]
    assert child["id"] != root_id
    delivery = date.fromisoformat(child["delivery_date"])
    date_query = delivery.strftime("%d/%m/%Y")
    assert (
        admin_client.get(path, {"generated_only": "true", "search": date_query}).json()["meta"][
            "total"
        ]
        == 1
    )
    assert (
        admin_client.get(path, {"generated_only": "true", "search": "no match"}).json()["meta"][
            "total"
        ]
        == 0
    )


def test_occurrence_search_uses_vietnam_day_as_timestamp_bounds(context):
    round_ = context[3]
    query = CSARound.objects.filter(occurrence_search("23/11/2026"))
    start = datetime(2026, 11, 22, 17, tzinfo=UTC)
    end = start + timedelta(days=1)
    before = start - timedelta(microseconds=1)
    later = end + timedelta(hours=1)
    for opens_at, closes_at, expected in (
        (before, end, False),
        (start, later, True),
        (before, start, True),
        (end, later, False),
    ):
        CSARound.objects.filter(pk=round_.pk).update(
            opens_at=opens_at, closes_at=closes_at, delivery_date=date(2026, 11, 26)
        )
        assert query.exists() is expected

    sql, _ = query.query.sql_with_params()
    assert "cast_date" not in sql.lower()
    assert "CONVERT_TZ" not in sql


@pytest.mark.parametrize("mode", [None, "draft", "auto"])
def test_configuration_draft_does_not_create_a_round_and_publish_is_idempotent(
    context, admin_client, mode
):
    count = CSARound.objects.count()
    response = admin_client.post(
        CONFIGURATIONS, create_body(context, recurrence_mode=mode), format="json"
    )
    assert response.status_code == 201, response.json()
    configuration = response.json()["data"]
    assert configuration["status"] == "draft"
    assert configuration["published_at"] is None
    assert configuration["created_at"]
    path = f"{CONFIGURATIONS}/{configuration['id']}"
    assert CSARound.objects.count() == count
    assert admin_client.get(f"{path}/rounds").json()["data"] == []
    assert admin_client.patch(path, {"name": "Updated"}, format="json").status_code == 200
    for _ in range(2):
        response = admin_client.post(f"{path}/publish", {}, format="json")
        assert response.status_code == 200, response.json()
        assert response.json()["data"]["status"] == "published"
        assert response.json()["data"]["published_at"]
    assert CSARound.objects.count() == count + 1
    [round_] = admin_client.get(f"{path}/rounds").json()["data"]
    assert round_["name"] == "Updated"
    assert round_["configuration_id"] == configuration["id"]
    assert admin_client.delete(path).status_code == 409
    assert admin_client.patch(path, {"name": "Too late"}, format="json").status_code == 409


@pytest.mark.parametrize("mode", [None, "draft"])
def test_deleting_a_draft_configuration_keeps_round_count(context, admin_client, mode):
    count = CSARound.objects.count()
    response = admin_client.post(
        CONFIGURATIONS, create_body(context, recurrence_mode=mode), format="json"
    )
    config = response.json()["data"]
    path = f"{CONFIGURATIONS}/{config['id']}"
    assert (
        admin_client.get(f"{CONFIGURATIONS}?status=draft").json()["data"][0]["id"] == config["id"]
    )
    assert admin_client.delete(path).status_code == 204
    assert admin_client.get(path).status_code == 404
    assert admin_client.post(f"{path}/publish", {}, format="json").status_code == 404
    assert CSARound.objects.count() == count


@pytest.mark.django_db(transaction=True)
def test_backfill_preserves_round_ids_selections_and_draft_companion_delivery(context):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    from memberships.models import CSARoundConfiguration, CSASelection

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    legacy = [("memberships", "0039_csa_round_recurrence")]
    backfilled = [("memberships", "0041_csa_round_configuration_backfill")]
    try:
        executor.migrate(legacy)
        apps = executor.loader.project_state(legacy).apps
        OldRound = apps.get_model("memberships", "CSARound")
        OldSchedule = apps.get_model("memberships", "CSARoundRecurrence")
        OldSelection = apps.get_model("memberships", "CSASelection")
        now = context[4]
        dates = {
            "opens_at": now,
            "closes_at": now + timedelta(hours=2),
            "delivery_date": timezone.localdate(now) + timedelta(days=1),
        }
        first = OldRound.objects.create(sequence=2, name="Edited round", published_at=now, **dates)
        schedule = OldSchedule.objects.create(
            root_round=first,
            mode="draft",
            anchor_name="Original configuration",
            anchor_opens_at=dates["opens_at"],
            anchor_closes_at=dates["closes_at"],
            anchor_delivery_date=dates["delivery_date"],
            next_week=2,
            stopped_at=now + timedelta(days=8),
        )
        child = OldRound.objects.create(
            sequence=3,
            name="Generated",
            recurrence=schedule,
            recurrence_week=1,
            **{key: value + timedelta(weeks=1) for key, value in dates.items()},
        )
        cutoff = now + timedelta(hours=1)
        draft = OldRound.objects.create(
            sequence=4,
            name="Unpublished",
            companion_delivery_enabled=True,
            companion_delivery_cutoff=cutoff,
            **dates,
        )
        selection = OldSelection.objects.create(
            round=first, membership_id=context[2].pk, state="selecting"
        )

        MigrationExecutor(connection).migrate(backfilled)
        MigrationExecutor(connection).migrate(legacy)
        restored_draft = OldRound.objects.get(pk=draft.pk, published_at__isnull=True)
        assert restored_draft.companion_delivery_enabled
        assert restored_draft.companion_delivery_cutoff == cutoff
        assert OldRound.objects.get(pk=child.pk).recurrence.root_round_id == first.pk

        MigrationExecutor(connection).migrate(latest)
        configuration = CSARoundConfiguration.objects.get(pk=first.pk)
        assert configuration.name == "Original configuration"
        assert configuration.status == "cancelled"
        assert configuration.created_at == first.created_at
        assert list(configuration.rounds.values_list("pk", flat=True)) == [first.pk, child.pk]
        assert CSARound.objects.get(pk=first.pk).name == "Edited round"
        assert CSASelection.objects.get(pk=selection.pk).round_id == first.pk
        draft_configuration = CSARoundConfiguration.objects.get(pk=draft.pk)
        assert draft_configuration.status == "draft"
        assert not draft_configuration.rounds.exists()
        assert not CSARound.objects.filter(pk=draft.pk).exists()
        with pytest.raises(csa_rounds.CSARoundError, match="csa_companion_deadline_invalid"):
            csa_rounds.update_round_configuration(
                configuration_id=draft_configuration.pk,
                opens_at=now - timedelta(days=1, hours=3),
                closes_at=now - timedelta(days=1, hours=2),
                delivery_date=timezone.localdate(now),
            )
        published = csa_rounds.publish_round_configuration(configuration_id=draft_configuration.pk)
        first_round = published.rounds.get()
        assert first_round.companion_delivery_enabled
        assert first_round.companion_delivery_cutoff == cutoff
    finally:
        MigrationExecutor(connection).migrate(latest)
