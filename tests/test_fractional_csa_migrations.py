from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor

from commerce.models import Product
from memberships.models import MembershipCycleQuota
from tests.test_store_orders import attach_active_membership
from tests.test_store_orders import store_product as store_product


@pytest.mark.django_db(transaction=True)
def test_fractional_schema_preserves_existing_integer_point_values(active_user, store_product):
    membership = attach_active_membership(active_user, store_product)
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate(
            [
                ("commerce", "0005_storeorder_referral_rewards_eligible"),
                ("memberships", "0031_store_usage_link"),
            ]
        )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        store_product.refresh_from_db()
        quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
        assert store_product.csa_quota_cost == Decimal("2.00")
        assert quota.remaining_units == Decimal("4.00")
        assert quota.granted_units == 4
        assert quota.expired_units == Decimal("0.00")
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("scope", ["commerce", "memberships"])
def test_rollback_refuses_to_truncate_fractional_points(active_user, store_product, scope):
    membership = attach_active_membership(active_user, store_product)
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    if scope == "commerce":
        Product.objects.filter(pk=store_product.pk).update(csa_quota_cost=Decimal("0.25"))
        target = ("commerce", "0005_storeorder_referral_rewards_eligible")
    else:
        MembershipCycleQuota.objects.filter(cycle__membership=membership).update(
            remaining_units=Decimal("3.75")
        )
        target = ("memberships", "0031_store_usage_link")
    try:
        with pytest.raises(IrreversibleError, match="fractional CSA"):
            MigrationExecutor(connection).migrate([target])
        if scope == "commerce":
            store_product.refresh_from_db()
            assert store_product.csa_quota_cost == Decimal("0.25")
        else:
            assert MembershipCycleQuota.objects.get(
                cycle__membership=membership
            ).remaining_units == Decimal("3.75")
    finally:
        MigrationExecutor(connection).migrate(latest)
