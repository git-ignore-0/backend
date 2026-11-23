"""The Admin configuration API groups real CSA occurrences without changing the member API."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from tests.test_csa_round_api_fixes import admin_client as admin_client
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
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


def test_lists_standalone_and_weekly_roots_without_generated_rounds(
    context, admin_client, monkeypatch
):
    response = admin_client.post(
        ROUNDS, create_body(context, recurrence_mode="draft"), format="json"
    )
    assert response.status_code == 201, response.json()
    root = response.json()["data"]
    schedule = root["recurrence"]
    assert schedule == {
        "root_round_id": root["id"],
        "mode": "draft",
        "status": "pending",
        "next_create_at": None,
        "occurrence_count": 1,
    }
    root_id = root["id"]
    response = admin_client.post(f"{ROUNDS}/{root_id}/publish", {}, format="json")
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["recurrence"]["status"] == "active"
    assert response.json()["data"]["recurrence"]["next_create_at"] is not None

    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=6))
    call_command("process_csa_rounds")
    response = admin_client.get(f"{CONFIGURATIONS}?type=weekly&page_size=1")
    assert response.status_code == 200, response.json()
    assert response["Cache-Control"] == "private, no-store"
    assert response.json()["meta"] == {"page": 1, "page_size": 1, "total": 1}
    configuration = response.json()["data"][0]
    assert configuration["id"] == root_id
    assert configuration["type"] == "weekly"
    assert configuration["recurrence"]["occurrence_count"] == 2

    response = admin_client.get(f"{CONFIGURATIONS}/{root_id}/rounds?status=draft")
    assert response.status_code == 200, response.json()
    [child] = response.json()["data"]
    assert child["id"] != root_id
    assert child["recurrence"]["root_round_id"] == root_id
    assert child["participant_count"] == 0
    assert child["can_edit"] is True
    assert admin_client.get(f"{CONFIGURATIONS}/{child['id']}").status_code == 404
    assert (
        admin_client.get(f"{ROUNDS}/{child['id']}").json()["data"]["recurrence"]
        == child["recurrence"]
    )

    standalone = admin_client.get(f"{CONFIGURATIONS}?type=standalone").json()["data"]
    assert [entry["id"] for entry in standalone] == [str(context[3].pk)]
    assert standalone[0]["type"] == "standalone"
    assert standalone[0]["recurrence"] is None


def test_stops_configuration_without_removing_occurrences(context, admin_client, monkeypatch):
    root = admin_client.post(
        ROUNDS, create_body(context, recurrence_mode="auto"), format="json"
    ).json()["data"]
    path = f"{CONFIGURATIONS}/{root['id']}"
    assert admin_client.post(f"{path}/stop", {}, format="json").status_code == 409
    assert admin_client.post(f"{ROUNDS}/{root['id']}/publish", {}, format="json").status_code == 200
    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=7))
    call_command("process_csa_rounds")
    for _ in range(2):
        response = admin_client.post(f"{path}/stop", {}, format="json")
        assert response.status_code == 200, response.json()
        schedule = response.json()["data"]["recurrence"]
        assert schedule["status"] == "stopped"
        assert schedule["next_create_at"] is None
        assert schedule["occurrence_count"] == 2
    monkeypatch.setattr(timezone, "now", lambda: context[4] + timedelta(days=14))
    call_command("process_csa_rounds")
    assert admin_client.get(f"{path}/rounds").json()["meta"]["total"] == 2


def test_canceling_initial_draft_leaves_a_terminal_configuration(context, admin_client):
    root = admin_client.post(
        ROUNDS, create_body(context, recurrence_mode="draft"), format="json"
    ).json()["data"]
    response = admin_client.post(
        f"{ROUNDS}/{root['id']}/cancel", {"reason": "No harvest"}, format="json"
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["recurrence"]["status"] == "stopped"

    response = admin_client.post(f"{CONFIGURATIONS}/{root['id']}/stop", {}, format="json")
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["recurrence"]["status"] == "stopped"


def test_configuration_filters_are_strict_and_admin_scoped(context, admin_client):
    save(context)
    assert admin_client.get(f"{CONFIGURATIONS}?type=monthly").status_code == 400
    assert admin_client.get(f"{CONFIGURATIONS}?status=open&status=closed").status_code == 400
    assert admin_client.get(f"{CONFIGURATIONS}?search=anything").status_code == 400
    assert admin_client.get(f"{CONFIGURATIONS}?status=open").json()["data"][0]["can_edit"] is False
    assert admin_client.get(f"{CONFIGURATIONS}/{context[3].pk}/rounds").status_code == 404
    assert (
        admin_client.post(
            ROUNDS, create_body(context, recurrence_mode="monthly"), format="json"
        ).status_code
        == 400
    )
    member = APIClient()
    member.force_authenticate(context[0], token=SimpleNamespace(scope="profile email"))
    assert member.get(CONFIGURATIONS).status_code == 403
    assert "recurrence" not in member.get("/api/v1/csa-rounds").json()["data"][0]
