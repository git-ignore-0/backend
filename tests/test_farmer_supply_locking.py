"""Force real row-lock races on the production database family."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from allauth.account.models import EmailAddress
from django.db import connection, connections, transaction
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
    make_product_editable,
    read_supply_payload,
    submit,
    supply_payload,
)
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_product_lifecycle_locking import LOCK_TIMEOUT, run_in_lock_order

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


def change_product_identity(client, source):
    url = f"/api/v1/admin/store-products/{source.product_id}"
    return client.patch(url, {"sku": "RACE-NEW-SKU"}, format="json")


@pytest.mark.parametrize("submit_first", [True, False], ids=["submit-first", "edit-first"])
def test_submit_and_product_identity_update_serialize(
    farm_admin_api, second_admin, sources, submit_first
):
    source = sources[0]
    make_product_editable(source.product)
    farmer = farmer_client(farm_admin_api.post(link_url(source), {}, format="json"))
    payload = read_supply_payload(farmer, sources)
    submission, update = run_in_lock_order(
        lambda: submit(farmer, payload),
        lambda: change_product_identity(second_admin, source),
        left_first=submit_first,
    )
    source.product.refresh_from_db()
    assert not FarmProductStockBatch.objects.exists()
    if submit_first:
        assert submission.result().status_code == 201
        assert update.result().status_code == 409
        assert update.result().json()["errors"][0]["code"] == "product_unit_in_use"
        entry = FarmSupplyRequest.objects.get()
        assert entry.items.get(source=source).product_snapshot["sku"] == source.product.sku
    else:
        assert update.result().status_code == 200, update.result().content
        assert source.product.sku == "RACE-NEW-SKU"
        assert submission.result().status_code == 409
        assert submission.result().json() == {
            "errors": [
                {
                    "code": "product_identity_changed",
                    "context": {"item_index": 0, "product_id": str(source.product_id)},
                }
            ]
        }
        assert not FarmSupplyRequest.objects.exists()


@pytest.mark.parametrize("approve_first", [True, False], ids=["approve-first", "edit-first"])
def test_approve_and_product_identity_update_keep_identity_locked(
    farm_admin_api, second_admin, sources, approve_first
):
    source = sources[0]
    farmer = farmer_client(farm_admin_api.post(link_url(source), {}, format="json"))
    entry_id = submit(farmer, supply_payload(sources)).json()["data"]["id"]
    approval, update = run_in_lock_order(
        lambda: decide(farm_admin_api, entry_id),
        lambda: change_product_identity(second_admin, source),
        left_first=approve_first,
    )
    assert approval.result().status_code == 200
    assert update.result().status_code == 409
    assert update.result().json()["errors"][0]["code"] == "product_unit_in_use"
    entry = FarmSupplyRequest.objects.get(pk=entry_id)
    assert entry.status == "approved"
    assert FarmProductStockBatch.objects.count() == len(sources)
    item = entry.items.select_related("source__product").get(source=source)
    assert item.product_snapshot["sku"] == item.source.product.sku


def test_reject_releases_product_identity_only_after_commit(farm_admin_api, second_admin, sources):
    source = sources[0]
    make_product_editable(source.product)
    farmer = farmer_client(farm_admin_api.post(link_url(source), {}, format="json"))
    entry_id = submit(farmer, supply_payload(sources)).json()["data"]["id"]
    decision_written = Event()
    allow_commit = Event()

    def reject():
        db = connections["default"]
        try:
            with transaction.atomic():
                response = decide(farm_admin_api, entry_id, "reject", reason="Test")
                assert response.status_code == 200, response.content
                decision_written.set()
                assert allow_commit.wait(LOCK_TIMEOUT)
        finally:
            decision_written.set()
            db.close()

    with ThreadPoolExecutor(max_workers=1) as worker:
        rejection = worker.submit(reject)
        try:
            assert decision_written.wait(LOCK_TIMEOUT)
            assert FarmSupplyRequest.objects.get(pk=entry_id).status == "pending"
            response = change_product_identity(second_admin, source)
            assert response.status_code == 409
            assert response.json()["errors"][0]["code"] == "product_unit_in_use"
        finally:
            allow_commit.set()
        rejection.result(timeout=LOCK_TIMEOUT)
    assert FarmSupplyRequest.objects.get(pk=entry_id).status == "rejected"
    assert change_product_identity(second_admin, source).status_code == 200
    assert not FarmProductStockBatch.objects.exists()
