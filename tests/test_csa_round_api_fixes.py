"""Regression coverage for reviewed CSA API correctness and bounded queries."""

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AccountStatus, User
from memberships import csa_rounds as service
from memberships.models import (
    CSACustomer,
    CSARound,
    CSASelection,
    CSASelectionEvent,
    CSASelectionLine,
    Membership,
)
from memberships.views import usage_payload, usage_queryset
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(context):
    group, _ = Group.objects.get_or_create(name="website_admin")
    context[0].groups.add(group)
    client = APIClient()
    client.force_authenticate(
        context[0], token=SimpleNamespace(scope="profile email content:manage")
    )
    return client


def test_catalog_own_hold_is_added_before_clamping_availability(context, monkeypatch):
    user, product, _, round_, now = context
    round_.closes_at = now + timedelta(days=5)
    round_.delivery_at = now + timedelta(days=6)
    round_.save()
    save(context)
    monkeypatch.setattr(timezone, "now", lambda: now + timedelta(days=3))
    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    response = client.get(f"/api/v1/csa-rounds/{round_.pk}/products")
    assert response.status_code == 200
    product_data = next(p for p in response.json()["data"] if p["id"] == str(product.pk))
    assert product_data["available_quantity"] == 0
    assert response.json()["selection"]["lines"][0]["quantity"] == 2


def test_member_round_refresh_does_not_rewrite_other_already_ended_membership(context):
    _, _, member, _, now = context
    old = now - timedelta(days=30)
    Membership.objects.filter(pk=member.pk).update(
        start_date=timezone.localdate(now) - timedelta(days=60),
        end_date=timezone.localdate(now) - timedelta(days=30),
        status="ended",
        current_slot=None,
        updated_at=old,
    )
    other = User.objects.create_user(
        email="other-fix@example.com", name="Other", password=None, status=AccountStatus.ACTIVE
    )
    service.process_due_rounds(user=other)
    member.refresh_from_db()
    assert member.updated_at == old
    from memberships.services import refresh_membership_statuses

    refresh_membership_statuses()
    member.refresh_from_db()
    assert member.updated_at == old


def test_usage_history_serialization_needs_no_per_usage_selection_queries(context):
    save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    usages = list(usage_queryset())
    with CaptureQueriesContext(connection) as queries:
        payloads = [usage_payload(usage) for usage in usages]
    assert len(queries) == 0
    assert payloads[0]["order_id"]


def test_round_lists_are_paginated_with_validated_bounds(context, admin_client):
    *_, round_, now = context
    CSARound.objects.bulk_create(
        [
            CSARound(
                name=f"Round {i}",
                sequence=i,
                opens_at=now,
                closes_at=now + timedelta(hours=1),
                delivery_at=now + timedelta(days=1),
            )
            for i in range(2, 25)
        ]
    )
    response = admin_client.get("/api/v1/admin/csa-rounds")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 20
    assert response.json()["meta"] == {"page": 1, "page_size": 20, "total": 24}
    assert len(admin_client.get("/api/v1/admin/csa-rounds?page=2").json()["data"]) == 4
    assert admin_client.get("/api/v1/admin/csa-rounds?page_size=101").status_code == 400


def test_participants_are_paged_demand_is_whole_round_and_history_is_lazy(context, admin_client):
    selected = save(context)
    member = context[2]
    product = context[1]
    for i in range(2):
        user = User.objects.create_user(
            email=f"page{i}@example.com",
            name=f"Member {i}",
            password=None,
            status=AccountStatus.ACTIVE,
        )
        customer = CSACustomer.objects.create(
            user=user,
            name=user.name,
            phone_e164=f"+8491000000{i}",
            province_code="66",
            ward_code="22015",
            address="12 Nguyen Hue",
        )
        extra_member = copy.copy(member)
        extra_member.pk = uuid.uuid4()
        extra_member.customer = customer
        extra_member.farmbrite_order_reference = str(uuid.uuid4())
        extra_member.farmbrite_order_reference_key = str(uuid.uuid4())
        extra_member.save(force_insert=True)
        extra = CSASelection.objects.create(round=context[3], membership=extra_member)
        CSASelectionLine.objects.create(
            selection=extra,
            product=product,
            quantity=i + 1,
            snapshot={"name": "Tomato", "unit_label": "kg", "quota_cost": "1"},
        )
    CSASelectionEvent.objects.bulk_create(
        [
            CSASelectionEvent(selection=selected, action="saved", context={"revision": i})
            for i in range(24)
        ]
    )
    with CaptureQueriesContext(connection) as queries:
        response = admin_client.get(
            f"/api/v1/admin/csa-rounds/{context[3].pk}/selections?page_size=1&locale=en"
        )
    body = response.json()
    assert response.status_code == 200, body
    assert len(body["data"]) == 1
    assert body["meta"]["total"] == 3
    assert all("events" not in row for row in body["data"])
    assert not any("memberships_csaselectionevent" in q["sql"] for q in queries)
    assert body["demand"] == [
        {
            "product_id": str(product.pk),
            "quantity": 5,
            "unit_size": "1.000",
            "name": "Tomato",
            "unit_label": "kg",
        }
    ]
    response = admin_client.get(f"/api/v1/admin/csa-selections/{selected.pk}/events")
    assert response.status_code == 200, response.json()
    assert response.json()["meta"]["total"] == 25
    assert len(response.json()["data"]) == 20
    assert response.json()["data"][0]["context"]["revision"] == 23
    assert (
        len(
            admin_client.get(f"/api/v1/admin/csa-selections/{selected.pk}/events?page=2").json()[
                "data"
            ]
        )
        == 5
    )


def test_round_validation_preserves_shared_field_and_list_errors():
    from memberships.csa_round_views import ReasonInput, RoundInput, SelectionInput

    round_input = RoundInput(data={"unexpected": True})
    assert not round_input.is_valid()
    assert "unexpected" in round_input.errors
    selection_input = SelectionInput(data={"expected_revision": 0, "lines": [{"quantity": 0}]})
    assert not selection_input.is_valid()
    assert isinstance(selection_input.errors["lines"], list)
    assert "product_id" in selection_input.errors["lines"][0]
    reason = ReasonInput(data={"reason": "  No harvest  "})
    assert reason.is_valid()
    assert reason.validated_data["reason"] == "No harvest"


def test_owner_refresh_keeps_terminal_revoke_during_mysql_update_preselect(context, monkeypatch):
    from memberships.services import refresh_membership_statuses

    user, _, member, _, now = context
    Membership.objects.filter(pk=member.pk).update(
        start_date=timezone.localdate(now) - timedelta(days=60),
        end_date=timezone.localdate(now) - timedelta(days=30),
    )
    # MySQL cannot self-select the update target: joined owner filters can become
    # SELECT ids + UPDATE ids, losing transition predicates between statements.
    monkeypatch.setattr(connection.features, "update_can_self_select", False)
    injected = False

    def revoke_before_update(execute, sql, params, many, execution_context):
        nonlocal injected
        if not injected and sql.lstrip().replace("`", '"').startswith(
            'UPDATE "memberships_membership"'
        ):
            injected = True
            Membership.objects.filter(pk=member.pk).update(
                status="revoked",
                current_slot=None,
                revoked_at=now,
                revoked_by=user,
                revoke_reason="Concurrent revoke",
            )
        return execute(sql, params, many, execution_context)

    with connection.execute_wrapper(revoke_before_update):
        refresh_membership_statuses(user=user)
    assert injected
    member.refresh_from_db()
    assert member.status == "revoked"


def test_admin_round_metadata_does_not_process_global_backlog(context, admin_client, monkeypatch):
    selected = save(context)
    monkeypatch.setattr(timezone, "now", lambda: context[3].closes_at)
    for path in [
        "/api/v1/admin/csa-rounds?page_size=1&page=9999",
        f"/api/v1/admin/csa-rounds/{context[3].pk}",
    ]:
        response = admin_client.get(path)
        assert response.status_code == 200
        selected.refresh_from_db()
        assert selected.state == "selecting"


def test_admin_empty_participant_page_does_not_process_other_pages(
    context, admin_client, monkeypatch
):
    selected = save(context)
    monkeypatch.setattr(timezone, "now", lambda: context[3].closes_at)
    response = admin_client.get(
        f"/api/v1/admin/csa-rounds/{context[3].pk}/selections?page_size=1&page=9999"
    )
    assert response.status_code == 200
    assert response.json()["data"] == []
    selected.refresh_from_db()
    assert selected.state == "selecting"
    response = admin_client.get(f"/api/v1/admin/csa-rounds/{context[3].pk}/selections?page_size=1")
    assert response.status_code == 200
    assert response.json()["data"][0]["state"] == "locked"


def test_admin_participant_page_and_history_only_reconcile_requested_selections(
    context, admin_client, monkeypatch
):
    selected = save(context)
    extra_member = copy.copy(context[2])
    extra_member.pk = uuid.uuid4()
    extra_member.current_slot = None
    extra_member.farmbrite_order_reference = str(uuid.uuid4())
    extra_member.farmbrite_order_reference_key = str(uuid.uuid4())
    extra_member.save(force_insert=True)
    other = CSASelection.objects.create(round=context[3], membership=extra_member)
    CSASelection.objects.filter(pk=other.pk).update(created_at=context[4] + timedelta(seconds=1))
    monkeypatch.setattr(timezone, "now", lambda: context[3].closes_at)
    response = admin_client.get(f"/api/v1/admin/csa-rounds/{context[3].pk}/selections?page_size=1")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == str(selected.pk)
    response = admin_client.get(f"/api/v1/admin/csa-selections/{selected.pk}/events")
    assert response.status_code == 200
    other.refresh_from_db()
    assert other.state == "selecting"


def test_demand_excludes_expired_reselection_outside_requested_page(
    context, admin_client, monkeypatch
):
    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    deadline = context[3].delivery_at - timedelta(hours=1)
    service.reopen_selection(
        selection_id=selected.pk,
        actor=context[0],
        deadline=deadline,
        now=context[3].closes_at,
    )
    save(context, 3, now=context[3].closes_at)
    monkeypatch.setattr(timezone, "now", lambda: deadline)
    response = admin_client.get(
        f"/api/v1/admin/csa-rounds/{context[3].pk}/selections?page_size=1&page=9999"
    )
    assert response.status_code == 200
    assert response.json()["demand"] == []
    selected.refresh_from_db()
    assert selected.state == "reselecting"


def test_admin_round_metadata_exposes_editability_without_demand(context, admin_client):
    path = f"/api/v1/admin/csa-rounds/{context[3].pk}"
    with CaptureQueriesContext(connection) as queries:
        response = admin_client.get(path)
    assert response.status_code == 200
    assert response.json()["data"]["can_edit"] is True
    assert not any("SUM(" in q["sql"] for q in queries)
    assert not any("commerce_producttranslation" in q["sql"] for q in queries)
    save(context)
    assert admin_client.get(path).json()["data"]["can_edit"] is False


def test_cancelled_round_metadata_is_not_editable(context, admin_client):
    service.cancel_round(round_id=context[3].pk, actor=context[0], reason="No harvest")
    response = admin_client.get(f"/api/v1/admin/csa-rounds/{context[3].pk}")
    assert response.status_code == 200
    assert response.json()["data"]["can_edit"] is False


def test_reopen_rejects_stale_admin_command_after_member_confirmation(context, admin_client):
    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    selected.refresh_from_db()
    payload = {
        "deadline": (context[3].delivery_at - timedelta(hours=1)).isoformat(),
        "expected_revision": selected.revision,
    }
    path = f"/api/v1/admin/csa-selections/{selected.pk}/reopen"
    assert admin_client.post(path, payload, format="json").status_code == 200
    saved = save(context, 3, now=context[3].closes_at)
    service.confirm_selection(
        selection_id=selected.pk,
        user=context[0],
        now=context[3].closes_at,
        expected_revision=saved.revision,
    )
    response = admin_client.post(path, payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "csa_selection_stale"
    selected.refresh_from_db()
    assert selected.state == "confirmed"
    assert selected.lines.get().quantity == 3
    assert selected.stock_holds.get().quantity == 3
    assert selected.order.status == "pending_review"
    # A deliberately fresh command may still reopen the confirmed choice.
    payload["expected_revision"] = selected.revision
    assert admin_client.post(path, payload, format="json").status_code == 200


def test_admin_reopen_requires_revision(context, admin_client):
    selected = save(context)
    path = f"/api/v1/admin/csa-selections/{selected.pk}/reopen"
    response = admin_client.post(
        path,
        {
            "deadline": (context[3].delivery_at - timedelta(hours=1)).isoformat(),
        },
        format="json",
    )
    assert response.status_code == 400


def test_demand_includes_selling_unit_size(context, admin_client):
    context[1].unit_size = 500
    context[1].save(update_fields=["unit_size"])
    save(context)
    response = admin_client.get(f"/api/v1/admin/csa-rounds/{context[3].pk}/selections")
    assert response.status_code == 200
    assert response.json()["demand"][0]["unit_size"] == "500.000"


def test_confirmation_invalidates_old_deadline_command_and_allows_safe_retries(
    context, admin_client
):
    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    selected.refresh_from_db()
    deadline = context[3].delivery_at - timedelta(hours=1)
    service.reopen_selection(
        selection_id=selected.pk,
        actor=context[0],
        deadline=deadline,
        now=context[3].closes_at,
        expected_revision=selected.revision,
    )
    selected = save(context, 3, now=context[3].closes_at)
    revision = selected.revision
    old_command = {"deadline": deadline.isoformat(), "expected_revision": revision}
    confirm = f"/api/v1/csa-selections/{selected.pk}/confirm"
    payload = {"expected_revision": revision}
    response = admin_client.post(confirm, payload, format="json")
    assert response.status_code == 200
    assert response.json()["data"]["revision"] == revision + 1
    selected.refresh_from_db()
    usage_id = selected.current_usage_id
    # Lost response: replay the original confirmation without a second debit/event.
    assert admin_client.post(confirm, payload, format="json").status_code == 200
    selected.refresh_from_db()
    assert selected.current_usage_id == usage_id
    assert selected.events.filter(action="confirmed").count() == 1
    reopen = f"/api/v1/admin/csa-selections/{selected.pk}/reopen"
    assert admin_client.post(reopen, old_command, format="json").status_code == 409
    selected.refresh_from_db()
    assert selected.state == "confirmed"
    assert selected.lines.get().quantity == 3
    # A fresh admin decision can reopen; an old confirmation cannot confirm that new round.
    old_command["expected_revision"] = selected.revision
    assert admin_client.post(reopen, old_command, format="json").status_code == 200
    assert admin_client.post(confirm, payload, format="json").status_code == 409
