from datetime import timedelta
from uuid import uuid4

import pytest
from django.utils import timezone

from content.models import AdminCreationReceipt, ContentAuditLog
from farms.models import Farm, FarmProduct, FarmProductStockBatch
from tests.test_csa_rounds import context as context
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_farms_api import farm_payload
from tests.test_product_lifecycle_locking import checkout_operation as checkout_operation
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def bulk_payload(sources, **overrides):
    today = timezone.localdate()
    payload = {
        "idempotency_key": str(uuid4()),
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=7)).isoformat(),
        "items": [
            {
                "product_id": str(s.product_id),
                "expected_link_id": str(s.pk),
                "expected_version": s.version,
                "quantity_units": 10 + i,
            }
            for i, s in enumerate(sources)
        ],
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def sources():
    farm = make_farm()
    return [
        FarmProduct.objects.create(farm=farm, product=make_product(f"BULK-{i}")) for i in range(2)
    ]


def bulk_url(farm_id):
    return f"/api/v1/admin/farms/{farm_id}/stock-batches/bulk"


def test_phone_create_patch_search(farm_admin_api):
    response = farm_admin_api.post(
        "/api/v1/admin/farms", farm_payload(contact_phone="0912345678"), format="json"
    )
    assert response.status_code == 201, response.data
    data = response.json()["data"]
    farm = Farm.objects.get(pk=data["id"])
    assert farm.contact_phone == "+84912345678"
    assert data["contact_phone"] == "0912345678"
    url = f"/api/v1/admin/farms/{farm.pk}"
    response = farm_admin_api.patch(url, {"version": 1, "name_en": "Changed"}, format="json")
    assert response.status_code == 200
    assert response.json()["data"]["contact_phone"] == "0912345678"
    assert farm_admin_api.get("/api/v1/admin/farms?search=0912345678").json()["meta"]["total"] == 1
    response = farm_admin_api.patch(url, {"version": 2, "contact_phone": ""}, format="json")
    assert response.status_code == 200
    farm.refresh_from_db()
    assert farm.contact_phone == ""


def test_eligible_filter_keeps_zero_stock(farm_admin_api, sources):
    sources[1].product.status = "draft"
    sources[1].product.is_published = False
    sources[1].product.save()
    url = f"/api/v1/admin/farms/{sources[0].farm_id}/products"
    assert farm_admin_api.get(url).json()["meta"]["total"] == 2
    response = farm_admin_api.get(url + "?supply_status=supplying&product_status=active")
    assert response.status_code == 200, response.data
    assert response.json()["meta"]["total"] == 1
    assert response.json()["data"][0]["available_quantity"] == 0


def test_bulk_commit_replay_recovery(farm_admin_api, sources, django_capture_on_commit_callbacks):
    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    with django_capture_on_commit_callbacks() as callbacks:
        response = farm_admin_api.post(url, payload, format="json")
    assert response.status_code == 201, response.content
    assert len(callbacks) == 1
    assert response.json()["data"] == {"farm_id": str(sources[0].farm_id), "created_count": 2}
    assert response.json()["meta"]["idempotent_replay"] is False
    assert FarmProductStockBatch.objects.count() == 2
    assert list(FarmProduct.objects.order_by("product__sku").values_list("version", flat=True)) == [
        2,
        2,
    ]
    assert ContentAuditLog.objects.filter(action="farm.stock_batch.create").count() == 2
    assert AdminCreationReceipt.objects.count() == 1
    with django_capture_on_commit_callbacks() as callbacks:
        replay = farm_admin_api.post(url, payload, format="json")
    assert replay.status_code == 200
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert not callbacks
    recovery = farm_admin_api.get(url, {"creation_key": payload["idempotency_key"]})
    assert recovery.status_code == 200
    assert recovery.json() == replay.json()
    assert FarmProductStockBatch.objects.count() == 2


def test_bulk_bad_item_rolls_back(farm_admin_api, sources, django_capture_on_commit_callbacks):
    payload = bulk_payload(sources)
    payload["items"][1]["expected_version"] = 99
    with django_capture_on_commit_callbacks() as callbacks:
        response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"][0]["context"]["product_id"] == str(sources[1].product_id)
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.filter(action="farm.stock_batch.create").exists()
    assert set(FarmProduct.objects.values_list("version", flat=True)) == {1}
    assert not callbacks


@pytest.mark.parametrize("value", ["0912345678", "84912345678", "+84912345678", "02412345678"])
def test_contact_phone_normalizes_and_is_not_unique(farm_admin_api, value):
    for _ in range(2):
        response = farm_admin_api.post(
            "/api/v1/admin/farms", farm_payload(contact_phone=value), format="json"
        )
        assert response.status_code == 201
    assert Farm.objects.count() == 2
    assert all(farm.contact_phone.startswith("+84") for farm in Farm.objects.all())


@pytest.mark.parametrize("value", [None, "hello", "+12025550123", "0912 345 678", "091", True])
def test_contact_phone_invalid(farm_admin_api, value):
    response = farm_admin_api.post(
        "/api/v1/admin/farms", farm_payload(contact_phone=value), format="json"
    )
    assert response.status_code == 400
    assert "contact_phone" in response.json()
    assert not Farm.objects.exists()


def test_phone_legacy_create_update_and_version(farm_admin_api):
    response = farm_admin_api.post("/api/v1/admin/farms", farm_payload(), format="json")
    assert response.status_code == 201
    assert response.json()["data"]["contact_phone"] == ""
    url = f"/api/v1/admin/farms/{response.json()['data']['id']}"
    assert (
        farm_admin_api.patch(
            url, {"version": 1, "contact_phone": "+84912345678"}, format="json"
        ).status_code
        == 200
    )
    stale = farm_admin_api.patch(url, {"version": 1, "contact_phone": ""}, format="json")
    assert stale.status_code == 409
    assert Farm.objects.get().contact_phone == "+84912345678"
    assert ContentAuditLog.objects.filter(action="farm.update").count() == 1
    for query in ["0912345678", "+84912345678", "84912345678", "0912", "345678"]:
        assert (
            farm_admin_api.get("/api/v1/admin/farms", {"search": query}).json()["meta"]["total"]
            == 1
        )


def test_eligible_pagination_search_and_removed(farm_admin_api, sources):
    farm = sources[0].farm
    for i in range(2, 23):
        FarmProduct.objects.create(farm=farm, product=make_product(f"BULK-{i:02d}"))
    sources[1].removed_at = timezone.now()
    sources[1].is_active = False
    sources[1].save()
    url = f"/api/v1/admin/farms/{farm.pk}/products"
    for page, count in [(1, 20), (2, 2)]:
        response = farm_admin_api.get(
            url, {"supply_status": "supplying", "product_status": "active", "page": page}
        )
        assert response.status_code == 200
        assert response.json()["meta"] == {"page": page, "page_size": 20, "total": 22}
        assert len(response.json()["data"]) == count
    response = farm_admin_api.get(
        url, {"supply_status": "supplying", "product_status": "active", "search": "BULK-02"}
    )
    assert response.json()["meta"]["total"] == 1
    row = response.json()["data"][0]
    assert (row["unit_size"], row["unit_label_vi"], row["version"], row["supply_generation"]) == (
        "0.500",
        "kg",
        1,
        1,
    )
    assert farm_admin_api.get(url, {"product_status": "invalid"}).status_code == 400
    assert (
        farm_admin_api.get(url, {"product_status": "active", "supply_status": "removed"}).json()[
            "meta"
        ]["total"]
        == 1
    )


@pytest.mark.parametrize(
    "quantity", [0, -1, 1.0, 1.5, "1.0", "1.5", 2147483648, True, None, {}, "bad"]
)
def test_bulk_rejects_quantity(farm_admin_api, sources, quantity):
    payload = bulk_payload(sources)
    payload["items"][0]["quantity_units"] = quantity
    response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 400, response.content
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()


@pytest.mark.parametrize(
    "mutation",
    ["empty", "duplicate", "too_many", "bad_key", "bad_product", "extra", "missing_revision"],
)
def test_bulk_rejects_schema(farm_admin_api, sources, mutation):
    payload = bulk_payload(sources)
    if mutation == "empty":
        payload["items"] = []
    elif mutation == "duplicate":
        payload["items"] *= 2
    elif mutation == "too_many":
        payload["items"] = [dict(payload["items"][0], product_id=str(uuid4())) for _ in range(51)]
    elif mutation == "bad_key":
        payload["idempotency_key"] = "bad"
    elif mutation == "bad_product":
        payload["items"][0]["product_id"] = "bad"
    elif mutation == "extra":
        payload["items"][0]["price"] = 10
    else:
        del payload["items"][0]["expected_version"]
    response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 400
    assert not AdminCreationReceipt.objects.exists()
    assert not FarmProductStockBatch.objects.exists()


@pytest.mark.parametrize(
    "kind, code",
    [
        ("inactive", "product_not_selling"),
        ("removed", "farm_product_removed"),
        ("identity", "farm_product_identity_changed"),
        ("unlinked", "farm_product_changed"),
        ("wrong_farm", "farm_product_identity_changed"),
        ("wrong_product", "farm_product_identity_changed"),
    ],
)
def test_bulk_eligibility_atomic(
    farm_admin_api, sources, kind, code, django_capture_on_commit_callbacks
):
    payload = bulk_payload(sources)
    item = payload["items"][1]
    if kind == "inactive":
        from commerce.models import Product

        Product.objects.filter(pk=sources[1].product_id).update(
            status="discontinued", is_active=False, is_published=False
        )
    elif kind == "removed":
        FarmProduct.objects.filter(pk=sources[1].pk).update(
            removed_at=timezone.now(), is_active=False
        )
    elif kind == "identity":
        item["expected_link_id"] = str(uuid4())
    elif kind == "unlinked":
        item["product_id"] = str(make_product("UNLINKED").pk)
    elif kind == "wrong_farm":
        other = FarmProduct.objects.create(farm=make_farm("Other"), product=sources[1].product)
        item["expected_link_id"] = str(other.pk)
    else:
        item["expected_link_id"] = str(sources[0].pk)
    with django_capture_on_commit_callbacks() as callbacks:
        response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 409, response.content
    assert response.json()["errors"] == [
        {"code": code, "context": {"item_index": 1, "product_id": item["product_id"]}}
    ]
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.filter(action="farm.stock_batch.create").exists()
    assert set(FarmProduct.objects.values_list("version", flat=True)) == {1}
    assert not callbacks


@pytest.mark.parametrize("kind", ["expired", "reversed"])
def test_bulk_dates_rollback(farm_admin_api, sources, kind):
    today = timezone.localdate()
    payload = bulk_payload(
        sources,
        start_date=(today - timedelta(days=2)).isoformat(),
        end_date=(today - timedelta(days=1)).isoformat(),
    )
    if kind == "reversed":
        payload["start_date"] = (today + timedelta(days=2)).isoformat()
        payload["end_date"] = today.isoformat()
    response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 400
    assert "end_date" in response.json()
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()


@pytest.mark.parametrize("future, active", [(False, True), (True, True), (False, False)])
def test_bulk_preserves_old_batches_generation_and_availability(
    farm_admin_api, sources, future, active
):
    from farms.services import farm_product_queryset

    today = timezone.localdate()
    old = FarmProductStockBatch.objects.create(
        farm_product=sources[0],
        start_date=today,
        end_date=today + timedelta(days=7),
        quantity_units=3,
    )
    Farm.objects.filter(pk=sources[0].farm_id).update(is_active=active)
    payload = bulk_payload(sources[:1])
    if future:
        payload["start_date"] = (today + timedelta(days=1)).isoformat()
    response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 201
    assert response.json()["data"]["created_count"] == 1
    old.refresh_from_db()
    assert old.quantity_units == 3
    new = FarmProductStockBatch.objects.exclude(pk=old.pk).get()
    assert (
        new.quantity_units,
        new.start_date.isoformat(),
        new.end_date.isoformat(),
        new.supply_generation,
    ) == (10, payload["start_date"], payload["end_date"], 1)
    source = farm_product_queryset().get(pk=sources[0].pk)
    assert source.available_quantity == (0 if not active else 3 if future else 13)
    assert (source.version, source.supply_generation) == (2, 1)


@pytest.mark.parametrize("kind", ["quantity", "farm", "version", "dates"])
def test_bulk_idempotency_conflict(farm_admin_api, sources, kind):
    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    assert farm_admin_api.post(url, payload, format="json").status_code == 201
    if kind == "quantity":
        payload["items"][0]["quantity_units"] += 1
    elif kind == "version":
        payload["items"][0]["expected_version"] += 1
    elif kind == "dates":
        payload["start_date"] = (timezone.localdate() - timedelta(days=1)).isoformat()
    else:
        url = bulk_url(make_farm("Other").pk)
    response = farm_admin_api.post(url, payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "idempotency_conflict"}]
    assert FarmProductStockBatch.objects.count() == 2
    assert ContentAuditLog.objects.filter(action="farm.stock_batch.create").count() == 2


def test_replay_survives_edits_cancel_expiry_and_input_reordering(
    farm_admin_api, sources, monkeypatch, django_capture_on_commit_callbacks
):
    from commerce.models import Product

    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    initial = farm_admin_api.post(url, payload, format="json").json()
    FarmProduct.objects.update(version=9)
    FarmProductStockBatch.objects.update(quantity_units=1, cancelled_at=timezone.now())
    Product.objects.update(status="draft", is_published=False)
    future = timezone.now() + timedelta(days=30)
    # Keep OAuth expiry valid; move the domain decision date only.
    monkeypatch.setattr("farms.services.timezone.localdate", lambda *a, **kw: future.date())
    payload["items"].reverse()
    with django_capture_on_commit_callbacks() as callbacks:
        replay = farm_admin_api.post(url, payload, format="json")
    assert replay.status_code == 200
    assert replay.json()["data"] == initial["data"]
    assert replay.json()["meta"]["action_receipt"] == initial["meta"]["action_receipt"]
    assert not callbacks
    assert FarmProductStockBatch.objects.count() == 2
    assert ContentAuditLog.objects.filter(action="farm.stock_batch.create").count() == 2
    assert set(FarmProduct.objects.values_list("version", flat=True)) == {9}


def test_receipt_permissions_and_wrong_farm(farm_admin_api, sources, active_user):
    from rest_framework.test import APIClient

    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    assert APIClient().post(url, payload, format="json").status_code == 401
    assert farm_admin_api.post(url, payload, format="json").status_code == 201
    key = {"creation_key": payload["idempotency_key"]}
    assert farm_admin_api.get(bulk_url(uuid4()), key).status_code == 404
    assert APIClient().get(url, key).status_code == 401
    active_user.groups.clear()
    assert farm_admin_api.get(url, key).status_code == 403
    assert farm_admin_api.post(url, payload, format="json").status_code == 403


def test_other_actor_cannot_recover_receipt(farm_admin_api, sources, active_user):
    from types import SimpleNamespace

    from identity.models import User

    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    assert farm_admin_api.post(url, payload, format="json").status_code == 201
    other = User.objects.create_user(email="other-bulk@example.com", name="Other", status="active")
    other.groups.set(active_user.groups.all())
    farm_admin_api.force_authenticate(other, SimpleNamespace(scope="content:manage"))
    assert farm_admin_api.get(url, {"creation_key": payload["idempotency_key"]}).status_code == 404
    assert farm_admin_api.post(url, payload, format="json").status_code == 409
    assert AdminCreationReceipt.objects.count() == 1


def test_bulk_response_failure_recovery(farm_admin_api, sources, monkeypatch):
    from farms.views import AdminFarmBulkStockBatchView

    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    original = AdminFarmBulkStockBatchView.receipt_response

    def broken(*args, **kwargs):
        raise RuntimeError("response lost")

    monkeypatch.setattr(AdminFarmBulkStockBatchView, "receipt_response", broken)
    with pytest.raises(RuntimeError, match="response lost"):
        farm_admin_api.post(url, payload, format="json")
    monkeypatch.setattr(AdminFarmBulkStockBatchView, "receipt_response", original)
    response = farm_admin_api.get(url, {"creation_key": payload["idempotency_key"]})
    assert response.status_code == 200
    assert response.json()["data"]["created_count"] == 2
    assert FarmProductStockBatch.objects.count() == 2


def test_bulk_ordered_locks_and_decision_clock(active_user, sources, monkeypatch):
    from django.db.models import QuerySet
    from rest_framework.exceptions import ValidationError

    from commerce.models import Product
    from farms import services
    from tests.test_farm_lifecycle_locking import record_resource_locks

    locks = record_resource_locks(monkeypatch)
    payload = bulk_payload(sources)
    from farms.serializers import BulkStockBatchWriteSerializer

    serializer = BulkStockBatchWriteSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    services.create_stock_batches_bulk(
        farm_id=sources[0].farm_id, actor=active_user, **serializer.validated_data
    )
    assert [(model, ids) for model, ids, _ in locks] == [
        (Product, sorted(s.product_id for s in sources)),
        (FarmProduct, sorted(s.pk for s in sources)),
        (Farm, [sources[0].farm_id]),
    ]
    assert all(not query.select_related for _, _, query in locks)
    # Simulate midnight passing while waiting on the last resource lock.
    today = timezone.localdate()
    clock = [today]
    fetch = QuerySet._fetch_all

    def wait_for_farm(rows):
        result = fetch(rows)
        if rows.model is Farm and rows.query.select_for_update:
            clock[0] = today + timedelta(days=1)
        return result

    monkeypatch.setattr(QuerySet, "_fetch_all", wait_for_farm)
    monkeypatch.setattr(services.timezone, "localdate", lambda *args: clock[0])
    command = dict(serializer.validated_data, idempotency_key=uuid4(), end_date=today)
    command["items"] = [dict(item, expected_version=2) for item in command["items"]]
    with pytest.raises(ValidationError):
        services.create_stock_batches_bulk(farm_id=sources[0].farm_id, actor=active_user, **command)
    assert FarmProductStockBatch.objects.count() == 2
    assert AdminCreationReceipt.objects.count() == 1


def test_bulk_query_growth_and_revalidation_tags(
    active_user, sources, django_capture_on_commit_callbacks, monkeypatch
):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from farms.serializers import BulkStockBatchWriteSerializer
    from farms.services import create_stock_batches_bulk

    tags = []
    monkeypatch.setattr("commerce.services.request_frontend_revalidation", tags.append)
    measurements = []
    for count in (1, 50):
        rows = [
            FarmProduct.objects.create(
                farm=make_farm(f"Measure {count}"), product=make_product(f"MEASURE-{count}-0")
            )
        ]
        rows.extend(
            FarmProduct.objects.create(
                farm=rows[0].farm, product=make_product(f"MEASURE-{count}-{i}")
            )
            for i in range(1, count)
        )
        serializer = BulkStockBatchWriteSerializer(data=bulk_payload(rows))
        serializer.is_valid(raise_exception=True)
        with (
            CaptureQueriesContext(connection) as queries,
            django_capture_on_commit_callbacks(execute=True) as callbacks,
        ):
            receipt, created = create_stock_batches_bulk(
                farm_id=rows[0].farm_id, actor=active_user, **serializer.validated_data
            )
        assert created and receipt.created_count == count
        assert len(callbacks) == 1
        selects = [q["sql"] for q in queries if q["sql"].startswith("SELECT")]
        writes = [q["sql"] for q in queries if q["sql"].startswith(("INSERT", "UPDATE"))]
        measurements.append((len(selects), len(writes)))
    assert measurements[0][0] == measurements[1][0]
    assert measurements[1][1] - measurements[0][1] == 3 * 49
    assert tags[1] == ["store-product-details", "store-products"]
    assert tags[0] == ["store-product:measure-1-0", "store-products"]
    print(f"bulk SELECT/write queries for 1 and 50 items: {measurements}")


def test_phone_not_in_public_projections(farm_admin_api, sources):
    from rest_framework.test import APIClient

    from commerce.models import CategoryTranslation

    for source in sources:
        CategoryTranslation.objects.create(
            category=source.product.category, locale="vi", name="Rau", description=""
        )

    farm = sources[0].farm
    Farm.objects.filter(pk=farm.pk).update(contact_phone="+84912345678")
    assert (
        farm_admin_api.post(
            f"/api/v1/admin/farms/{farm.pk}/growth-tracker", {}, format="json"
        ).status_code
        == 201
    )
    client = APIClient()
    for url in [
        "/api/v1/public/tracker-farms?locale=vi",
        "/api/v1/public/store-products?locale=vi",
        f"/api/v1/public/store-products/{sources[0].product.slug}?locale=vi",
    ]:
        response = client.get(url)
        assert response.status_code == 200
        assert b"contact_phone" not in response.content
        assert b"912345678" not in response.content


@pytest.mark.django_db(transaction=True)
def test_contact_phone_and_receipt_migrations_preserve_old_rows():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    old = [
        ("farms", "0007_farm_product_lifecycle_constraints"),
        ("content", "0021_admincreationreceipt"),
    ]
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        farm = apps.get_model("farms", "Farm").objects.create(**farm_payload())
        receipt = apps.get_model("content", "AdminCreationReceipt").objects.create(
            action="create_product", key=uuid4(), request_fingerprint="a" * 64, resource_id=uuid4()
        )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        assert Farm.objects.get(pk=farm.pk).contact_phone == ""
        upgraded = AdminCreationReceipt.objects.get(pk=receipt.pk)
        assert upgraded.created_count is None
        assert upgraded.resource_id == receipt.resource_id
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_revalidation_failure_after_commit_can_recover(farm_admin_api, sources, monkeypatch):
    def unavailable(tags):
        raise RuntimeError("revalidation unavailable")

    monkeypatch.setattr("commerce.services.request_frontend_revalidation", unavailable)
    payload = bulk_payload(sources)
    url = bulk_url(sources[0].farm_id)
    with pytest.raises(RuntimeError, match="revalidation unavailable"):
        farm_admin_api.post(url, payload, format="json")
    assert FarmProductStockBatch.objects.count() == 2
    assert AdminCreationReceipt.objects.count() == 1
    replay = farm_admin_api.post(url, payload, format="json")
    assert replay.status_code == 200
    assert replay.json()["meta"]["idempotent_replay"] is True
    assert ContentAuditLog.objects.filter(action="farm.stock_batch.create").count() == 2


def test_bulk_write_failure_rolls_back_audit_receipt_and_versions(
    farm_admin_api, sources, monkeypatch, django_capture_on_commit_callbacks
):
    from farms import services

    real_audit = services.audit
    count = 0

    def failing_audit(**kwargs):
        nonlocal count
        count += 1
        real_audit(**kwargs)
        if count == 2:
            raise RuntimeError("database write interrupted")

    monkeypatch.setattr(services, "audit", failing_audit)
    with django_capture_on_commit_callbacks() as callbacks:
        with pytest.raises(RuntimeError, match="database write interrupted"):
            farm_admin_api.post(bulk_url(sources[0].farm_id), bulk_payload(sources), format="json")
    assert not FarmProductStockBatch.objects.exists()
    assert not AdminCreationReceipt.objects.exists()
    assert not ContentAuditLog.objects.filter(action="farm.stock_batch.create").exists()
    assert set(FarmProduct.objects.values_list("version", flat=True)) == {1}
    assert not callbacks


def test_bulk_unknown_farm_and_missing_recovery_do_not_create_receipts(farm_admin_api, sources):
    response = farm_admin_api.post(bulk_url(uuid4()), bulk_payload(sources), format="json")
    assert response.status_code == 404
    assert not AdminCreationReceipt.objects.exists()
    assert not FarmProductStockBatch.objects.exists()
    response = farm_admin_api.get(bulk_url(sources[0].farm_id), {"creation_key": str(uuid4())})
    assert response.status_code == 404
    assert response.json() == {"errors": [{"code": "create_receipt_not_found"}]}


def test_bulk_validation_response_shape(farm_admin_api, sources):
    payload = bulk_payload(sources)
    payload["items"][0]["quantity_units"] = 0
    response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 400
    assert response.json() == {
        "items": {"0": {"quantity_units": ["Ensure this value is greater than or equal to 1."]}}
    }
    payload["items"][0]["quantity_units"] = 2147483647
    response = farm_admin_api.post(bulk_url(sources[0].farm_id), payload, format="json")
    assert response.status_code == 201
    assert FarmProductStockBatch.objects.get(farm_product=sources[0]).quantity_units == 2147483647


@pytest.mark.parametrize("bulk_first", [True, False])
def test_bulk_and_store_checkout_preserve_quote_contract(
    store_product, active_user, checkout_operation, bulk_first
):
    from commerce.models import StoreOrder
    from commerce.store_orders import StoreOrderError
    from farms.services import create_stock_batches_bulk
    from tests.test_farm_bulk_supply_mysql import command

    source = store_product.farm_offers.get()
    data = command([source], active_user)
    if bulk_first:
        create_stock_batches_bulk(**data)
        with pytest.raises(StoreOrderError, match="store_requote_required") as error:
            checkout_operation()
        assert error.value.context["available_quantity"] == 20
        assert not StoreOrder.objects.exists()
    else:
        order, _, created = checkout_operation()
        assert created
        create_stock_batches_bulk(**data)
        assert order.lines.get().stock_reservations.get().quantity == 2
    assert source.stock_batches.count() == 2


@pytest.mark.parametrize("bulk_first", [True, False])
def test_bulk_and_csa_selection_preserve_holds(context, bulk_first):
    from farms.services import create_stock_batches_bulk
    from tests.test_csa_rounds import save
    from tests.test_farm_bulk_supply_mysql import command

    actor, product, _, _, _ = context
    source = product.farm_offers.get()
    data = command([source], actor)
    if bulk_first:
        create_stock_batches_bulk(**data)
    selection = save(context, quantity=1)
    if not bulk_first:
        create_stock_batches_bulk(**data)
    assert selection.stock_holds.get().quantity == 1
    assert source.stock_batches.count() == 2
