from datetime import date, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import StoreOrder, StoreOrderLine
from content.creation_receipts import creation_fingerprint
from content.models import ContentAuditLog, MediaAsset
from farms import supply_requests
from farms.models import (
    FarmFarmerLink,
    FarmProduct,
    FarmProductStockBatch,
    FarmSupplyRequest,
    FarmSupplyRequestItem,
    LegacyFarmWeeklySupply,
)
from farms.supply_request_views import FarmerSubmitThrottle
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_bulk_supply import bulk_payload
from tests.test_farm_bulk_supply import sources as sources
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db

FARMER = "/api/v1/farmer"
ADMIN = "/api/v1/admin/farm-supply-requests"


def link_url(source):
    return f"/api/v1/admin/farms/{source.farm_id}/farmer-link"


def farmer_client(response):
    token = response.json()["data"]["url"].split("#token=")[1]
    return APIClient(HTTP_X_FARM_TOKEN=token)


def supply_payload(sources, **overrides):
    payload = bulk_payload(sources)
    for source, item in zip(sources, payload["items"], strict=True):
        del item["expected_version"]
        item["expected_generation"] = source.supply_generation
        item["expected_product_identity"] = supply_requests.product_identity_fingerprint(
            source.product
        )
    payload.update(overrides)
    return payload


def test_link_submit_rotate_and_approve(
    farm_admin_api, sources, django_capture_on_commit_callbacks
):
    url = link_url(sources[0])
    link = farm_admin_api.post(url, {}, format="json")
    assert link.status_code == 201
    assert link["Cache-Control"] == "private, no-store"
    assert farm_admin_api.get(url).json() == link.json()
    farmer = farmer_client(link)
    payload = supply_payload(sources)
    submitted = farmer.post(f"{FARMER}/supply-requests", payload, format="json")
    assert submitted.status_code == 201, submitted.content
    entry = submitted.json()["data"]
    assert entry["status"] == "pending"
    assert FarmProductStockBatch.objects.count() == 0

    rotated = farm_admin_api.post(f"{url}/rotate", {}, format="json")
    assert rotated.status_code == 200
    assert farmer.get(f"{FARMER}/supply-requests").status_code == 403
    farmer = farmer_client(rotated)
    assert farmer.get(f"{FARMER}/supply-requests/{entry['id']}").json()["data"] == entry
    replay = farmer.post(f"{FARMER}/supply-requests", payload, format="json")
    assert replay.status_code == 200
    assert replay.json()["meta"]["idempotent_replay"] is True

    approve_url = f"{ADMIN}/{entry['id']}/approve"
    intent = {"idempotency_key": str(uuid4())}
    with django_capture_on_commit_callbacks() as callbacks:
        approved = farm_admin_api.post(approve_url, intent, format="json")
    assert approved.status_code == 200, approved.content
    assert approved.json()["data"]["status"] == "approved"
    assert FarmProductStockBatch.objects.count() == 2
    assert len(callbacks) == 1
    with django_capture_on_commit_callbacks() as callbacks:
        replay = farm_admin_api.post(approve_url, intent, format="json")
    assert replay.status_code == 200
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert len(callbacks) == 0
    assert FarmProductStockBatch.objects.count() == 2


@pytest.fixture(autouse=True)
def isolated_throttle_cache(settings):
    settings.CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "test-farmer-supply-requests",
        }
    }
    cache.clear()


@pytest.fixture
def farmer(farm_admin_api, sources):
    return farmer_client(farm_admin_api.post(link_url(sources[0]), {}, format="json"))


def submit(client, payload, **extra):
    return client.post(f"{FARMER}/supply-requests", payload, format="json", **extra)


def decide(client, entry_id, action="approve", **payload):
    payload.setdefault("idempotency_key", str(uuid4()))
    return client.post(f"{ADMIN}/{entry_id}/{action}", payload, format="json")


def assert_error(response, code, http_status=409, **context):
    assert response.status_code == http_status, response.content
    expected = {"code": code}
    if context:
        expected["context"] = context
    assert response.json() == {"errors": [expected]}
    assert response["Cache-Control"] == "private, no-store"


def test_link_credentials_are_recoverable_and_audit_is_redacted(farm_admin_api, sources, caplog):
    source = sources[0]
    url = link_url(source)
    assert farm_admin_api.get(url).status_code == 404
    assert farm_admin_api.post(f"{url}/rotate", {}, format="json").status_code == 404
    created = farm_admin_api.post(url, {}, format="json")
    link = FarmFarmerLink.objects.get(farm_id=source.farm_id)
    token = created.json()["data"]["url"].split("#token=")[1]
    assert len(token) == 43 and token.encode() not in bytes(link.encrypted_token)
    assert link.token_digest == sha256(token.encode()).hexdigest()
    assert farm_admin_api.post(url, {}, format="json").status_code == 200
    assert farm_admin_api.get(url).json() == created.json()
    assert created["Referrer-Policy"] == "no-referrer"
    assert farm_admin_api.post(f"{url}/rotate", {}, format="json").status_code == 200
    assert token not in str(list(ContentAuditLog.objects.values()))
    assert token not in caplog.text
    assert FarmFarmerLink.objects.count() == 1


@pytest.mark.parametrize("token", [None, "wrong", "Z" * 43])
def test_every_farmer_endpoint_requires_valid_token(token):
    client = APIClient(HTTP_X_FARM_TOKEN=token) if token else APIClient()
    for path in ("farm", "products", "supply-requests", f"supply-requests/{uuid4()}"):
        assert_error(client.get(f"{FARMER}/{path}"), "invalid_farm_token", http_status=403)
    assert_error(submit(client, {}), "invalid_farm_token", http_status=403)


def test_farmer_scope_and_inactive_farm_rules(farm_admin_api, farmer, sources):
    farm = sources[0].farm
    outside = FarmProduct.objects.create(farm=make_farm("Other"), product=sources[0].product)
    other_farmer = farmer_client(farm_admin_api.post(link_url(outside), {}, format="json"))
    payload = supply_payload(sources)
    response = submit(farmer, payload)
    assert response.status_code == 201, response.content
    entry = response.json()["data"]
    assert other_farmer.get(f"{FARMER}/supply-requests").json()["data"] == []
    assert other_farmer.get(f"{FARMER}/supply-requests/{entry['id']}").status_code == 404
    assert_error(
        submit(farmer, supply_payload([outside])),
        "farm_product_identity_changed",
        item_index=0,
        product_id=str(outside.product_id),
    )

    farm.is_active = False
    farm.save(update_fields=["is_active"])
    assert farmer.get(f"{FARMER}/farm").json()["data"] == {
        "name_en": farm.name_en,
        "name_vi": farm.name_vi,
        "is_active": False,
    }
    assert farmer.get(f"{FARMER}/supply-requests/{entry['id']}").json()["data"] == entry
    assert submit(farmer, payload).status_code == 200
    assert_error(submit(farmer, supply_payload(sources)), "farm_inactive")
    assert_error(decide(farm_admin_api, entry["id"]), "farm_inactive")
    assert decide(farm_admin_api, entry["id"], "reject", reason="Farm paused").status_code == 200


def test_farmer_products_and_history_expose_only_allowed_fields(farm_admin_api, farmer, sources):
    farm = sources[0].farm
    FarmProduct.objects.filter(pk=sources[1].pk).update(is_active=False, removed_at=timezone.now())
    FarmProduct.objects.create(farm=farm, product=make_product("DRAFT", published=False))
    FarmProduct.objects.create(farm=make_farm("Other"), product=make_product("OTHER-FARM"))
    products = farmer.get(f"{FARMER}/products", {"search": "Cà chua"}).json()
    assert products["meta"] == {"total": 1, "page": 1, "page_size": 20}
    (item,) = products["data"]
    assert set(item) == {
        "product_id",
        "product_identity",
        "source_id",
        "supply_generation",
        "sku",
        "name_en",
        "name_vi",
        "unit_label_en",
        "unit_label_vi",
        "unit_size",
    }
    assert item["source_id"] == str(sources[0].pk)
    assert not sources[0].stock_batches.exists()

    created = submit(farmer, supply_payload(sources[:1])).json()["data"]
    assert set(created) == {
        "id",
        "code",
        "status",
        "start_date",
        "end_date",
        "submitted_at",
        "processed_at",
        "rejection_reason",
        "items",
    }
    assert set(created["items"][0]) == set(item) - {
        "source_id",
        "supply_generation",
        "product_identity",
    } | {"quantity_units"}
    assert decide(farm_admin_api, created["id"]).status_code == 200
    # A direct Admin batch for this source never appears as Farmer history.
    source_url = f"/api/v1/admin/farms/{farm.pk}/stock-batches/bulk"
    sources[0].refresh_from_db()
    response = farm_admin_api.post(source_url, bulk_payload(sources[:1]), format="json")
    assert response.status_code == 201
    assert farmer.get(f"{FARMER}/supply-requests").json()["meta"]["total"] == 1


@pytest.mark.parametrize("quantity", [0, -1, True, False, None, "1", "x", 1.0, 1.2, 2_147_483_648])
def test_quantity_rejects_invalid_type_or_range(farmer, sources, quantity):
    payload = supply_payload(sources)
    payload["items"][1]["quantity_units"] = quantity
    response = submit(farmer, payload)
    assert response.status_code == 400
    assert set(response.json()["items"]) == {"1"}
    assert "quantity_units" in response.json()["items"]["1"]
    assert not FarmSupplyRequest.objects.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("product_id", "bad"),
        ("product_id", 7),
        ("expected_link_id", True),
        ("expected_generation", "1"),
        ("expected_generation", 0),
        ("farm_id", "unknown"),
    ],
)
def test_invalid_item_field(farmer, sources, field, value):
    payload = supply_payload(sources)
    payload["items"][0][field] = value
    response = submit(farmer, payload)
    assert response.status_code == 400
    assert field in response.json()["items"]["0"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("idempotency_key", 7),
        ("idempotency_key", None),
        ("start_date", "2026-1-1"),
        ("start_date", "2026-02-30"),
        ("end_date", "2026-01-01T00:00:00Z"),
        ("items", []),
        ("items", {}),
        ("farm_id", "unexpected"),
    ],
)
def test_invalid_top_level_field(farmer, sources, field, value):
    response = submit(farmer, supply_payload(sources, **{field: value}))
    assert response.status_code == 400
    assert field in response.json()


def test_date_and_list_validation_including_limits(farmer, sources):
    today = timezone.localdate()
    for start, end in (
        (today, today - timedelta(days=1)),
        (today - timedelta(days=2), today - timedelta(days=1)),
    ):
        response = submit(
            farmer, supply_payload(sources, start_date=start.isoformat(), end_date=end.isoformat())
        )
        assert response.status_code == 400 and "end_date" in response.json()
    response = submit(farmer, supply_payload([sources[0]] * 2))
    assert response.status_code == 400 and "items" in response.json()
    many = sources + [
        FarmProduct.objects.create(farm=sources[0].farm, product=make_product(f"MANY-{i}"))
        for i in range(49)
    ]
    assert submit(farmer, supply_payload(many)).status_code == 400
    payload = supply_payload(many[:50])
    payload["items"][-1]["quantity_units"] = 2_147_483_647
    response = submit(farmer, payload)
    assert response.status_code == 201, response.content
    assert len(response.json()["data"]["items"]) == 50
    assert not FarmProductStockBatch.objects.exists()


def test_submit_retry_normalizes_item_order_but_rejects_changed_intent(farmer, sources):
    payload = supply_payload(sources)
    assert submit(farmer, payload).status_code == 201
    first, second = payload["items"]
    response = submit(farmer, {**payload, "items": [second, first]})
    assert response.status_code == 200
    assert [item["product_id"] for item in response.json()["data"]["items"]] == [
        first["product_id"],
        second["product_id"],
    ]
    first["quantity_units"] += 1
    assert_error(submit(farmer, payload), "idempotency_conflict")
    assert FarmSupplyRequest.objects.count() == 1


@pytest.mark.parametrize("reason", ["", "   ", None, 123, "X" * 501])
def test_reject_reason_validation(farm_admin_api, farmer, sources, reason):
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    response = decide(farm_admin_api, entry["id"], "reject", reason=reason)
    assert response.status_code == 400 and "reason" in response.json()
    assert FarmSupplyRequest.objects.get().status == "pending"


def test_reject_is_terminal_and_recovers_with_original_key(
    farm_admin_api, farmer, sources, active_user
):
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    command = {"idempotency_key": str(uuid4()), "reason": "  Chưa đủ chứng từ  "}
    response = decide(farm_admin_api, entry["id"], "reject", **command)
    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["rejection_reason"] == "Chưa đủ chứng từ"
    assert data["processed_at"]
    assert data["processed_by_id"] == str(active_user.pk)
    assert data["status"] == "rejected"
    assert (
        farmer.get(f"{FARMER}/supply-requests/{entry['id']}").json()["data"]["rejection_reason"]
        == data["rejection_reason"]
    )
    assert decide(farm_admin_api, entry["id"], "reject", **command).json()["meta"][
        "idempotent_replay"
    ]
    assert_error(
        decide(farm_admin_api, entry["id"], idempotency_key=command["idempotency_key"]),
        "idempotency_conflict",
    )
    assert_error(
        decide(farm_admin_api, entry["id"], "reject", **{**command, "reason": "Changed"}),
        "idempotency_conflict",
    )
    assert_error(
        decide(farm_admin_api, entry["id"]), "farm_supply_request_processed", status="rejected"
    )
    assert not FarmProductStockBatch.objects.exists()
    assert ContentAuditLog.objects.filter(action="farm.supply_request.rejected").count() == 1


def test_admin_permissions_require_group_and_scope(sources, active_user, access_token):
    link = link_url(sources[0])
    anonymous = APIClient()
    assert anonymous.get(ADMIN).status_code == 401
    assert anonymous.post(link, {}, format="json").status_code == 401
    client = APIClient(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert client.get(ADMIN).status_code == 403
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    assert client.get(ADMIN).status_code == 403
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    assert client.get(ADMIN).status_code == 200
    access_token.scope = "profile email"
    access_token.save(update_fields=["scope"])
    assert client.get(ADMIN).status_code == 403
    assert client.post(f"{link}/rotate", {}, format="json").status_code == 403


def test_approval_rechecks_permission_under_lock(
    farm_admin_api, farmer, sources, active_user, monkeypatch
):
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    lock_sources = supply_requests._lock_farm_stock_sources

    def revoke_group(farm_id, product_ids):
        rows = lock_sources(farm_id, product_ids)
        active_user.groups.clear()
        return rows

    monkeypatch.setattr(supply_requests, "_lock_farm_stock_sources", revoke_group)
    response = decide(farm_admin_api, entry["id"])
    assert response.status_code == 403
    assert FarmSupplyRequest.objects.get().status == "pending"
    assert not FarmProductStockBatch.objects.exists()


def test_filter_search_and_pagination(farm_admin_api, farmer, sources):
    created = []
    for index in range(21):
        response = submit(farmer, supply_payload(sources), REMOTE_ADDR=f"198.51.100.{index}")
        assert response.status_code == 201, response.content
        created.append(response.json()["data"])
    for index, item in enumerate(created, start=1):
        FarmSupplyRequest.objects.filter(pk=item["id"]).update(
            submitted_at=timezone.now() + timedelta(seconds=index)
        )
    assert decide(farm_admin_api, created[-1]["id"], "reject", reason="Test").status_code == 200
    for client, path in ((farm_admin_api, ADMIN), (farmer, f"{FARMER}/supply-requests")):
        page = client.get(path).json()
        assert page["meta"] == {"page": 1, "page_size": 20, "total": 21}
        assert [row["id"] for row in page["data"]] == [row["id"] for row in reversed(created[1:])]
        assert client.get(path, {"page": 2}).json()["data"][0]["id"] == created[0]["id"]
        assert client.get(path, {"page": 3}).json()["data"] == []

    assert farm_admin_api.get(ADMIN, {"search": created[0]["code"]}).json()["meta"]["total"] == 1
    query = {"search": "Green", "status": "pending", "farm_id": str(sources[0].farm_id)}
    assert farm_admin_api.get(ADMIN, query).json()["meta"]["total"] == 20
    assert (
        farm_admin_api.get(ADMIN, {**query, "farm_id": str(uuid4())}).json()["meta"]["total"] == 0
    )
    assert farm_admin_api.get(ADMIN, {**query, "status": "rejected"}).json()["meta"]["total"] == 1
    for field, value in (
        ("status", "all"),
        ("farm_id", "bad"),
        ("page", "1.0"),
        ("page", 0),
        ("page", 10001),
        ("search", "X" * 201),
        ("page_size", 500),
    ):
        response = farm_admin_api.get(ADMIN, {field: value})
        assert response.status_code == 400
        assert field in response.json()
    assert farmer.get(f"{FARMER}/products?page=1&page=2").status_code == 400


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("removed", "farm_product_removed"),
        ("readd", "farm_product_generation_changed"),
        ("discontinued", "product_not_selling"),
        ("earlier_version", "farm_product_version_conflict"),
    ],
)
def test_one_changed_source_blocks_entire_approval(
    farm_admin_api, farmer, sources, change, code, django_capture_on_commit_callbacks
):
    source = sources[1]
    source.version = 2
    source.save(update_fields=["version"])
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    source_url = f"/api/v1/admin/farms/{source.farm_id}/products/{source.product_id}"
    if change in {"removed", "readd"}:
        identity = {"expected_link_id": str(source.pk), "expected_version": 2}
        response = farm_admin_api.post(f"{source_url}/remove", identity, format="json")
        assert response.status_code == 200, response.content
        if change == "readd":
            payload = supply_payload(sources)
            response = farm_admin_api.post(
                f"{source_url}/re-add",
                {
                    **identity,
                    "expected_version": 3,
                    "start_date": payload["start_date"],
                    "end_date": payload["end_date"],
                    "quantity_units": 3,
                },
                format="json",
            )
            assert response.status_code == 200, response.content
    elif change == "discontinued":
        source.product.status = "discontinued"
        source.product.is_active = source.product.is_published = False
        source.product.save(update_fields=["status", "is_active", "is_published"])
    else:
        FarmProduct.objects.filter(pk=source.pk).update(version=1)
    before = FarmProductStockBatch.objects.count()
    with django_capture_on_commit_callbacks() as callbacks:
        response = decide(farm_admin_api, entry["id"])
    assert_error(response, code, item_index=1, product_id=str(source.product_id))
    assert not callbacks
    assert FarmSupplyRequest.objects.get().status == "pending"
    assert FarmProductStockBatch.objects.count() == before
    assert not sources[0].stock_batches.exists()


def test_approved_batches_accept_regular_version_advance_and_preserve_snapshot(
    farm_admin_api, farmer, sources
):
    source = sources[0]
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    url = f"/api/v1/admin/farms/{source.farm_id}/stock-batches/bulk"
    assert farm_admin_api.post(url, bulk_payload(sources), format="json").status_code == 201
    source.product.translations.update(name="Tên mới")
    response = decide(farm_admin_api, entry["id"])
    assert response.status_code == 200, response.content
    data = response.json()["data"]
    assert data["items"][0]["name_vi"] == entry["items"][0]["name_vi"]
    assert data["items"][0]["source_version"] == 1
    for product, item in zip(sources, data["items"], strict=True):
        product.refresh_from_db()
        assert product.version == 3
        batch = product.stock_batches.get(pk=item["stock_batch_id"])
        assert batch.quantity_units == item["quantity_units"]
    assert FarmProductStockBatch.objects.count() == 4
    assert_error(
        decide(farm_admin_api, entry["id"]), "farm_supply_request_processed", status="approved"
    )


def test_expired_request_is_revalidated_at_approval(farm_admin_api, farmer, sources, monkeypatch):
    payload = supply_payload(sources)
    entry = submit(farmer, payload).json()["data"]
    later = timezone.localdate() + timedelta(days=8)
    monkeypatch.setattr(supply_requests.timezone, "localdate", lambda: later)
    response = decide(farm_admin_api, entry["id"])
    assert response.status_code == 400 and "end_date" in response.json()
    assert not FarmProductStockBatch.objects.exists()


def test_mid_write_error_rolls_back_batches_audit_and_status(
    farm_admin_api, farmer, sources, monkeypatch, django_capture_on_commit_callbacks
):
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    write = supply_requests._write_stock_batch_for_source

    def fail_second(**kwargs):
        if kwargs["source"].pk == sources[1].pk:
            raise RuntimeError("simulated batch write failure")
        return write(**kwargs)

    monkeypatch.setattr(supply_requests, "_write_stock_batch_for_source", fail_second)
    with (
        django_capture_on_commit_callbacks() as callbacks,
        pytest.raises(RuntimeError, match="simulated batch write failure"),
    ):
        decide(farm_admin_api, entry["id"])
    assert not callbacks
    assert not FarmProductStockBatch.objects.exists()
    assert not ContentAuditLog.objects.filter(
        action__in=["farm.stock_batch.create", "farm.supply_request.approved"]
    ).exists()
    assert FarmSupplyRequest.objects.get().status == "pending"
    assert set(FarmProduct.objects.values_list("version", flat=True)) == {1}


def test_remove_all_sources_keeps_history_and_blocks_farm_delete(farm_admin_api, farmer, sources):
    entry = submit(farmer, supply_payload(sources)).json()["data"]
    assert decide(farm_admin_api, entry["id"], "reject", reason="Test").status_code == 200
    url = f"/api/v1/admin/farms/{sources[0].farm_id}"
    for source in sources:
        response = farm_admin_api.post(
            f"{url}/products/{source.product_id}/remove",
            {"expected_link_id": str(source.pk), "expected_version": 1},
            format="json",
        )
        assert response.status_code == 200, response.content
    expected = {"code": "farm_supply_request", "id": entry["id"], "label": entry["code"]}
    assert farm_admin_api.get(url).json()["data"]["capabilities"]["delete"] == {
        "allowed": False,
        "reasons": [expected],
    }
    assert_error(farm_admin_api.delete(url), "farm_in_use", reasons=[expected])
    response = farmer.get(f"{FARMER}/supply-requests/{entry['id']}")
    assert response.json()["data"]["items"] == entry["items"]


def test_ip_throttle_has_no_secret_and_limits_invalid_attempts(monkeypatch):
    monkeypatch.setattr(FarmerSubmitThrottle, "rate", "2/minute")
    token = "A" * 43
    client = APIClient(HTTP_X_FARM_TOKEN=token)
    for _ in range(2):
        assert_error(submit(client, {}), "invalid_farm_token", http_status=403)
    response = submit(client, {})
    assert response.status_code == 429
    assert response["Cache-Control"] == "private, no-store"
    assert token not in response.content.decode()
    assert response["Retry-After"]
    assert token not in str(list(cache._cache))


def test_link_recovery_with_rotated_server_key(farm_admin_api, sources, settings):
    url = link_url(sources[0])
    response = farm_admin_api.post(url, {}, format="json")
    assert response.status_code == 201
    settings.SECRET_KEY_FALLBACKS = [settings.SECRET_KEY]
    settings.SECRET_KEY = "new-test-signing-key"
    assert farm_admin_api.get(url).json() == response.json()
    settings.SECRET_KEY_FALLBACKS = []
    assert_error(farm_admin_api.get(url), "farm_farmer_link_unreadable")
    assert farmer_client(response).get(f"{FARMER}/farm").status_code == 200
    refreshed = farm_admin_api.post(f"{url}/rotate", {}, format="json")
    assert refreshed.status_code == 200
    assert farm_admin_api.get(url).json() == refreshed.json()


def test_deleting_unused_farm_revokes_its_link(farm_admin_api):
    farm = make_farm("Unused farm")
    url = f"/api/v1/admin/farms/{farm.pk}"
    link = farm_admin_api.post(f"{url}/farmer-link", {}, format="json")
    assert link.status_code == 201
    assert farm_admin_api.delete(url).status_code == 204
    assert not FarmFarmerLink.objects.filter(farm_id=farm.pk).exists()
    assert_error(farmer_client(link).get(f"{FARMER}/farm"), "invalid_farm_token", http_status=403)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("sku", "UPDATED-SKU"),
        ("unit_size", "1.000"),
        ("unit_label_vi", "gói"),
        ("unit_label_en", "pack"),
    ],
)
def test_pending_request_locks_identity_and_reject_releases_it(
    farm_admin_api, farmer, sources, field, replacement
):
    product = sources[0].product
    make_product_editable(product)
    url = f"/api/v1/admin/store-products/{product.pk}"
    before = farm_admin_api.get(url).json()["data"]["capabilities"]
    assert before["edit_identity"] == {"allowed": True, "reasons": []}
    pending = submit(farmer, supply_payload(sources)).json()["data"]
    after = farm_admin_api.get(url).json()["data"]["capabilities"]
    assert after == {**before, "edit_identity": {"allowed": False, "reasons": []}}
    assert_error(
        farm_admin_api.patch(url, {field: replacement}, format="json"), "product_unit_in_use"
    )
    assert (
        farm_admin_api.patch(url, {field: getattr(product, field)}, format="json").status_code
        == 200
    )
    assert decide(farm_admin_api, pending["id"], "reject", reason="Test").status_code == 200
    assert farm_admin_api.get(url).json()["data"]["capabilities"] == before
    assert farm_admin_api.patch(url, {field: replacement}, format="json").status_code == 200


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("sku", "UPDATED-SKU"),
        ("unit_size", "1.000"),
        ("unit_label_vi", "gói"),
        ("unit_label_en", "pack"),
    ],
)
def test_submit_rejects_identity_changed_after_product_read(
    farm_admin_api, farmer, sources, field, replacement
):
    payload = read_supply_payload(farmer, sources)
    product = sources[1].product
    make_product_editable(product)
    url = f"/api/v1/admin/store-products/{product.pk}"
    assert farm_admin_api.patch(url, {field: replacement}, format="json").status_code == 200
    assert_error(
        submit(farmer, payload),
        "product_identity_changed",
        item_index=1,
        product_id=str(product.pk),
    )
    assert not FarmSupplyRequest.objects.exists()
    assert not FarmSupplyRequestItem.objects.exists()
    assert not FarmProductStockBatch.objects.exists()
    assert not ContentAuditLog.objects.filter(action="farm.supply_request.submit").exists()


@pytest.mark.parametrize("field", ["sku", "unit_size", "unit_label_vi", "unit_label_en"])
def test_approval_rejects_inconsistent_legacy_snapshot(farm_admin_api, farmer, sources, field):
    pending = submit(farmer, supply_payload(sources)).json()["data"]
    entry = FarmSupplyRequest.objects.get(pk=pending["id"])
    item = entry.items.get(position=1)
    item.product_snapshot[field] = "42"
    item.save(update_fields=["product_snapshot"])
    assert_error(
        decide(farm_admin_api, pending["id"]),
        "product_identity_changed",
        item_index=1,
        product_id=str(sources[1].product_id),
    )
    entry.refresh_from_db()
    item.refresh_from_db()
    assert entry.status == "pending"
    assert item.product_snapshot[field] == "42"
    assert item.quantity_units == pending["items"][1]["quantity_units"]
    assert item.source_generation == sources[1].supply_generation
    assert not FarmProductStockBatch.objects.exists()


def product_image(name):
    return MediaAsset.objects.create(
        file=f"products/{name}", width=100, height=100, original_name=name
    )


def make_product_editable(product):
    product.primary_image = product_image(f"{product.slug}.jpg")
    product.save(update_fields=["primary_image"])
    product.translations.get_or_create(locale="en", defaults={"name": "Tomatoes"})


def read_supply_payload(farmer, sources):
    response = farmer.get(f"{FARMER}/products")
    assert response.status_code == 200
    products = {item["product_id"]: item for item in response.json()["data"]}
    payload = supply_payload(sources)
    for item in payload["items"]:
        item["expected_product_identity"] = products[item["product_id"]]["product_identity"]
    return payload


def test_pending_requests_across_farms_keep_shared_product_locked(farm_admin_api, farmer, sources):
    product = sources[0].product
    make_product_editable(product)
    url = f"/api/v1/admin/store-products/{product.pk}"
    second_source = FarmProduct.objects.create(farm=make_farm("Other farm"), product=product)
    second_farmer = farmer_client(farm_admin_api.post(link_url(second_source), {}, format="json"))
    first = submit(farmer, supply_payload(sources)).json()["data"]
    second = submit(second_farmer, supply_payload([second_source])).json()["data"]
    assert decide(farm_admin_api, first["id"], "reject", reason="First").status_code == 200
    assert (
        farm_admin_api.get(url).json()["data"]["capabilities"]["edit_identity"]["allowed"] is False
    )
    assert_error(
        farm_admin_api.patch(url, {"unit_label_en": "pack"}, format="json"), "product_unit_in_use"
    )
    assert decide(farm_admin_api, second["id"], "reject", reason="Second").status_code == 200
    assert (
        farm_admin_api.get(url).json()["data"]["capabilities"]["edit_identity"]["allowed"] is True
    )
    assert farm_admin_api.patch(url, {"unit_label_en": "pack"}, format="json").status_code == 200


@pytest.mark.parametrize("reason_code", ["stock_batch", "legacy_supply", "store_order"])
def test_reject_preserves_existing_identity_references(
    farm_admin_api, farmer, sources, reason_code
):
    source = sources[0]
    product = source.product
    pending = submit(farmer, supply_payload(sources)).json()["data"]
    today = timezone.localdate()
    if reason_code == "stock_batch":
        FarmProductStockBatch.objects.create(
            farm_product=source, start_date=today, end_date=today, quantity_units=1
        )
    elif reason_code == "legacy_supply":
        LegacyFarmWeeklySupply.objects.create(
            farm_product=source, week_start=today, quantity_units=0
        )
    else:
        order = StoreOrder.objects.create(
            reference_code="NFV-TEST-0031",
            create_idempotency_key=uuid4(),
            locale="vi",
            subtotal_vnd=10000,
            shipping_fee_vnd=0,
            total_vnd=10000,
        )
        StoreOrderLine.objects.create(
            order=order,
            product=product,
            position=1,
            sku_snapshot=product.sku,
            name_snapshot="Tomatoes",
            unit_size_snapshot=product.unit_size,
            unit_label_snapshot="kg",
            unit_price_vnd_snapshot=10000,
            quantity=1,
            cash_quantity=1,
            cash_subtotal_vnd=10000,
        )
    assert decide(farm_admin_api, pending["id"], "reject", reason="Test").status_code == 200
    url = f"/api/v1/admin/store-products/{product.pk}"
    capability = farm_admin_api.get(url).json()["data"]["capabilities"]["edit_identity"]
    assert capability["allowed"] is False
    assert [reason["code"] for reason in capability["reasons"]] == [reason_code]
    assert_error(
        farm_admin_api.patch(url, {"sku": "NEW-SKU"}, format="json"), "product_unit_in_use"
    )


def test_reject_preserves_existing_csa_hold(farm_admin_api, context, supported_area):
    _, product, *_ = context
    save_selection(context)
    source = product.farm_offers.get()
    farmer = farmer_client(farm_admin_api.post(link_url(source), {}, format="json"))
    pending = submit(farmer, supply_payload([source])).json()["data"]
    assert decide(farm_admin_api, pending["id"], "reject", reason="Test").status_code == 200
    url = f"/api/v1/admin/store-products/{product.pk}"
    capability = farm_admin_api.get(url).json()["data"]["capabilities"]["edit_identity"]
    assert capability["allowed"] is False
    assert "csa_hold" in {reason["code"] for reason in capability["reasons"]}
    assert_error(
        farm_admin_api.patch(url, {"sku": "NEW-SKU"}, format="json"), "product_csa_hold_in_use"
    )


def test_edit_nonidentity_fields_before_submit_and_while_pending(farm_admin_api, farmer, sources):
    product = sources[0].product
    make_product_editable(product)
    url = f"/api/v1/admin/store-products/{product.pk}"
    payload = read_supply_payload(farmer, sources)
    for index in (1, 2):
        image = product_image(f"updated-{index}.jpg")
        response = farm_admin_api.patch(
            url,
            {
                "translations": [
                    {"locale": "vi", "name": f"Tên mới {index}", "description": f"Mô tả {index}"},
                    {
                        "locale": "en",
                        "name": f"New name {index}",
                        "description": f"Details {index}",
                    },
                ],
                "primary_image_id": str(image.pk),
            },
            format="json",
        )
        assert response.status_code == 200, response.content
        if index == 1:
            response = submit(farmer, payload)
            assert response.status_code == 201, response.content
            entry = response.json()["data"]
    assert decide(farm_admin_api, entry["id"]).status_code == 200
    capability = farm_admin_api.get(url).json()["data"]["capabilities"]["edit_identity"]
    assert capability["allowed"] is False
    assert [reason["code"] for reason in capability["reasons"]] == ["stock_batch"]
    assert_error(
        farm_admin_api.patch(url, {"sku": "NEW-SKU"}, format="json"), "product_unit_in_use"
    )


def test_equivalent_decimal_and_source_version_increase_preserve_intent(
    farm_admin_api, farmer, sources
):
    source = sources[0]
    make_product_editable(source.product)
    payload = read_supply_payload(farmer, sources)
    url = f"/api/v1/admin/store-products/{source.product_id}"
    assert farm_admin_api.patch(url, {"unit_size": "0.5"}, format="json").status_code == 200
    bulk_url = f"/api/v1/admin/farms/{source.farm_id}/stock-batches/bulk"
    response = farm_admin_api.post(bulk_url, bulk_payload(sources), format="json")
    assert response.status_code == 201, response.content
    response = submit(farmer, payload)
    assert response.status_code == 201, response.content
    entry = FarmSupplyRequest.objects.get(pk=response.json()["data"]["id"])
    item = entry.items.get(position=0)
    item.product_snapshot["unit_size"] = "0.50000"
    item.save(update_fields=["product_snapshot"])
    assert decide(farm_admin_api, str(entry.pk)).status_code == 200


@pytest.mark.parametrize("value", [None, "missing", "A" * 64, 10**63, "0" * 64 + "\n"])
def test_new_submit_requires_a_valid_product_identity(farmer, sources, value):
    payload = supply_payload(sources)
    if value == "missing":
        del payload["items"][0]["expected_product_identity"]
    else:
        payload["items"][0]["expected_product_identity"] = value
    response = submit(farmer, payload)
    assert response.status_code == 400, response.content
    assert "expected_product_identity" in response.json()["items"]["0"]
    assert not FarmSupplyRequest.objects.exists()


@pytest.mark.parametrize(
    "snapshot", [None, [], {"unit_size": "not decimal"}, {"unit_size": "NaN"}, {}]
)
def test_approval_rejects_missing_or_malformed_identity_snapshot(
    farm_admin_api, farmer, sources, snapshot
):
    response = submit(farmer, supply_payload(sources))
    assert response.status_code == 201
    entry = FarmSupplyRequest.objects.get(pk=response.json()["data"]["id"])
    item = entry.items.get(position=1)
    if snapshot is not None:
        item.product_snapshot = snapshot
    else:
        del item.product_snapshot["unit_label_en"]
    item.save(update_fields=["product_snapshot"])
    assert_error(
        decide(farm_admin_api, entry.pk),
        "product_identity_changed",
        item_index=1,
        product_id=str(sources[1].product_id),
    )
    entry.refresh_from_db()
    assert entry.status == "pending"
    assert not FarmProductStockBatch.objects.exists()


def test_submission_replays_original_identity_even_after_actual_product_change(
    farm_admin_api, farmer, sources
):
    product = sources[0].product
    make_product_editable(product)
    payload = read_supply_payload(farmer, sources)
    response = submit(farmer, payload)
    entry_id = response.json()["data"]["id"]
    assert decide(farm_admin_api, entry_id, "reject", reason="Test").status_code == 200
    url = f"/api/v1/admin/store-products/{product.pk}"
    assert farm_admin_api.patch(url, {"unit_size": "1.000"}, format="json").status_code == 200
    replay = submit(farmer, payload)
    assert replay.status_code == 200
    assert replay.json()["data"]["id"] == entry_id
    fresh = read_supply_payload(farmer, sources)
    fresh["idempotency_key"] = payload["idempotency_key"]
    assert_error(submit(farmer, fresh), "idempotency_conflict")


def test_legacy_submission_replay_requires_exact_old_intent(farmer, sources):
    source = sources[0]
    payload = supply_payload([source])
    del payload["items"][0]["expected_product_identity"]
    start = date.fromisoformat(payload["start_date"])
    end = date.fromisoformat(payload["end_date"])
    fingerprint = creation_fingerprint(
        {"start_date": start, "end_date": end, "items": payload["items"]}
    )
    entry = FarmSupplyRequest.objects.create(
        farm=source.farm,
        code=f"FSR-{uuid4().hex.upper()}",
        start_date=start,
        end_date=end,
        submission_key=payload["idempotency_key"],
        submission_fingerprint=fingerprint,
    )
    FarmSupplyRequestItem.objects.create(
        request=entry,
        source=source,
        position=0,
        product_snapshot=supply_requests.supply_product_payload(source.product),
        source_generation=source.supply_generation,
        source_version=source.version,
        quantity_units=payload["items"][0]["quantity_units"],
    )
    response = submit(farmer, payload)
    assert response.status_code == 200
    assert response.json()["meta"]["idempotent_replay"] is True
    assert response.json()["data"]["id"] == str(entry.pk)
    payload["items"][0]["quantity_units"] += 1
    assert_error(submit(farmer, payload), "idempotency_conflict")
    payload["idempotency_key"] = str(uuid4())
    assert submit(farmer, payload).status_code == 400
    entry.refresh_from_db()
    assert entry.submission_fingerprint == fingerprint
    assert FarmSupplyRequest.objects.count() == 1
