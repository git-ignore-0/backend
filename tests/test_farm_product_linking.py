from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import StoreStockAllocation, StoreStockMovement, StoreStockReservation
from content.models import AdminCreationReceipt, ContentAuditLog
from farms import services
from farms.models import FarmProduct, FarmProductStockBatch, LegacyFarmWeeklySupply
from memberships.models import CSAStockHold
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_farmer_supply_requests import farmer_client, supply_payload
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_public_store_catalog_api import make_category
from tests.test_public_store_catalog_api import make_product as make_public_product

pytestmark = pytest.mark.django_db


def farm_url(farm):
    return f"/api/v1/admin/farms/{farm.pk}"


def link_url(farm):
    return f"{farm_url(farm)}/products/bulk"


def command(*products, **kwargs):
    return {
        "idempotency_key": str(uuid4()),
        "product_ids": [str(product.pk) for product in products],
        **kwargs,
    }


def error(response):
    return response.json()["errors"][0]["code"]


@pytest.mark.parametrize("count", [1, 3])
def test_bulk_links_only_and_replays_same_set(farm_admin_api, count):
    farm = make_farm()
    products = [make_product(f"LINK-{i}") for i in range(count)]
    body = command(*products)
    url = link_url(farm)
    created = farm_admin_api.post(url, body, format="json")
    assert created.status_code == 201, created.json()
    snapshot = created.json()
    assert snapshot["data"] == {"farm_id": str(farm.pk), "created_count": count}
    assert snapshot["meta"] == {
        "idempotent_replay": False,
        "action_receipt": {
            "action": "link_farm_products",
            "resource_id": str(farm.pk),
            "idempotency_key": body["idempotency_key"],
        },
    }
    assert created["Cache-Control"] == "private, no-store"
    rows = FarmProduct.objects.filter(farm=farm)
    assert set(rows.values_list("product_id", "version", "supply_generation")) == {
        (product.pk, 1, 1) for product in products
    }

    replay = farm_admin_api.post(
        url, dict(body, product_ids=list(reversed(body["product_ids"]))), format="json"
    )
    assert replay.status_code == 200
    snapshot["meta"]["idempotent_replay"] = True
    assert replay.json() == snapshot
    recovery = farm_admin_api.get(url, {"creation_key": body["idempotency_key"]})
    assert recovery.status_code == 200
    assert recovery.json() == snapshot
    assert recovery["Cache-Control"] == "private, no-store"
    audit = ContentAuditLog.objects.filter(action="farm.product.add")
    assert audit.count() == count
    assert set(audit.values_list("entity_id", flat=True)) == {str(row.pk) for row in rows}
    assert all("stock_batch_id" not in event.context for event in audit)
    assert AdminCreationReceipt.objects.count() == 1
    for model in (
        FarmProductStockBatch,
        StoreStockAllocation,
        StoreStockMovement,
        StoreStockReservation,
        CSAStockHold,
        LegacyFarmWeeklySupply,
    ):
        assert not model.objects.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"product_ids": []},
        {"product_ids": "invalid"},
        {"product_ids": ["invalid"]},
        {"product_ids": [1]},
        {"product_ids": ["00000000-0000-0000-0000-000000000001"] * 2},
        {"product_ids": [str(uuid4()) for _ in range(51)]},
        {"idempotency_key": "invalid"},
        {"idempotency_key": True},
        {"quantity_units": 2},
        {"start_date": "2026-11-28"},
        {"end_date": "2026-11-28"},
        {"farm_id": str(uuid4())},
    ],
)
def test_bulk_rejects_invalid_commands(farm_admin_api, overrides):
    farm = make_farm()
    product = make_product()
    response = farm_admin_api.post(link_url(farm), command(product, **overrides), format="json")
    assert response.status_code == 400, response.json()
    assert not FarmProduct.objects.exists()
    assert not AdminCreationReceipt.objects.exists()


@pytest.mark.parametrize(
    ("case", "status_code", "code"),
    [
        ("missing", 404, "not_found"),
        ("inactive", 409, "product_inactive"),
        ("linked", 409, "product_already_linked"),
        ("removed", 409, "farm_product_removed"),
    ],
)
def test_bulk_item_conflict_never_partially_writes(farm_admin_api, case, status_code, code):
    farm = make_farm()
    good = make_product("LINK-VALID")
    bad = make_product("LINK-INVALID", published=case != "inactive")
    if case == "missing":
        pk = bad.pk
        bad.delete()
        bad.pk = pk
    if case in {"linked", "removed"}:
        FarmProduct.objects.create(
            farm=farm,
            product=bad,
            removed_at=timezone.now() if case == "removed" else None,
            is_active=case != "removed",
        )
    response = farm_admin_api.post(link_url(farm), command(good, bad), format="json")
    assert response.status_code == status_code, response.json()
    assert error(response) == code
    assert response.json()["errors"][0]["context"]["item_index"] == 1
    assert response.json()["errors"][0]["context"]["product_id"] == str(bad.pk)
    assert not FarmProduct.objects.filter(product=good).exists()
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.filter(action="farm.product.add").exists()


def test_link_scope_idempotency_and_permissions(farm_admin_api, active_user, access_token):
    farm, other = make_farm(), make_farm("Other Farm")
    product, different = make_product("ONE"), make_product("TWO")
    body = command(product)
    assert APIClient().post(link_url(farm), body, format="json").status_code == 401
    assert farm_admin_api.post(link_url(farm), body, format="json").status_code == 201
    lookup = {"creation_key": body["idempotency_key"]}
    assert farm_admin_api.get(link_url(other), lookup).status_code == 404
    for target, payload in [
        (other, body),
        (farm, command(different, idempotency_key=body["idempotency_key"])),
    ]:
        response = farm_admin_api.post(link_url(target), payload, format="json")
        assert response.status_code == 409
        assert error(response) == "idempotency_conflict"

    other_actor = type(active_user).objects.create_user(
        email="task07-receipt@example.com", name="Other Admin", status="active"
    )
    other_actor.groups.add(Group.objects.get(name="website_admin"))
    other_admin = APIClient()
    other_admin.force_authenticate(user=other_actor, token=access_token)
    assert other_admin.get(link_url(farm), lookup).status_code == 404
    AdminCreationReceipt.objects.filter(key=body["idempotency_key"]).update(
        action=AdminCreationReceipt.Action.FARM_STOCK_BATCHES
    )
    assert farm_admin_api.get(link_url(farm), lookup).status_code == 404

    active_user.groups.clear()
    assert farm_admin_api.get(link_url(farm), lookup).status_code == 403
    assert farm_admin_api.post(link_url(farm), command(different), format="json").status_code == 403
    assert FarmProduct.objects.count() == 1


def test_missing_farm_and_retired_combined_write(farm_admin_api):
    farm = make_farm()
    product = make_product()
    response = farm_admin_api.post(
        f"/api/v1/admin/farms/{uuid4()}/products/bulk", command(product), format="json"
    )
    assert response.status_code == 404
    today = timezone.localdate().isoformat()
    response = farm_admin_api.post(
        f"{farm_url(farm)}/products",
        {
            "product_id": str(product.pk),
            "start_date": today,
            "end_date": today,
            "quantity_units": 2,
        },
        format="json",
    )
    assert response.status_code == 409
    assert error(response) == "farm_product_legacy_write_disabled"
    assert not FarmProduct.objects.exists()
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()


def test_zero_batch_source_projection_readd_and_first_supply(farm_admin_api):
    farm = make_farm()
    product = make_product()
    url = farm_url(farm)
    source_url = f"{url}/products/{product.pk}"
    assert farm_admin_api.post(link_url(farm), command(product), format="json").status_code == 201
    detail = farm_admin_api.get(url).json()["data"]
    assert detail["in_stock_product_count"] == 0
    assert detail["out_of_stock_product_count"] == 1
    assert detail["capabilities"]["delete"]["reasons"][0]["code"] == "supplying_product"

    with CaptureQueriesContext(connection) as source_queries:
        source = farm_admin_api.get(source_url).json()["data"]
    source_query_count = len(source_queries)
    listed = farm_admin_api.get(f"{url}/products").json()["data"]
    assert listed[0]["id"] == source["id"]
    for row in (source, listed[0]):
        for key in (
            "initial_quantity",
            "net_sold_quantity",
            "available_quantity",
            "batch_count",
            "active_batch_count",
        ):
            assert row[key] == 0
        assert row["next_expiry_date"] is None
    assert source["capabilities"]["can_create_batch"]
    assert farm_admin_api.get(f"{source_url}/batches").json()["data"] == []

    revision = {"expected_link_id": source["id"], "expected_version": source["version"]}
    removed = farm_admin_api.post(f"{source_url}/remove", revision, format="json")
    assert removed.status_code == 200, removed.json()
    assert farm_admin_api.get(url).json()["data"]["capabilities"]["delete"]["allowed"]
    body = dict(
        revision, expected_version=removed.json()["data"]["version"], idempotency_key=str(uuid4())
    )
    readd_url = f"{source_url}/re-add"
    assert (
        farm_admin_api.post(readd_url, dict(body, quantity_units=2), format="json").status_code
        == 400
    )
    restored = farm_admin_api.post(readd_url, body, format="json")
    assert restored.status_code == 200, restored.json()
    snapshot = restored.json()
    assert snapshot["data"]["id"] == source["id"]
    assert snapshot["data"]["supply_generation"] == 2
    assert snapshot["data"]["version"] == 3
    assert snapshot["data"]["batch_count"] == 0
    assert "first_batch_id" not in snapshot["data"]
    assert snapshot["meta"]["action_receipt"] == {
        "action": "readd_farm_product",
        "resource_id": source["id"],
        "idempotency_key": body["idempotency_key"],
    }
    assert not snapshot["meta"]["idempotent_replay"]
    snapshot["meta"]["idempotent_replay"] = True
    lookup = {"creation_key": body["idempotency_key"]}
    assert farm_admin_api.get(readd_url, lookup).json() == snapshot
    assert farm_admin_api.post(readd_url, body, format="json").json() == snapshot
    assert farm_admin_api.get(f"{url}/products/{uuid4()}/re-add", lookup).status_code == 404
    assert farm_admin_api.get(link_url(farm), lookup).status_code == 404
    assert ContentAuditLog.objects.filter(action="farm.product.readd").count() == 1
    assert not FarmProductStockBatch.objects.exists()
    call_command("check_farm_product_lifecycle")

    today = timezone.localdate()
    with CaptureQueriesContext(connection) as stock_queries:
        supply = farm_admin_api.post(
            f"{url}/stock-batches/bulk",
            {
                "idempotency_key": str(uuid4()),
                "start_date": str(today),
                "end_date": str(today + timedelta(days=7)),
                "items": [
                    {
                        "product_id": str(product.pk),
                        "expected_link_id": source["id"],
                        "expected_version": 3,
                        "quantity_units": 4,
                    }
                ],
            },
            format="json",
        )
    assert supply.status_code == 201, supply.json()
    stock_query_count = len(stock_queries)
    assert FarmProductStockBatch.objects.get().supply_generation == 2
    assert farm_admin_api.get(source_url).json()["data"]["available_quantity"] == 4
    print(f"Zero-batch source GET: {source_query_count}. First supply POST: {stock_query_count}.")


def test_readd_receipt_replays_after_later_removal_and_farm_deletion(farm_admin_api):
    farm = make_farm()
    product = make_product()
    source = FarmProduct.objects.create(
        farm=farm, product=product, is_active=False, removed_at=timezone.now()
    )
    url = f"{farm_url(farm)}/products/{product.pk}"
    body = dict(expected_link_id=str(source.pk), expected_version=1, idempotency_key=str(uuid4()))
    restored = farm_admin_api.post(f"{url}/re-add", body, format="json")
    assert restored.status_code == 200, restored.json()
    conflict = farm_admin_api.post(f"{url}/re-add", dict(body, expected_version=2), format="json")
    assert conflict.status_code == 409
    assert error(conflict) == "idempotency_conflict"
    removed = farm_admin_api.post(
        f"{url}/remove", dict(expected_link_id=str(source.pk), expected_version=2), format="json"
    )
    assert removed.status_code == 200
    assert farm_admin_api.delete(farm_url(farm)).status_code == 204

    lookup = {"creation_key": body["idempotency_key"]}
    recovered = farm_admin_api.get(f"{url}/re-add", lookup)
    assert recovered.status_code == 200
    assert recovered.json()["data"] is None
    assert recovered.json()["meta"]["action_receipt"] == restored.json()["meta"]["action_receipt"]
    assert recovered.json()["meta"]["idempotent_replay"] is True
    assert farm_admin_api.post(f"{url}/re-add", body, format="json").json() == recovered.json()
    wrong_farm = f"/api/v1/admin/farms/{uuid4()}/products/{product.pk}/re-add"
    assert error(farm_admin_api.get(wrong_farm, lookup)) == "create_receipt_not_found"
    assert ContentAuditLog.objects.filter(action="farm.product.readd").count() == 1
    assert not FarmProductStockBatch.objects.exists()


def test_farmer_can_request_link_only_product_public_stock_changes_at_approval(farm_admin_api):
    farm = make_farm()
    product = make_public_product("link-only-product", make_category("link-only-category"))
    catalog = APIClient()
    public_list = "/api/v1/public/store-products?locale=en"
    public_detail = f"/api/v1/public/store-products/{product.slug}?locale=en"
    assert catalog.get(public_detail).json()["data"]["available_quantity"] == 0
    assert farm_admin_api.post(link_url(farm), command(product), format="json").status_code == 201
    source = FarmProduct.objects.get()
    detail = catalog.get(public_detail).json()["data"]
    assert detail["available_quantity"] == 0
    assert [supplier["id"] for supplier in detail["suppliers"]] == [str(farm.pk)]
    assert catalog.get(public_list).json()["data"][0]["available_quantity"] == 0

    link = farm_admin_api.post(f"{farm_url(farm)}/farmer-link", {}, format="json")
    farmer = farmer_client(link)
    assert farmer.get("/api/v1/farmer/products").json()["data"][0]["source_id"] == str(source.pk)
    assert farmer.post(link_url(farm), command(product), format="json").status_code == 401
    response = farmer.post(
        "/api/v1/farmer/supply-requests", supply_payload([source]), format="json"
    )
    assert response.status_code == 201, response.json()
    entry = response.json()["data"]
    assert entry["status"] == "pending"
    assert not FarmProductStockBatch.objects.exists()
    approval = farm_admin_api.post(
        f"/api/v1/admin/farm-supply-requests/{entry['id']}/approve",
        {"idempotency_key": str(uuid4())},
        format="json",
    )
    assert approval.status_code == 200, approval.json()
    assert catalog.get(public_detail).json()["data"]["available_quantity"] == 10


def test_link_queries_do_not_increase_per_product(farm_admin_api):
    products = [make_product(f"MEASURE-{index}") for index in range(50)]
    farm_one = make_farm("One")
    farm_many = make_farm("Many")
    with CaptureQueriesContext(connection) as available_queries:
        available = farm_admin_api.get(f"{farm_url(farm_many)}/available-products")
    available_query_count = len(available_queries)
    assert len(available.json()["data"]) == 30
    assert available.json()["meta"]["total"] == 50
    counts = []
    for farm, selected in [(farm_one, products[:1]), (farm_many, products)]:
        body = command(*selected)
        with CaptureQueriesContext(connection) as queries:
            created = farm_admin_api.post(link_url(farm), body, format="json")
        assert created.status_code == 201, created.json()
        counts.append(len(queries))
    with CaptureQueriesContext(connection) as recovery_queries:
        recovered = farm_admin_api.get(
            link_url(farm_many), {"creation_key": body["idempotency_key"]}
        )
    assert recovered.status_code == 200
    assert recovered.json()["data"]["created_count"] == 50
    print(f"Bulk link queries (1, 50 products): {counts}")
    print(f"Available-products GET: {available_query_count}. Receipt GET: {len(recovery_queries)}.")
    assert counts[0] == counts[1]


def test_bulk_link_rechecks_admin_after_waiting_for_locks(farm_admin_api, active_user, monkeypatch):
    farm = make_farm()
    product = make_product()
    lock_sources = services._lock_farm_stock_sources

    def revoke_group(*args, **kwargs):
        active_user.groups.clear()
        return lock_sources(*args, **kwargs)

    monkeypatch.setattr(services, "_lock_farm_stock_sources", revoke_group)
    response = farm_admin_api.post(link_url(farm), command(product), format="json")
    assert response.status_code == 403
    assert not FarmProduct.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.filter(action="farm.product.add").exists()


@pytest.mark.django_db(transaction=True)
def test_receipt_migration_keeps_legacy_rows_and_writers_compatible():
    old_target = [("content", "0022_farm_bulk_creation_receipt")]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old_target)
        old_apps = executor.loader.project_state(old_target).apps
        old_receipt = old_apps.get_model("content", "AdminCreationReceipt")
        stock = old_receipt.objects.create(
            action="create_farm_stock_batches",
            key=uuid4(),
            request_fingerprint="1" * 64,
            resource_id=uuid4(),
            created_count=2,
        )
        MigrationExecutor(connection).migrate(latest)
        upgraded = AdminCreationReceipt.objects.get(pk=stock.pk)
        assert (upgraded.resource_id, upgraded.key, upgraded.created_count) == (
            stock.resource_id,
            stock.key,
            2,
        )
        assert upgraded.resource_scope is None
        created = old_receipt.objects.create(
            action="create_product",
            key=uuid4(),
            request_fingerprint="2" * 64,
            resource_id=uuid4(),
        )
        assert AdminCreationReceipt.objects.get(pk=created.pk).resource_id == created.resource_id
    finally:
        MigrationExecutor(connection).migrate(latest)
