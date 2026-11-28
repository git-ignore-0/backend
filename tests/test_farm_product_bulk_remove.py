from datetime import timedelta
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import Product, StoreStockAllocation, StoreStockMovement, StoreStockReservation
from commerce.store_orders import approve_store_order
from content.models import AdminCreationReceipt, ContentAuditLog
from farms import services
from farms.models import FarmProduct, FarmProductStockBatch
from farms.serializers import FARM_BULK_MAX_ITEMS
from memberships.csa_rounds import effective_stock_hold_rows
from memberships.models import CSAStockHold
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save as save_selection
from tests.test_farm_source_lifecycle import order_for
from tests.test_farm_stock_batches import make_farm, make_product
from tests.test_farms_api import farm_admin_api as farm_admin_api
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def bulk_url(farm_id):
    return f"/api/v1/admin/farms/{farm_id}/products/bulk-remove"


def item(source):
    return {
        "product_id": str(source.product_id),
        "expected_link_id": str(source.pk),
        "expected_version": source.version,
    }


def payload(sources):
    return {"idempotency_key": str(uuid4()), "items": [item(source) for source in sources]}


@pytest.fixture
def sources():
    farm = make_farm()
    return [
        FarmProduct.objects.create(farm=farm, product=make_product(f"REMOVE-{index}"))
        for index in range(3)
    ]


def test_bulk_removal_publishes_one_receipt(farm_admin_api, sources, active_user):
    command = payload(sources)
    url = bulk_url(sources[0].farm_id)
    result = farm_admin_api.post(url, command, format="json")
    assert result.status_code == 200
    assert result.json() == {
        "data": {"farm_id": str(sources[0].farm_id), "removed_count": 3},
        "meta": {
            "idempotent_replay": False,
            "action_receipt": {
                "resource_id": str(sources[0].farm_id),
                "action": "remove_farm_products",
                "idempotency_key": command["idempotency_key"],
            },
        },
    }
    for source in sources:
        source.refresh_from_db()
        assert (source.is_active, source.version, source.supply_generation) == (False, 2, 1)
        assert source.removed_by_id == active_user.pk
    assert len({source.removed_at for source in sources}) == 1
    assert ContentAuditLog.objects.filter(action="farm.product.remove").count() == 3
    assert AdminCreationReceipt.objects.count() == 1


@pytest.mark.parametrize("index", [0, 1, 2])
def test_stale_item_rolls_back_complete_operation(farm_admin_api, sources, index):
    command = payload(sources)
    command["items"][index]["expected_version"] += 1
    result = farm_admin_api.post(bulk_url(sources[0].farm_id), command, format="json")
    assert result.status_code == 409
    error = result.json()["errors"][0]
    assert error["code"] == "farm_product_version_conflict"
    assert error["context"]["item_index"] == index
    assert error["context"]["product_id"] == str(sources[index].product_id)
    assert (
        FarmProduct.objects.filter(is_active=True, version=1, removed_at__isnull=True).count() == 3
    )
    assert not ContentAuditLog.objects.filter(action="farm.product.remove").exists()
    assert not AdminCreationReceipt.objects.exists()


def stock(source, units=10):
    today = timezone.localdate()
    return source.stock_batches.create(
        start_date=today,
        end_date=today + timedelta(days=5),
        quantity_units=units,
        supply_generation=source.supply_generation,
    )


def assert_rolled_back(farm_id, active_count):
    assert (
        FarmProduct.objects.filter(farm_id=farm_id, removed_at__isnull=True, version=1).count()
        == active_count
    )
    assert not ContentAuditLog.objects.filter(action="farm.product.remove").exists()
    assert not AdminCreationReceipt.objects.filter(action="remove_farm_products").exists()


@pytest.mark.parametrize("count", [1, 3])
def test_removal_is_farm_scoped_and_keeps_history(
    farm_admin_api, sources, count, django_capture_on_commit_callbacks
):
    selected = sources[:count]
    batches = [stock(source) for source in sources]
    other = FarmProduct.objects.create(farm=make_farm("Other"), product=sources[0].product)
    other_batch = stock(other, 7)
    with django_capture_on_commit_callbacks() as callbacks:
        result = farm_admin_api.post(bulk_url(sources[0].farm_id), payload(selected), format="json")
    assert result.status_code == 200
    assert result["Cache-Control"] == "private, no-store"
    assert result.json()["data"]["removed_count"] == count
    assert len(callbacks) == 1
    other.refresh_from_db()
    assert (other.is_active, other.version) == (True, 1)
    assert Product.objects.filter(status="active").count() == 3
    assert FarmProduct.objects.count() == 4
    assert FarmProductStockBatch.objects.count() == 4
    for batch in [*batches, other_batch]:
        batch.refresh_from_db()
        assert (batch.supply_generation, batch.cancelled_at) == (1, None)
        assert batch.quantity_units == (7 if batch == other_batch else 10)
    events = ContentAuditLog.objects.filter(action="farm.product.remove")
    assert set(events.values_list("entity_id", flat=True)) == {
        str(source.pk) for source in selected
    }
    assert all(event.context["supply_generation"] == 1 for event in events)

    path = f"/api/v1/admin/farms/{sources[0].farm_id}"
    current = farm_admin_api.get(f"{path}/products?supply_status=supplying").json()["data"]
    assert {row["id"] for row in current} == {str(source.pk) for source in sources[count:]}
    detail = farm_admin_api.get(path).json()["data"]
    assert detail["in_stock_product_count"] == 3 - count
    assert detail["out_of_stock_product_count"] == 0
    assert detail["capabilities"]["can_add_supply"] == (count < 3)
    available = farm_admin_api.get(f"{path}/available-products").json()["data"]
    assert {row["removed_source"]["id"] for row in available} == {
        str(source.pk) for source in selected
    }


@pytest.mark.parametrize(
    ("level", "field", "value"),
    [
        ("body", "idempotency_key", "bad"),
        ("body", "idempotency_key", 7),
        ("body", "idempotency_key", None),
        ("body", "items", []),
        ("body", "items", {}),
        ("body", "items", None),
        ("body", "items", [None]),
        ("body", "farm_id", 1),
        ("body", "actor", 1),
        ("item", "product_id", 1),
        ("item", "product_id", None),
        ("item", "product_id", "bad"),
        ("item", "expected_link_id", 1),
        ("item", "expected_link_id", "bad"),
        ("item", "expected_link_id", None),
        ("item", "expected_version", 0),
        ("item", "expected_version", -1),
        ("item", "expected_version", True),
        ("item", "expected_version", "1"),
        ("item", "expected_version", 1.0),
        ("item", "expected_version", None),
        ("item", "farm_id", 1),
        ("item", "quantity_units", 1),
    ],
)
def test_invalid_values_are_rejected(farm_admin_api, sources, level, field, value):
    body = payload(sources)
    target = body if level == "body" else body["items"][0]
    target[field] = value
    result = farm_admin_api.post(bulk_url(sources[0].farm_id), body, format="json")
    assert result.status_code == 400
    assert result["Cache-Control"] == "private, no-store"
    assert_rolled_back(sources[0].farm_id, 3)


@pytest.mark.parametrize(
    "field", ["idempotency_key", "items", "product_id", "expected_link_id", "expected_version"]
)
def test_required_fields_are_not_defaulted(farm_admin_api, sources, field):
    body = payload(sources)
    target = body if field in body else body["items"][0]
    del target[field]
    assert farm_admin_api.post(bulk_url(sources[0].farm_id), body, format="json").status_code == 400
    assert_rolled_back(sources[0].farm_id, 3)


@pytest.mark.parametrize("field", ["product_id", "expected_link_id"])
def test_duplicate_ids_are_rejected(farm_admin_api, sources, field):
    body = payload(sources)
    body["items"][1][field] = body["items"][0][field].upper()
    assert farm_admin_api.post(bulk_url(sources[0].farm_id), body, format="json").status_code == 400
    assert_rolled_back(sources[0].farm_id, 3)


def test_bulk_source_limit(farm_admin_api, sources):
    body = payload([])
    body["items"] = [
        {"product_id": str(uuid4()), "expected_link_id": str(uuid4()), "expected_version": 1}
        for _ in range(FARM_BULK_MAX_ITEMS + 1)
    ]
    assert farm_admin_api.post(bulk_url(sources[0].farm_id), body, format="json").status_code == 400
    assert_rolled_back(sources[0].farm_id, 3)


@pytest.mark.parametrize(
    "case",
    [
        "missing_product",
        "missing_source",
        "only_other_farm",
        "other_farm",
        "other_product",
        "removed",
    ],
)
def test_stale_and_mismatched_sources_are_rejected(farm_admin_api, sources, case):
    source = sources[1]
    body = payload(sources)
    target = body["items"][1]
    code = "farm_product_changed"
    active_count = 3
    if case == "missing_product":
        target["product_id"] = str(uuid4())
    elif case in {"missing_source", "only_other_farm"}:
        product = make_product("MISSING-LINK")
        target["product_id"] = str(product.pk)
        if case == "only_other_farm":
            outside = FarmProduct.objects.create(farm=make_farm("Outside"), product=product)
            target["expected_link_id"] = str(outside.pk)
    elif case == "other_farm":
        foreign = FarmProduct.objects.create(farm=make_farm("Foreign"), product=source.product)
        target["expected_link_id"] = str(foreign.pk)
        code = "farm_product_identity_changed"
    elif case == "other_product":
        foreign = FarmProduct.objects.create(
            farm=source.farm, product=make_product("OTHER-PRODUCT")
        )
        target["expected_link_id"] = str(foreign.pk)
        code = "farm_product_identity_changed"
        active_count = 4
    else:
        FarmProduct.objects.filter(pk=source.pk).update(removed_at=timezone.now(), is_active=False)
        code = "farm_product_removed"
        active_count = 2
    response = farm_admin_api.post(bulk_url(source.farm_id), body, format="json")
    assert response.status_code == 409, response.json()
    error = response.json()["errors"][0]
    assert (error["code"], error["context"]["item_index"], error["context"]["product_id"]) == (
        code,
        1,
        target["product_id"],
    )
    if case == "only_other_farm":
        assert "source_id" not in error["context"]
        assert "version" not in error["context"]
    assert_rolled_back(source.farm_id, active_count)


def test_missing_farm_and_protected_routes(farm_admin_api, sources, active_user):
    body = payload(sources)
    url = bulk_url(sources[0].farm_id)
    query = {"creation_key": body["idempotency_key"]}
    assert APIClient().post(url, body, format="json").status_code == 401
    assert APIClient().get(url, query).status_code == 401
    assert farm_admin_api.post(bulk_url(uuid4()), body, format="json").status_code == 404
    active_user.groups.clear()
    assert farm_admin_api.post(url, body, format="json").status_code == 403
    assert farm_admin_api.get(url, query).status_code == 403
    assert_rolled_back(sources[0].farm_id, 3)


def test_permission_is_rechecked_after_stock_locks(
    farm_admin_api, sources, active_user, monkeypatch, django_capture_on_commit_callbacks
):
    lock_sources = services._lock_farm_stock_sources

    def revoke(*args):
        locked = lock_sources(*args)
        active_user.groups.clear()
        return locked

    monkeypatch.setattr(services, "_lock_farm_stock_sources", revoke)
    with django_capture_on_commit_callbacks() as callbacks:
        result = farm_admin_api.post(bulk_url(sources[0].farm_id), payload(sources), format="json")
    assert result.status_code == 403
    assert result["Cache-Control"] == "private, no-store"
    assert not callbacks
    assert_rolled_back(sources[0].farm_id, 3)


def test_removed_receipt_replays_original_after_readd_and_is_scoped(
    farm_admin_api, sources, active_user, access_token, django_capture_on_commit_callbacks
):
    source = sources[0]
    original_batch = stock(source)
    body = payload(sources)
    url = bulk_url(source.farm_id)
    lookup = {"creation_key": body["idempotency_key"]}
    missing = farm_admin_api.get(url, lookup)
    assert missing.status_code == 404
    assert missing.json() == {"errors": [{"code": "create_receipt_not_found"}]}
    response = farm_admin_api.post(url, body, format="json")
    assert response.status_code == 200
    snapshot = response.json()
    snapshot["meta"]["idempotent_replay"] = True

    source.refresh_from_db()
    prefix = f"/api/v1/admin/farms/{source.farm_id}/products"
    restored = farm_admin_api.post(
        f"{prefix}/{source.product_id}/re-add",
        {
            "idempotency_key": str(uuid4()),
            "expected_link_id": str(source.pk),
            "expected_version": source.version,
        },
        format="json",
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["available_quantity"] == 0
    assert restored.json()["data"]["supply_generation"] == 2
    assert original_batch.supply_generation == 1
    assert FarmProductStockBatch.objects.count() == 1

    with django_capture_on_commit_callbacks() as callbacks:
        for command in [body, {**body, "items": list(reversed(body["items"]))}]:
            replay = farm_admin_api.post(url, command, format="json")
            assert replay.status_code == 200
            assert replay.json() == snapshot
        recovery = farm_admin_api.get(url, lookup)
        assert recovery.status_code == 200
        assert recovery["Cache-Control"] == "private, no-store"
        assert recovery.json() == snapshot
    assert not callbacks

    source.refresh_from_db()
    assert (source.is_active, source.version, source.supply_generation) == (True, 3, 2)
    assert ContentAuditLog.objects.filter(action="farm.product.remove").count() == 3
    assert AdminCreationReceipt.objects.filter(action="remove_farm_products").count() == 1

    for items in [[item(source), *body["items"][1:]], body["items"][1:]]:
        failure = farm_admin_api.post(url, {**body, "items": items}, format="json")
        assert failure.status_code == 409
        assert failure.json()["errors"][0]["code"] == "idempotency_conflict"

    other_farm = make_farm("Other")
    wrong_farm_url = bulk_url(other_farm.pk)
    assert farm_admin_api.post(wrong_farm_url, body, format="json").status_code == 409
    for wrong_url in [wrong_farm_url, f"{prefix}/bulk", f"{prefix}/{source.product_id}/re-add"]:
        assert farm_admin_api.get(wrong_url, lookup).json() == missing.json()

    other_actor = type(active_user).objects.create_user(
        email="other-bulk-remove@example.com",
        name="Other admin",
        status="active",
    )
    other_actor.groups.add(Group.objects.get(name="website_admin"))
    other_session = APIClient()
    other_session.force_authenticate(user=other_actor, token=access_token)
    assert other_session.get(url, lookup).json() == missing.json()
    for query in [{}, {"creation_key": "bad"}, {**lookup, "extra": "bad"}]:
        assert farm_admin_api.get(url, query).status_code == 400
    key = body["idempotency_key"]
    assert farm_admin_api.get(f"{url}?creation_key={key}&creation_key={key}").status_code == 400


@pytest.mark.parametrize("phase", ["source", "audit", "receipt"])
def test_failed_write_rolls_back_sources_audit_receipt_and_callbacks(
    farm_admin_api, sources, phase, monkeypatch, django_capture_on_commit_callbacks
):
    if phase == "source":
        real = FarmProduct.objects.bulk_update

        def fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("write failure")

        monkeypatch.setattr(FarmProduct.objects, "bulk_update", fail)
    elif phase == "audit":
        real = ContentAuditLog.objects.bulk_create

        def fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("write failure")

        monkeypatch.setattr(ContentAuditLog.objects, "bulk_create", fail)
    else:
        real_save = AdminCreationReceipt.save

        def fail(self, *args, **kwargs):
            real_save(self, *args, **kwargs)
            if self.resource_id:
                raise RuntimeError("write failure")

        monkeypatch.setattr(AdminCreationReceipt, "save", fail)

    body = payload(sources)
    url = bulk_url(sources[0].farm_id)
    with (
        django_capture_on_commit_callbacks() as callbacks,
        pytest.raises(RuntimeError, match="write failure"),
    ):
        farm_admin_api.post(url, body, format="json")
    assert not callbacks
    assert_rolled_back(sources[0].farm_id, 3)
    assert farm_admin_api.get(url, {"creation_key": body["idempotency_key"]}).status_code == 404


def test_bulk_blocks_open_allocation_without_partial_removal(
    farm_admin_api, store_product, payment_settings, active_user, django_capture_on_commit_callbacks
):
    source = store_product.farm_offers.get()
    batch = source.stock_batches.get()
    good = FarmProduct.objects.create(farm_id=source.farm_id, product=make_product("GOOD"))
    order = order_for(store_product)
    line = order.lines.get()
    approve_store_order(
        order_id=order.pk,
        actor=active_user,
        idempotency_key=uuid4(),
        allocations=[{"line_id": line.pk, "batch_id": batch.pk, "quantity": line.quantity}],
    )
    with django_capture_on_commit_callbacks() as callbacks:
        result = farm_admin_api.post(
            bulk_url(source.farm_id), payload([good, source]), format="json"
        )
    assert result.status_code == 409
    error = result.json()["errors"][0]
    assert error["code"] == "farm_product_has_open_allocations"
    assert error["context"]["item_index"] == 1
    assert error["context"]["orders"] == [
        {"id": str(order.pk), "reference_code": order.reference_code}
    ]
    assert error["context"]["has_more"] is False
    assert StoreStockAllocation.objects.filter(line=line, batch=batch).count() == 1
    assert StoreStockMovement.objects.filter(batch=batch, kind="sale").count() == 1
    assert not callbacks
    assert_rolled_back(source.farm_id, 2)


@pytest.mark.parametrize("hold_kind", ["store", "csa"])
def test_bulk_blocks_store_and_csa_supply_shortfalls(
    farm_admin_api,
    store_product,
    payment_settings,
    context,
    hold_kind,
    django_capture_on_commit_callbacks,
):
    if hold_kind == "csa":
        _, product, _, _, _ = context
        selection = save_selection(context)
        assert effective_stock_hold_rows(now=timezone.now()).filter(selection=selection).exists()
        guard_rows = CSAStockHold.objects.filter(selection=selection)
    else:
        product = store_product
        order = order_for(product)
        guard_rows = StoreStockReservation.objects.filter(line__order=order)
        assert guard_rows.filter(released_at__isnull=True).exists()

    source = product.farm_offers.get()
    good = FarmProduct.objects.create(farm_id=source.farm_id, product=make_product("GOOD"))
    before = list(guard_rows.values())
    with django_capture_on_commit_callbacks() as callbacks:
        response = farm_admin_api.post(
            bulk_url(source.farm_id), payload([good, source]), format="json"
        )
    assert response.status_code == 409, response.json()
    error = response.json()["errors"][0]
    assert error["code"] == "farm_product_supply_shortfall"
    assert error["context"] == {
        "item_index": 1,
        "product_id": str(source.product_id),
        "source_id": str(source.pk),
        "version": 1,
        "required_units": 2,
        "remaining_units": 0,
        "shortfall_units": 2,
    }
    assert list(guard_rows.values()) == before
    assert not callbacks
    assert_rolled_back(source.farm_id, 2)


def test_removal_loads_sources_in_bulk_and_batches_writes(farm_admin_api):
    counts = []
    for count in (1, FARM_BULK_MAX_ITEMS):
        farm = make_farm(f"Farm {count}")
        sources = [
            FarmProduct.objects.create(farm=farm, product=make_product(f"PERF-{count}-{index}"))
            for index in range(count)
        ]
        with CaptureQueriesContext(connection) as queries:
            response = farm_admin_api.post(bulk_url(farm.pk), payload(sources), format="json")
        assert response.status_code == 200, response.json()
        counts.append(
            (
                len(queries),
                sum(query["sql"].startswith('UPDATE "farms_farmproduct"') for query in queries),
                sum(
                    query["sql"].startswith('INSERT INTO "content_contentauditlog"')
                    for query in queries
                ),
            )
        )
    print(f"Bulk remove 1 and 50 items, total queries / source writes / audit writes: {counts}")
    assert all(source_writes == audit_writes == 1 for _, source_writes, audit_writes in counts)
