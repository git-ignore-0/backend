"""Force real row-lock races on the production database family."""

from uuid import uuid4

import pytest
from allauth.account.models import EmailAddress
from django.db import connection
from django.utils import timezone
from oauth2_provider.models import AccessToken
from rest_framework.test import APIClient

from content.models import ContentAuditLog
from farms.models import Farm, FarmProductStockBatch, FarmSupplyRequest
from identity.models import AccountStatus, User
from tests.test_farm_bulk_supply import sources as sources
from tests.test_farmer_supply_requests import (
    decide,
    farmer_client,
    link_url,
    submit,
    supply_payload,
)
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_product_lifecycle_locking import run_in_lock_order

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        connection.vendor != "mysql" or not connection.features.has_select_for_update,
        reason="requires MySQL/InnoDB row locks; SQLite cannot verify concurrency",
    ),
]


@pytest.fixture(autouse=True)
def isolate_side_effects(settings):
    settings.CONTENT_FRONTEND_REVALIDATE_URL = ""
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": f"test-farm-locking-{uuid4()}",
        }
    }


@pytest.fixture
def second_admin(farm_admin_api, active_user, oauth_application):
    user = User.objects.create_user(
        email="second-admin@example.com", name="Second admin", status=AccountStatus.ACTIVE
    )
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    user.groups.set(active_user.groups.all())
    token = AccessToken.objects.create(
        user=user,
        application=oauth_application,
        token=f"test-{uuid4()}",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email content:manage",
    )
    return APIClient(HTTP_AUTHORIZATION=f"Bearer {token.token}")


@pytest.mark.parametrize("submit_first", [True, False], ids=["submit-first", "rotate-first"])
def test_rotate_and_submit_serialize(farm_admin_api, sources, submit_first):
    url = link_url(sources[0])
    link = farm_admin_api.post(url, {}, format="json")
    payload = supply_payload(sources)
    sending, rotating = run_in_lock_order(
        lambda: submit(farmer_client(link), payload),
        lambda: farm_admin_api.post(f"{url}/rotate", {}, format="json"),
        left_first=submit_first,
        lock_model=Farm,
    )
    assert rotating.result().status_code == 200
    assert sending.result().status_code == (201 if submit_first else 403)
    assert FarmSupplyRequest.objects.count() == int(submit_first)
    assert not FarmProductStockBatch.objects.exists()
    replay = submit(farmer_client(rotating.result()), payload)
    assert replay.status_code == (200 if submit_first else 201)
    assert FarmSupplyRequest.objects.count() == 1


@pytest.mark.parametrize("same_intent", [True, False], ids=["same-payload", "changed-payload"])
def test_keyed_submissions_serialize(farm_admin_api, sources, same_intent):
    link = farm_admin_api.post(link_url(sources[0]), {}, format="json")
    payload = supply_payload(sources)
    other = supply_payload(sources, idempotency_key=payload["idempotency_key"])
    if not same_intent:
        other["items"][0]["quantity_units"] += 1
    first, second = run_in_lock_order(
        lambda: submit(farmer_client(link), payload),
        lambda: submit(farmer_client(link), other),
        left_first=True,
    )
    assert first.result().status_code == 201
    assert second.result().status_code == (200 if same_intent else 409)
    assert FarmSupplyRequest.objects.count() == 1
    assert not FarmProductStockBatch.objects.exists()


@pytest.mark.parametrize("same_key", [True, False], ids=["same-key", "other-key"])
def test_two_admin_approvals_create_batches_once(farm_admin_api, second_admin, sources, same_key):
    link = farm_admin_api.post(link_url(sources[0]), {}, format="json")
    response = submit(farmer_client(link), supply_payload(sources))
    entry_id = response.json()["data"]["id"]
    key = str(uuid4())
    other_key = key if same_key else str(uuid4())
    first, second = run_in_lock_order(
        lambda: decide(farm_admin_api, entry_id, idempotency_key=key),
        lambda: decide(second_admin, entry_id, idempotency_key=other_key),
        left_first=True,
    )
    assert first.result().status_code == 200
    assert second.result().status_code == (200 if same_key else 409)
    if same_key:
        assert second.result().json()["meta"]["idempotent_replay"] is True
    assert FarmProductStockBatch.objects.count() == len(sources)
    assert ContentAuditLog.objects.filter(action="farm.supply_request.approved").count() == 1


@pytest.mark.parametrize("approve_first", [True, False], ids=["approve-first", "reject-first"])
def test_approve_and_reject_have_one_winner(farm_admin_api, second_admin, sources, approve_first):
    link = farm_admin_api.post(link_url(sources[0]), {}, format="json")
    entry_id = submit(farmer_client(link), supply_payload(sources)).json()["data"]["id"]
    approval, rejection = run_in_lock_order(
        lambda: decide(farm_admin_api, entry_id),
        lambda: decide(second_admin, entry_id, "reject", reason="Rejected in parallel"),
        left_first=approve_first,
        lock_model=Farm,
    )
    assert approval.result().status_code == (200 if approve_first else 409)
    assert rejection.result().status_code == (409 if approve_first else 200)
    assert FarmSupplyRequest.objects.get().status == ("approved" if approve_first else "rejected")
    assert FarmProductStockBatch.objects.count() == (len(sources) if approve_first else 0)
