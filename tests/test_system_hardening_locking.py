import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import partial
from threading import Barrier
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.db import connection, connections
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import CatalogTreeLock, Category, Product
from commerce.services import CatalogConflict, update_product
from content.models import AdminCreationReceipt
from farms.models import Farm
from farms.services import (
    FarmConflict,
    create_growth_tracker_profile,
    delete_growth_tracker_profile,
    update_growth_tracker_profile,
)
from identity.models import AccountStatus, User
from memberships import csa_rounds
from memberships.models import CSARoundConfiguration, Membership, MembershipUsageOrder
from memberships.services import create_usage_order
from referrals.models import PointTransaction, ReferralAccount
from referrals.services import adjust_points, point_balance
from tests.test_commerce_catalog_api import product_payload
from tests.test_farm_deletion_locking import another_farm
from tests.test_product_lifecycle_locking import isolate_side_effects as isolate_side_effects
from tests.test_product_lifecycle_locking import run_in_lock_order
from tests.test_store_orders import attach_active_membership
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "mysql", reason="Requires MySQL row locks"),
]


@pytest.mark.parametrize("kind", ["product", "configuration"])
def test_mysql_concurrent_keyed_creation_writes_one_resource_receipt(active_user, kind):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    if kind == "product":
        category = Category.objects.create(slug="concurrency-create", is_active=True)
        path = "/api/v1/admin/store-products"
        body = product_payload(str(category.pk))
        resource_model = Product
    else:
        now = timezone.now()
        path = "/api/v1/admin/csa-round-configurations"
        body = {
            "name": "Concurrent draft",
            "opens_at": (now + timedelta(hours=1)).isoformat(),
            "closes_at": (now + timedelta(hours=2)).isoformat(),
            "delivery_date": (now.date() + timedelta(days=3)).isoformat(),
        }
        resource_model = CSARoundConfiguration
    body["idempotency_key"] = str(uuid.uuid4())
    before_insert = Barrier(2)
    table = AdminCreationReceipt._meta.db_table.upper()

    def create():
        db = connections["default"]
        client = APIClient()
        client.force_authenticate(active_user, token=SimpleNamespace(scope="content:manage"))

        def coordinate(execute, sql, params, many, context):
            statement = sql.upper()
            if statement.startswith("INSERT") and table in statement:
                before_insert.wait(timeout=10)
            return execute(sql, params, many, context)

        try:
            with db.execute_wrapper(coordinate):
                return client.post(path, body, format="json")
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(create)
        second = workers.submit(create)
        responses = [future.result(timeout=20) for future in (first, second)]
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["data"]["id"] == responses[1].json()["data"]["id"]
    assert resource_model.objects.count() == 1
    assert AdminCreationReceipt.objects.count() == 1


def test_mysql_concurrent_points_replay_keeps_one_ledger_entry(active_user):
    user = User.objects.create_user(
        email="points-race@example.com", name="Points race", status=AccountStatus.ACTIVE
    )
    adjust = partial(
        adjust_points,
        user=user,
        actor=active_user,
        direction="credit",
        amount=15,
        message="Concurrent adjustment",
        idempotency_key=uuid.uuid4(),
    )
    first, second = run_in_lock_order(adjust, adjust, left_first=True, lock_model=ReferralAccount)
    item, balance, created = first.result()
    replay, replay_balance, replayed_new = second.result()
    assert (item.pk, balance, created) == (replay.pk, 15, True)
    assert (replay_balance, replayed_new) == (15, False)
    assert PointTransaction.objects.filter(user=user).count() == 1
    assert point_balance(user) == 15


def test_mysql_concurrent_quota_replay_deducts_once(active_user, store_product):
    membership = attach_active_membership(active_user, store_product)
    use = partial(
        create_usage_order,
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": store_product.csa_quota_group_id, "units": 1}],
    )
    first, second = run_in_lock_order(use, use, left_first=True, lock_model=Membership)
    usage, created = first.result()
    replay, replayed_new = second.result()
    assert created and not replayed_new
    assert replay.pk == usage.pk
    assert MembershipUsageOrder.objects.filter(membership=membership).count() == 1
    assert membership.cycles.get().quotas.get().remaining_units == 3


def test_mysql_product_concurrent_edit_checks_version_after_lock(active_user, store_product):
    edit = partial(update_product, actor=active_user, product=store_product, version=1)
    first, second = run_in_lock_order(
        partial(edit, payload={"retail_price_vnd": 40000}),
        partial(edit, payload={"retail_price_vnd": 42000}),
        left_first=True,
        lock_model=CatalogTreeLock,
    )
    assert first.result().version == 2
    with pytest.raises(CatalogConflict, match="version_conflict"):
        second.result()
    store_product.refresh_from_db()
    assert store_product.retail_price_vnd == 40000


def test_mysql_csa_publish_cannot_commit_after_a_newer_draft_edit(active_user):
    now = timezone.now()
    root = csa_rounds.create_round_configuration(
        name="Concurrency draft",
        opens_at=now + timedelta(hours=1),
        closes_at=now + timedelta(hours=2),
        delivery_date=now.date() + timedelta(days=3),
        actor=active_user,
    )
    first, second = run_in_lock_order(
        partial(
            csa_rounds.update_round_configuration,
            configuration_id=root.pk,
            version=1,
            name="Fresh draft",
        ),
        partial(csa_rounds.publish_round_configuration, configuration_id=root.pk, version=1),
        left_first=True,
        lock_model=CSARoundConfiguration,
    )
    assert first.result().version == 2
    with pytest.raises(csa_rounds.CSARoundError, match="version_conflict"):
        second.result()
    assert not root.rounds.exists()


def test_mysql_tracker_delete_cannot_commit_after_a_newer_profile_edit(active_user):
    farm = another_farm()
    target = {"farm_id": farm.pk, "actor": active_user}
    profile = create_growth_tracker_profile(**target, data={})
    target["expected_version"] = profile.version
    first, second = run_in_lock_order(
        partial(update_growth_tracker_profile, **target, data={"is_visible": False}),
        partial(delete_growth_tracker_profile, **target),
        left_first=True,
        lock_model=Farm,
    )
    assert first.result().version == profile.version + 1
    with pytest.raises(FarmConflict, match="version_conflict"):
        second.result()
    profile.refresh_from_db()
    assert not profile.is_visible
