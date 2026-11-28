from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import (
    ProductTranslation,
    StoreOrderStatus,
    StoreStockAllocation,
    StoreStockMovement,
    StoreStockReservation,
)
from content.models import AdminCreationReceipt, ContentAuditLog
from farms import services
from farms.models import FarmProduct, FarmProductStockBatch, LegacyFarmWeeklySupply
from memberships.models import CSAStockHold
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_farmer_supply_requests import farmer_client, supply_payload
from tests.test_farms_api import allocate_historical_farm_batch
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


def item(product, source=None):
    return {
        "product_id": str(product.pk),
        "expected_source": (
            {"id": str(source.pk), "version": source.version} if source is not None else None
        ),
    }


def revision_command(*items):
    return {"idempotency_key": str(uuid4()), "items": list(items)}


def make_removed_source(farm, product, **revision):
    return FarmProduct.objects.create(
        farm=farm, product=product, removed_at=timezone.now(), is_active=False, **revision
    )


def test_available_products_scopes_source_metadata_search_and_pagination(farm_admin_api):
    farm, other, third = [make_farm(f"Farm {index}") for index in range(3)]
    products = [make_product(f"CATALOG-{i:02d}") for i in range(34)]
    active_elsewhere, active_here, unlinked, removed = products[:4]
    source = make_removed_source(farm, active_elsewhere, version=4, supply_generation=2)
    FarmProduct.objects.create(farm=other, product=active_elsewhere)
    FarmProduct.objects.create(farm=third, product=active_elsewhere)
    FarmProduct.objects.create(farm=farm, product=active_here)
    make_removed_source(other, active_here)
    make_removed_source(farm, removed)
    make_product("DRAFT-OFFER", published=False)
    make_removed_source(farm, make_product("DISCONTINUED-OFFER", active=False, published=False))
    for locale in ("vi", "en"):
        ProductTranslation.objects.update_or_create(
            product=active_elsewhere, locale=locale, defaults={"name": "Winding leaves"}
        )
    url = f"{farm_url(farm)}/available-products"
    with CaptureQueriesContext(connection) as full_page_queries:
        page = farm_admin_api.get(url)
    full_page_query_count = len(full_page_queries)
    with CaptureQueriesContext(connection) as single_row_queries:
        match = farm_admin_api.get(url, {"search": "Winding leaves"})
    single_row_query_count = len(single_row_queries)
    short = farm_admin_api.get(url, {"page": 2})
    assert page.status_code == short.status_code == match.status_code == 200
    assert page["Cache-Control"] == "private, no-store"
    assert page.json()["meta"] == {"page": 1, "page_size": 30, "total": 33}
    assert short.json()["meta"] == {"page": 2, "page_size": 30, "total": 33}
    rows = page.json()["data"] + short.json()["data"]
    assert [row["id"] for row in rows] == [str(row.pk) for row in products if row != active_here]
    assert full_page_query_count == single_row_query_count > 0
    assert (rows[1]["id"], rows[1]["link_state"], rows[1]["removed_source"]) == (
        str(unlinked.pk),
        "unlinked",
        None,
    )
    expected_source = {"id": str(source.pk), "version": 4, "supply_generation": 2}
    assert match.json()["meta"]["total"] == 1
    assert rows[0] == match.json()["data"][0]
    assert (rows[0]["link_state"], rows[0]["removed_source"]) == ("removed", expected_source)
    assert farm_admin_api.get(url, {"search": active_elsewhere.sku}).json() == match.json()
    other_match = farm_admin_api.get(
        f"{farm_url(other)}/available-products", {"search": active_elsewhere.sku}
    )
    assert other_match.json()["meta"]["total"] == 0
    print(f"Available-products GET with 30 rows and removed metadata: {full_page_query_count}")


@pytest.mark.parametrize(("new_count", "removed_count"), [(1, 0), (0, 1), (3, 0), (0, 3), (2, 2)])
def test_revision_linking_applies_lifecycle_and_audit_atomically(
    farm_admin_api, active_user, new_count, removed_count
):
    farm = make_farm()
    new = [make_product(f"NEW-{index}") for index in range(new_count)]
    removed = [
        make_removed_source(
            farm,
            make_product(f"REMOVED-{index}"),
            version=5,
            supply_generation=3,
            removed_by=active_user,
        )
        for index in range(removed_count)
    ]
    items = [item(product) for product in new] + [
        item(source.product, source) for source in removed
    ]
    response = farm_admin_api.post(link_url(farm), revision_command(*items), format="json")
    assert response.status_code == 201, response.json()
    count = new_count + removed_count
    assert response.json()["data"] == {"farm_id": str(farm.pk), "created_count": count}
    assert AdminCreationReceipt.objects.get().created_count == count
    assert FarmProduct.objects.filter(farm=farm).count() == count
    added = [FarmProduct.objects.get(farm=farm, product=product) for product in new]
    for source in added:
        assert (source.version, source.supply_generation, source.is_active) == (1, 1, True)
    for source in removed:
        created_at, updated_at = source.created_at, source.updated_at
        source.refresh_from_db()
        assert (source.version, source.supply_generation, source.is_active) == (6, 4, True)
        assert source.removed_at is None and source.removed_by_id is None
        assert source.updated_at > updated_at and source.created_at == created_at
    audit = ContentAuditLog.objects.all()
    assert audit.count() == count
    for action, sources in [("add", added), ("readd", removed)]:
        for source in sources:
            event = audit.get(entity_id=str(source.pk))
            assert event.actor_id == active_user.pk and event.entity_type == "farmproduct"
            assert event.action == f"farm.product.{action}"
            assert event.context == {
                "farm_id": str(farm.pk),
                "product_id": str(source.product_id),
                "supply_generation": source.supply_generation,
            }
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
    "invalid",
    [
        "mixed_formats",
        "no_format",
        "empty",
        "over_limit",
        "duplicate",
        "invalid_product_id",
        "non_string_product_id",
        "missing_expected_source",
        "invalid_source_id",
        "missing_source_version",
        "zero_source_version",
        "boolean_source_version",
        "string_source_version",
        "float_source_version",
        "unexpected_source_field",
        "unexpected_item_field",
        "unexpected_body_field",
    ],
)
def test_revision_linking_rejects_invalid_schema(farm_admin_api, invalid):
    farm = make_farm()
    product = make_product()
    source = make_removed_source(farm, product)
    current = item(product, source)
    expected = current["expected_source"]
    body = revision_command(current)
    if invalid == "mixed_formats":
        body["product_ids"] = [str(product.pk)]
    elif invalid == "no_format":
        del body["items"]
    elif invalid == "empty":
        body["items"] = []
    elif invalid == "over_limit":
        body["items"] = [{"product_id": str(uuid4()), "expected_source": None} for _ in range(51)]
    elif invalid == "duplicate":
        body["items"].append(item(product))
    elif invalid == "invalid_product_id":
        current["product_id"] = "bad-uuid"
    elif invalid == "non_string_product_id":
        current["product_id"] = 1
    elif invalid == "missing_expected_source":
        del current["expected_source"]
    elif invalid == "invalid_source_id":
        expected["id"] = "bad-uuid"
    elif invalid == "missing_source_version":
        del expected["version"]
    elif invalid == "zero_source_version":
        expected["version"] = 0
    elif invalid == "boolean_source_version":
        expected["version"] = True
    elif invalid == "string_source_version":
        expected["version"] = "1"
    elif invalid == "float_source_version":
        expected["version"] = 1.0
    elif invalid == "unexpected_source_field":
        expected["supply_generation"] = 1
    elif invalid == "unexpected_item_field":
        current["price"] = 100
    else:
        body["start_date"] = str(timezone.localdate())

    response = farm_admin_api.post(link_url(farm), body, format="json")
    assert response.status_code == 400, response.json()
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.exists()
    source.refresh_from_db()
    assert source.removed_at is not None


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("new_became_active", "product_already_linked"),
        ("new_became_removed", "farm_product_removed"),
        ("revision_changed", "farm_product_version_conflict"),
        ("source_replaced", "farm_product_identity_changed"),
        ("source_active", "farm_product_not_removed"),
        ("source_deleted", "farm_product_changed"),
        ("product_inactive", "product_inactive"),
    ],
)
def test_revision_linking_rolls_back_entire_mixed_request_for_stale_item(
    farm_admin_api, state, code
):
    farm = make_farm()
    fresh = make_product("FRESH")
    restorable = make_removed_source(farm, make_product("RESTORABLE"), version=3)
    product = make_product("CHANGED")
    source = None if state.startswith("new_") else make_removed_source(farm, product)
    intent = item(product, source)
    current = source
    if state == "new_became_active":
        current = FarmProduct.objects.create(farm=farm, product=product)
    elif state == "new_became_removed":
        current = make_removed_source(farm, product)
    elif state == "revision_changed":
        source.version = 2
        source.save(update_fields=["version"])
    elif state == "source_replaced":
        source.delete()
        current = make_removed_source(farm, product)
    elif state == "source_active":
        source.removed_at = None
        source.is_active = True
        source.save(update_fields=["removed_at", "is_active"])
    elif state == "source_deleted":
        source.delete()
        current = None
    elif state == "product_inactive":
        type(product).objects.filter(pk=product.pk).update(
            status="discontinued", is_active=False, is_published=False
        )

    body = revision_command(item(fresh), item(restorable.product, restorable), intent)
    response = farm_admin_api.post(link_url(farm), body, format="json")
    assert response.status_code == 409, response.json()
    context = {"item_index": 2, "product_id": str(product.pk)}
    if current is not None:
        context.update(source_id=str(current.pk), version=current.version)
    assert response.json()["errors"] == [{"code": code, "context": context}]
    assert not FarmProduct.objects.filter(product=fresh).exists()
    restorable.refresh_from_db()
    assert restorable.removed_at is not None
    assert (restorable.version, restorable.supply_generation) == (3, 1)
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.exists()


def test_mixed_linking_replays_original_revision_and_rejects_changed_intent(farm_admin_api):
    farm = make_farm()
    new = make_product("NEW")
    source = make_removed_source(farm, make_product("REMOVED"), version=3)
    body = revision_command(item(new), item(source.product, source))
    url = link_url(farm)
    first = farm_admin_api.post(url, body, format="json")
    assert first.status_code == 201, first.json()
    snapshot = first.json()
    assert snapshot["meta"]["idempotent_replay"] is False
    assert snapshot["meta"]["action_receipt"] == {
        "action": "link_farm_products",
        "resource_id": str(farm.pk),
        "idempotency_key": body["idempotency_key"],
    }
    snapshot["meta"]["idempotent_replay"] = True
    for intent in [body, dict(body, items=list(reversed(body["items"])))]:
        replay = farm_admin_api.post(url, intent, format="json")
        assert replay.status_code == 200
        assert replay.json() == snapshot
    lookup = {"creation_key": body["idempotency_key"]}
    assert farm_admin_api.get(url, lookup).json() == snapshot
    assert farm_admin_api.get(link_url(make_farm("Other")), lookup).status_code == 404
    assert farm_admin_api.get(f"{farm_url(farm)}/stock-batches/bulk", lookup).status_code == 404
    source.refresh_from_db()
    assert (source.version, source.supply_generation) == (4, 2)
    for intent in [
        dict(body, items=[item(new), item(source.product, source)]),
        dict(body, items=[item(new)]),
        command(new, source.product, idempotency_key=body["idempotency_key"]),
    ]:
        response = farm_admin_api.post(url, intent, format="json")
        assert response.status_code == 409
        assert error(response) == "idempotency_conflict"
    assert ContentAuditLog.objects.count() == 2
    assert AdminCreationReceipt.objects.count() == 1


def test_legacy_receipt_from_previous_version_still_replays(farm_admin_api, active_user):
    farm = make_farm()
    product = make_product()
    body = command(product)
    material = f'{{"farm_id":"{farm.pk}","product_ids":["{product.pk}"]}}'
    AdminCreationReceipt.objects.create(
        actor=active_user,
        action="link_farm_products",
        key=body["idempotency_key"],
        request_fingerprint=sha256(material.encode()).hexdigest(),
        resource_id=farm.pk,
        created_count=1,
    )
    make_removed_source(farm, product)
    replay = farm_admin_api.post(link_url(farm), body, format="json")
    assert replay.status_code == 200, replay.json()
    assert replay.json()["data"] == {"farm_id": str(farm.pk), "created_count": 1}
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert not ContentAuditLog.objects.exists()
    assert FarmProduct.objects.get().removed_at is not None


@pytest.mark.parametrize(
    ("state", "can_add_supply"),
    [
        ("no_source", False),
        ("removed_only", False),
        ("discontinued_only", False),
        ("draft_only", False),
        ("no_batch", True),
        ("no_remaining_stock", True),
        ("positive_stock", True),
        ("mixed", True),
        ("inactive_farm", True),
    ],
)
def test_farm_supply_capability_uses_source_eligibility(farm_admin_api, state, can_add_supply):
    farm = make_farm(active=state != "inactive_farm")
    if state == "removed_only":
        make_removed_source(farm, make_product())
    elif state in {"draft_only", "discontinued_only"}:
        product = make_product(active=state == "draft_only", published=False)
        FarmProduct.objects.create(farm=farm, product=product)
    elif state != "no_source":
        source = FarmProduct.objects.create(farm=farm, product=make_product())
        today = timezone.localdate()
        if state == "no_remaining_stock":
            _, allocation = allocate_historical_farm_batch(source, StoreOrderStatus.DELIVERED)
            FarmProductStockBatch.objects.filter(pk=allocation.batch_id).update(
                quantity_units=1, start_date=today, end_date=today
            )
        elif state == "positive_stock":
            FarmProductStockBatch.objects.create(
                farm_product=source, quantity_units=10, start_date=today, end_date=today
            )
        elif state == "mixed":
            make_removed_source(farm, make_product("REMOVED"))
            FarmProduct.objects.create(farm=farm, product=make_product("DRAFT", published=False))
    with CaptureQueriesContext(connection) as queries:
        response = farm_admin_api.get(farm_url(farm))
    print(f"Farm detail GET, {state}: {len(queries)} queries")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["capabilities"]["can_add_supply"] is can_add_supply
    if state in ("no_remaining_stock", "positive_stock"):
        detail = farm_admin_api.get(f"{farm_url(farm)}/products/{source.product_id}")
        assert detail.json()["data"]["available_quantity"] == (
            0 if state == "no_remaining_stock" else 10
        )


def test_farm_supply_capability_changes_without_farm_version_change(farm_admin_api):
    farm = make_farm()
    product = make_product()
    url = farm_url(farm)

    def assert_capability(expected):
        detail = farm_admin_api.get(url).json()["data"]
        assert detail["version"] == farm.version
        assert detail["capabilities"]["can_add_supply"] is expected

    assert_capability(False)
    assert (
        farm_admin_api.post(
            link_url(farm), revision_command(item(product)), format="json"
        ).status_code
        == 201
    )
    assert_capability(True)
    source = FarmProduct.objects.get(farm=farm, product=product)
    removed = farm_admin_api.post(
        f"{url}/products/{product.pk}/remove",
        {"expected_link_id": str(source.pk), "expected_version": source.version},
        format="json",
    )
    assert removed.status_code == 200, removed.json()
    assert_capability(False)
    source.refresh_from_db()
    assert (
        farm_admin_api.post(
            link_url(farm), revision_command(item(product, source)), format="json"
        ).status_code
        == 201
    )
    assert_capability(True)


def test_mixed_readd_keeps_historical_inventory_and_farmer_requests(farm_admin_api):
    farm = make_farm()
    product = make_public_product("restore-product", make_category("restore-category"))
    new = make_product("NEW")
    source = FarmProduct.objects.create(farm=farm, product=product)
    today = timezone.localdate()
    batch = FarmProductStockBatch.objects.create(
        farm_product=source, start_date=today, end_date=today, quantity_units=10
    )
    url = farm_url(farm)
    source_url = f"{url}/products/{product.pk}"
    link = farm_admin_api.post(f"{url}/farmer-link", {}, format="json")
    farmer = farmer_client(link)
    requested = farmer.post(
        "/api/v1/farmer/supply-requests", supply_payload([source]), format="json"
    )
    assert requested.status_code == 201, requested.json()
    removed = farm_admin_api.post(
        f"{source_url}/remove",
        {"expected_link_id": str(source.pk), "expected_version": source.version},
        format="json",
    )
    assert removed.status_code == 200, removed.json()
    source.refresh_from_db()
    body = revision_command(item(new), item(product, source))
    restored = farm_admin_api.post(link_url(farm), body, format="json")
    assert restored.status_code == 201, restored.json()

    current = farm_admin_api.get(source_url).json()["data"]
    assert (current["id"], current["available_quantity"], current["active_batch_count"]) == (
        str(source.pk),
        0,
        0,
    )
    history = farm_admin_api.get(f"{source_url}/batches", {"scope": "history"}).json()["data"]
    assert [
        (row["id"], row["available_quantity"], row["remaining_quantity"]) for row in history
    ] == [(str(batch.pk), 0, 10)]
    catalog = APIClient().get(f"/api/v1/public/store-products/{product.slug}?locale=en")
    assert catalog.json()["data"]["available_quantity"] == 0
    expected_sources = {
        str(source.pk),
        str(FarmProduct.objects.get(farm=farm, product=new).pk),
    }
    picker = farmer.get("/api/v1/farmer/products").json()["data"]
    assert {row["source_id"] for row in picker} == expected_sources
    request_id = requested.json()["data"]["id"]
    approval = farm_admin_api.post(
        f"/api/v1/admin/farm-supply-requests/{request_id}/approve",
        {"idempotency_key": str(uuid4())},
        format="json",
    )
    assert approval.status_code == 409
    assert error(approval) == "farm_product_generation_changed"
    assert FarmProductStockBatch.objects.get().pk == batch.pk
    assert not StoreStockMovement.objects.exists()
    assert not StoreStockReservation.objects.exists()
    assert not CSAStockHold.objects.exists()


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


def test_revision_linking_batches_writes_and_preserves_receipt_read_cost(farm_admin_api):
    products = [make_product(f"PROFILE-{index}") for index in range(50)]
    one, many, only_removed, mixed = [make_farm(name) for name in ("One", "Many", "Restore", "Mix")]
    commands = {}
    for label, farm, count, readd_count in [
        ("1 new", one, 1, 0),
        ("50 new", many, 50, 0),
        ("1 re-add", only_removed, 1, 1),
        ("50 re-add", only_removed, 50, 50),
        ("mixed 25+25", mixed, 50, 25),
    ]:
        # The all-readd cases use a separate set of existing sources per operation.
        if label == "50 re-add":
            farm = make_farm("Restore many")
        items = [
            item(product, make_removed_source(farm, product) if index < readd_count else None)
            for index, product in enumerate(products[:count])
        ]
        url = link_url(farm)
        body = revision_command(*items)
        with CaptureQueriesContext(connection) as queries:
            response = farm_admin_api.post(url, body, format="json")
        assert response.status_code == 201, response.json()
        assert response.json()["data"]["created_count"] == count
        commands[label] = len(queries)

    with CaptureQueriesContext(connection) as receipt_queries:
        response = farm_admin_api.get(url, {"creation_key": body["idempotency_key"]})
    assert response.status_code == 200
    assert response.json()["data"]["created_count"] == 50
    print(f"Revision-aware linking POST: {commands}. Receipt GET: {len(receipt_queries)}")
    assert commands["1 new"] == commands["50 new"]
    assert commands["1 re-add"] == commands["50 re-add"]


@pytest.mark.parametrize("revision_aware", [False, True], ids=["legacy", "mixed"])
def test_bulk_link_rechecks_admin_after_waiting_for_locks(
    farm_admin_api, active_user, monkeypatch, revision_aware
):
    farm = make_farm()
    product = make_product()
    body = command(product)
    if revision_aware:
        source = make_removed_source(farm, make_product("REMOVED"))
        body = revision_command(item(product), item(source.product, source))
    lock_sources = services._lock_farm_stock_sources

    def revoke_group(*args, **kwargs):
        active_user.groups.clear()
        return lock_sources(*args, **kwargs)

    monkeypatch.setattr(services, "_lock_farm_stock_sources", revoke_group)
    response = farm_admin_api.post(link_url(farm), body, format="json")
    assert response.status_code == 403
    assert not FarmProduct.objects.filter(product=product).exists()
    if revision_aware:
        source.refresh_from_db()
        assert source.removed_at is not None
        assert (source.version, source.supply_generation) == (1, 1)
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.exists()


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
