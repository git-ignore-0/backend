from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from tests.test_csa_payment_quotes import catalog as catalog
from tests.test_shared_store_vouchers import context as context
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product


@pytest.fixture(autouse=True)
def voucher_schema(transactional_db):
    # Legacy migration tests restore only their own historical target. Establish
    # the schema explicitly before any current-model fixtures in this module.
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.fixture(autouse=True)
def supported_area(voucher_schema):
    from commerce.models import ShippingArea

    return ShippingArea.objects.create(
        province_code="66", ward_code="22015", shipping_fee_vnd=30_000, is_active=True
    )


@pytest.mark.django_db(transaction=True)
def test_voucher_migration_preserves_coupon_ids_counts_without_inventing_usage():
    # Historical state is used so this also detects accidental table/model renames.
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old = [
        ("memberships", "0032_fractional_csa_points"),
        ("commerce", "0006_fractional_csa_points"),
    ]
    try:
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        Coupon = apps.get_model("memberships", "CSACoupon")
        now = timezone.now()
        coupon = Coupon.objects.create(
            code="LEGACY",
            discount_type="percent",
            discount_value=Decimal("12.50"),
            usage_limit=7,
            used_count=3,
            starts_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=1),
            package_ids=[],
            price_option_ids=[],
            payment_types=[],
            installment_counts=[],
        )
        old_id = coupon.pk
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        apps = executor.loader.project_state(latest).apps
        migrated = apps.get_model("memberships", "CSACoupon").objects.get(pk=old_id)
        assert migrated.application_scope == "csa"
        assert migrated.code == "LEGACY"
        assert migrated.discount_value == Decimal("12.50")
        assert migrated.used_count == 3
        assert migrated.usage_limit == 7
        assert not apps.get_model("memberships", "CouponUsage").objects.exists()
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
def test_usage_backfill_uses_exact_request_fk_without_inferring_guest_uuid(
    context, catalog, active_user
):
    from memberships.purchases import confirm_csa_payment_quote
    from tests.test_voucher_lifecycle import csa_quote

    _, coupon, _ = context
    token = csa_quote(catalog, active_user, coupon)
    item, _ = confirm_csa_payment_quote(quote_token=token, user=active_user)
    snapshot, item_id, coupon_id = item.coupon_snapshot, item.pk, coupon.pk
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old = [
        ("memberships", "0032_fractional_csa_points"),
        ("commerce", "0006_fractional_csa_points"),
    ]
    try:
        executor.migrate(old)
        old_apps = executor.loader.project_state(old).apps
        # Existing guest request is evidence of consumption, never evidence of UUID.
        Request = old_apps.get_model("memberships", "CSAPurchaseRequest")
        Request.objects.filter(pk=item_id).update(user_id=None)
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        apps = executor.loader.project_state(latest).apps
        restored = apps.get_model("memberships", "CSAPurchaseRequest").objects.get(pk=item_id)
        usage = apps.get_model("memberships", "CouponUsage").objects.get(
            purchase_request_id=item_id
        )
        assert restored.coupon_id == coupon_id
        assert restored.coupon_snapshot == snapshot
        assert usage.coupon_id == coupon_id
        assert usage.user_id is None
        assert usage.active_slot is True
        assert apps.get_model("memberships", "CSACoupon").objects.get(pk=coupon_id).used_count == 1
    finally:
        MigrationExecutor(connection).migrate(latest)
