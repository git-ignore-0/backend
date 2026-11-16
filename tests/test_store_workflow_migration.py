"""A historical schema fixture proves legacy semantics survive the upgrade."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.store_orders import _guest_access_token, _hash, request_fingerprint
from tests.test_store_orders import guest_payload
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("legacy_without_date", [False, True])
def test_migration_preserves_legacy_states_deadlines_bank_and_exact_create_retry(
    store_product,
    supported_area,
    legacy_without_date,
):
    latest = MigrationExecutor(connection).loader.graph.leaf_nodes()
    old = [
        ("commerce", "0009_storeorder_delivery_date_and_more"),
        ("memberships", "0037_csaround_companion_delivery_cutoff_and_more"),
    ]
    try:
        executor = MigrationExecutor(connection)
        executor.migrate(old)
        apps = executor.loader.project_state(old).apps
        OldOrder = apps.get_model("commerce", "StoreOrder")
        OldLine = apps.get_model("commerce", "StoreOrderLine")
        now = timezone.now()
        payload = guest_payload(store_product)
        from commerce.serializers import StoreCheckoutSerializer

        serializer = StoreCheckoutSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        validated = dict(serializer.validated_data)
        if legacy_without_date:
            payload.pop("delivery_date")
            validated.pop("delivery_date")
        fingerprint = request_fingerprint(validated)
        key = payload["idempotency_key"]
        rows = []
        for index, state in enumerate(
            ("payment_pending", "pending_review", "pending_delivery", "cancelled")
        ):
            order_id = uuid4()
            order_key = key if index == 0 else uuid4()
            token = _guest_access_token(order_id, order_key)
            deadline = (
                now + timedelta(minutes=4)
                if index == 0
                else (now + timedelta(hours=7) if index == 1 else None)
            )
            item = OldOrder.objects.create(
                id=order_id,
                reference_code=f"LEGACY-{index}",
                create_idempotency_key=order_key,
                request_fingerprint=fingerprint,
                locale="vi",
                recipient_name_snapshot="Nguyễn Văn An",
                phone_e164_snapshot="+84901234567",
                province_code_snapshot="66",
                ward_code_snapshot="22015",
                address_snapshot="Số 12 Nguyễn Huệ",
                status=state,
                payment_status="received" if index > 1 else "unconfirmed",
                subtotal_vnd=90000,
                shipping_fee_vnd=30000,
                total_vnd=120000,
                delivery_date=timezone.localdate()
                if index == 0 and not legacy_without_date
                else None,
                bank_bin_snapshot="970436",
                bank_name_snapshot="Old bank",
                account_number_snapshot="123456789",
                account_name_snapshot="OLD ACCOUNT",
                transfer_content=f"LEGACY-CONTENT-{index}",
                guest_access_token_hash=_hash(token),
                expires_at=deadline,
                transfer_reported_at=now if index == 1 else None,
                approved_at=now if index == 2 else None,
            )
            OldLine.objects.create(
                order=item,
                product_id=store_product.pk,
                position=0,
                sku_snapshot="OLD-SKU",
                name_snapshot="Old name",
                unit_size_snapshot="1.000",
                unit_label_snapshot="kg",
                unit_price_vnd_snapshot=45000,
                quantity=2,
                cash_quantity=2,
                cash_subtotal_vnd=90000,
            )
            rows.append((item.pk, state, deadline, token))
        MigrationExecutor(connection).migrate(latest)
        from commerce.models import StoreOrder

        for pk, state, deadline, _ in rows:
            item = StoreOrder.objects.get(pk=pk)
            assert item.workflow_version == 1
            assert item.status == state
            assert item.expires_at == deadline
            assert item.bank_name_snapshot == "Old bank"
            if state != "payment_pending":
                assert item.delivery_date is None
        response = APIClient().post("/api/v1/public/store-checkouts", payload, format="json")
        assert response.status_code == 200, response.data
        data = response.data["data"]
        assert data["id"] == str(rows[0][0])
        assert data["workflow_version"] == 1
        assert data["status"] == "payment_pending"
        assert data["payment"]["bank_name"] == "Old bank"
        assert data["guest_access_token"] == rows[0][3]
        assert data["expires_at"] == rows[0][2].isoformat()
        assert StoreOrder.objects.count() == 4
        response = APIClient().get(
            f"/api/v1/public/store-orders/{rows[1][0]}",
            HTTP_X_STORE_ACCESS_TOKEN=rows[1][3],
        )
        assert response.status_code == 200
        assert response.data["data"]["status"] == "pending_review"
        assert response.data["data"]["payment"]["qr_payload"]["amount"] == 120000
    finally:
        MigrationExecutor(connection).migrate(latest)
