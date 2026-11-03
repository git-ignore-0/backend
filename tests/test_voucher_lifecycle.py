import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, local

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from commerce.models import ShippingAddress, StoreOrder
from commerce.store_orders import (
    cancel_store_order,
    create_store_checkout,
    expire_order_if_needed,
    preview_store_voucher,
    record_store_payment,
    refund_store_order,
)
from identity.models import User
from memberships.coupons import CouponError
from memberships.expiry import expire_csa_purchase_request_if_due
from memberships.models import CouponUsage, CSACoupon, CSAPurchaseRequest
from memberships.purchases import (
    confirm_csa_payment_quote,
    create_csa_payment_quote,
    mark_csa_purchase_request_refunded,
    reject_csa_purchase_request,
)
from tests.test_csa_payment_quotes import catalog as catalog
from tests.test_shared_store_vouchers import checkout_body, preview
from tests.test_shared_store_vouchers import context as context
from tests.test_store_orders import (
    payment_settings as payment_settings,
)
from tests.test_store_orders import (
    store_product as store_product,
)
from tests.test_store_orders import (
    supported_area as supported_area,
)

pytestmark = pytest.mark.django_db


def csa_quote(catalog, user, voucher):
    # Transaction tests flush migration seed data between cases.
    from memberships.models import CSATermsClause, CSATermsDocument
    from memberships.terms import publish_csa_terms

    if not CSATermsDocument.objects.filter(status=CSATermsDocument.Status.PUBLISHED).exists():
        draft = CSATermsDocument.objects.create(
            version="VOUCHER_TEST_TERMS", title_vi="Điều khoản", title_en="Terms"
        )
        CSATermsClause.objects.create(
            document=draft,
            key="delivery",
            heading_vi="Giao hàng",
            heading_en="Delivery",
            paragraphs_vi=["Giao hàng."],
            paragraphs_en=["Delivery."],
        )
        publish_csa_terms(draft, actor=None, expected_revision=1)
    package, option = catalog
    return create_csa_payment_quote(
        package_id=package.pk, price_option_id=option.pk, user=user, coupon_code=voucher.code
    )[0]


@pytest.mark.parametrize("transition", ["reject", "expiry"])
def test_csa_releases_once_and_allows_same_uuid_in_store(context, active_user, catalog, transition):
    _, voucher, _ = context
    token = csa_quote(catalog, active_user, voucher)
    item, _ = confirm_csa_payment_quote(quote_token=token, user=active_user)
    assert CouponUsage.objects.get().purchase_request == item
    from commerce.serializers import StoreVoucherPreviewSerializer

    serializer = StoreVoucherPreviewSerializer(data=context[2])
    serializer.is_valid(raise_exception=True)
    with pytest.raises(CouponError, match="coupon_already_used"):
        preview_store_voucher(user=active_user, payload=serializer.validated_data)
    if transition == "reject":
        reject_csa_purchase_request(request_id=item.pk, reason="test", actor=active_user)
        reject_csa_purchase_request(request_id=item.pk, reason="test", actor=active_user)
        mark_csa_purchase_request_refunded(request_id=item.pk, actor=active_user)
        mark_csa_purchase_request_refunded(request_id=item.pk, actor=active_user)
    else:
        CSAPurchaseRequest.objects.filter(pk=item.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        assert expire_csa_purchase_request_if_due(request_id=item.pk)
        assert not expire_csa_purchase_request_if_due(request_id=item.pk)
    voucher.refresh_from_db()
    assert voucher.used_count == 0
    assert CouponUsage.objects.get().released_at is not None
    quote = preview(context)
    response = context[0].post(
        "/api/v1/public/store-checkouts", checkout_body(quote, voucher.code), format="json"
    )
    assert response.status_code == 201, response.data
    voucher.refresh_from_db()
    assert voucher.used_count == 1
    assert CouponUsage.objects.count() == 2


@pytest.mark.parametrize("transition", ["expiry", "cancel_refund"])
def test_store_releases_and_allows_same_uuid_in_csa(context, active_user, catalog, transition):
    client, voucher, _ = context
    quote = preview(context)
    body = checkout_body(quote, voucher.code)
    created = client.post("/api/v1/public/store-checkouts", body, format="json")
    assert created.status_code == 201
    order = StoreOrder.objects.get()
    with pytest.raises(CouponError, match="coupon_already_used"):
        csa_quote(catalog, active_user, voucher)
    if transition == "expiry":
        StoreOrder.objects.filter(pk=order.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        expire_order_if_needed(order.pk)
        expire_order_if_needed(order.pk)
    else:
        record_store_payment(order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4())
        key = uuid.uuid4()
        cancel_store_order(order_id=order.pk, actor=active_user, idempotency_key=key, reason="test")
        cancel_store_order(order_id=order.pk, actor=active_user, idempotency_key=key, reason="test")
        with pytest.raises(Exception, match="store_refund_state_conflict"):
            refund_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                amount_vnd=order.subtotal_vnd + order.shipping_fee_vnd,
                note="wrong before discount",
            )
        refund_key = uuid.uuid4()
        for _ in range(2):
            refund_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=refund_key,
                amount_vnd=order.total_vnd,
                note="paid total",
            )
    voucher.refresh_from_db()
    assert voucher.used_count == 0
    token = csa_quote(catalog, active_user, voucher)
    item, _ = confirm_csa_payment_quote(quote_token=token, user=active_user)
    assert item.coupon_snapshot["discount_amount"] == "30000"
    voucher.refresh_from_db()
    assert voucher.used_count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
@pytest.mark.parametrize("branches", [("store", "store"), ("csa", "csa"), ("csa", "store")])
def test_mysql_last_voucher_use_across_csa_and_store(context, catalog, active_user, branches):
    _, voucher, payload = context
    CSACoupon.objects.filter(pk=voucher.pk).update(usage_limit=1)
    voucher.refresh_from_db()
    second = User.objects.create_user(email="second-voucher@example.com", name="Second")
    ShippingAddress.objects.create(
        user=second,
        recipient_name="Second",
        phone_number="0987654321",
        province_code="66",
        ward_code="22015",
        address="Second address",
    )
    users = [active_user, second]
    operations = []
    for user, branch in zip(users, branches, strict=True):
        if branch == "csa":
            operations.append((branch, user, csa_quote(catalog, user, voucher)))
        else:
            from commerce.serializers import StoreVoucherPreviewSerializer

            serializer = StoreVoucherPreviewSerializer(data=payload)
            serializer.is_valid(raise_exception=True)
            quote = preview_store_voucher(user=user, payload=serializer.validated_data)
            body = checkout_body(quote, voucher.code)
            # Parse exactly as the transport does.
            from commerce.serializers import StoreCheckoutSerializer

            serializer = StoreCheckoutSerializer(data=body)
            serializer.is_valid(raise_exception=True)
            operations.append((branch, user, serializer.validated_data))
    barrier = Barrier(2)

    def attempt(operation):
        close_old_connections()
        try:
            branch, user, data = operation
            barrier.wait(timeout=10)
            if branch == "csa":
                confirm_csa_payment_quote(quote_token=data, user=user)
            else:
                create_store_checkout(user=user, payload=data)
            return "ok"
        except CouponError as error:
            return error.code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, operations))
    assert sorted(results) == ["coupon_usage_exhausted", "ok"]
    voucher.refresh_from_db()
    assert voucher.used_count == 1
    assert CouponUsage.objects.filter(coupon=voucher).count() == 1
    assert CSAPurchaseRequest.objects.count() + StoreOrder.objects.count() == 1


def ordered_coupon_pair():
    now = timezone.now()
    return [
        CSACoupon.objects.create(
            id=uuid.UUID(int=index),
            code=f"LOCK{index}",
            application_scope="both",
            discount_type="fixed",
            discount_value=30_000,
            usage_limit=2,
            starts_at=now - timedelta(days=1),
            expires_at=now + timedelta(days=1),
        )
        for index in (1, 2)
    ]


def test_store_checkout_locks_products_before_identity_and_identity_before_address(
    context, active_user, monkeypatch
):
    from django.db.models.query import QuerySet

    from commerce import store_orders
    from commerce.models import Product

    locked = []
    lock_identity = store_orders._lock_checkout_identity

    def observe_identity(user):
        locked.append(User)
        lock_identity(user)

    fetch = QuerySet._fetch_all

    def observe_locks(queryset):
        fetch(queryset)
        if queryset.query.select_for_update and queryset._result_cache:
            locked.append(queryset.model)

    quote = preview(context)
    monkeypatch.setattr(QuerySet, "_fetch_all", observe_locks)
    monkeypatch.setattr(store_orders, "_lock_checkout_identity", observe_identity)
    response = context[0].post(
        "/api/v1/public/store-checkouts", checkout_body(quote, context[1].code), format="json"
    )
    assert response.status_code == 201, response.data
    # Observe the requested order; SQLite does not implement row locks.
    assert locked.index(Product) < locked.index(User) < locked.index(ShippingAddress)
    assert locked.index(User) < locked.index(CSACoupon)


def test_csa_confirm_replays_winner_created_while_waiting_for_identity(
    context, catalog, active_user, monkeypatch
):
    from django.db.models.query import QuerySet

    token = csa_quote(catalog, active_user, context[1])
    winner, _ = confirm_csa_payment_quote(quote_token=token, user=active_user)
    first = QuerySet.first
    missed = False

    def miss_initial_nonce(queryset):
        nonlocal missed
        if not missed and queryset.model is CSAPurchaseRequest and queryset.query.select_for_update:
            missed = True
            return None
        return first(queryset)

    # Model the absent initial nonce read before the winner committed. Subsequent
    # identity locking, conflict checks, and database reads remain real.
    monkeypatch.setattr(QuerySet, "first", miss_initial_nonce)
    replay, _ = confirm_csa_payment_quote(quote_token=token, user=active_user)
    assert missed
    assert replay.pk == winner.pk
    assert CSAPurchaseRequest.objects.count() == CouponUsage.objects.count() == 1
    context[1].refresh_from_db()
    assert context[1].used_count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
def test_mysql_same_csa_quote_concurrent_confirms_replay_once(
    context, catalog, active_user, monkeypatch
):
    from django.db.models.query import QuerySet

    token = csa_quote(catalog, active_user, context[1])
    barrier = Barrier(2)
    state = local()
    first = QuerySet.first

    def synchronize_initial_nonce(queryset):
        result = first(queryset)
        if (
            queryset.model is CSAPurchaseRequest
            and queryset.query.select_for_update
            and not getattr(state, "initial_read", False)
        ):
            state.initial_read = True
            assert result is None
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(QuerySet, "first", synchronize_initial_nonce)

    def confirm(_):
        close_old_connections()
        try:
            return confirm_csa_payment_quote(quote_token=token, user=active_user)[0].pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(confirm, range(2)))
    assert len(set(ids)) == 1
    assert CSAPurchaseRequest.objects.count() == CouponUsage.objects.count() == 1
    context[1].refresh_from_db()
    assert context[1].used_count == 1


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
def test_mysql_checkout_and_approve_by_same_user_do_not_deadlock(
    context, active_user, store_product, monkeypatch
):
    from django.db.models.query import QuerySet
    from rest_framework.test import APIClient

    from commerce import store_orders
    from commerce.models import Product, StoreStockMovement
    from commerce.serializers import StoreCheckoutSerializer
    from farms.models import FarmProductStockBatch
    from tests.test_store_orders import guest_payload

    guest = APIClient()
    created = guest.post(
        "/api/v1/public/store-checkouts", guest_payload(store_product), format="json"
    )
    assert created.status_code == 201, created.data
    data = created.json()["data"]
    reported = guest.post(
        f"/api/v1/public/store-orders/{data['id']}/report-transfer",
        {"access_token": data["guest_access_token"]},
        format="json",
    )
    assert reported.status_code == 200, reported.data
    order = StoreOrder.objects.get(pk=data["id"])
    batch = FarmProductStockBatch.objects.get(farm_product__product=store_product)
    serializer = StoreCheckoutSerializer(data=checkout_body(preview(context), context[1].code))
    serializer.is_valid(raise_exception=True)
    product_locked, checkout_waiting = Event(), Event()
    state = local()
    lock_products = store_orders._lock_order_products
    fetch = QuerySet._fetch_all

    def hold_approval_products(order):
        lock_products(order)
        product_locked.set()
        assert checkout_waiting.wait(timeout=10)

    def observe_checkout_product_wait(queryset):
        if (
            getattr(state, "checkout", False)
            and queryset.model is Product
            and queryset.query.select_for_update
        ):
            checkout_waiting.set()
        fetch(queryset)

    monkeypatch.setattr(store_orders, "_lock_order_products", hold_approval_products)
    monkeypatch.setattr(QuerySet, "_fetch_all", observe_checkout_product_wait)

    def approve():
        close_old_connections()
        try:
            return store_orders.approve_store_order(
                order_id=order.pk,
                actor=active_user,
                idempotency_key=uuid.uuid4(),
                allocations=[
                    {"line_id": order.lines.get().pk, "batch_id": batch.pk, "quantity": 2}
                ],
            ).pk
        finally:
            close_old_connections()

    def checkout():
        close_old_connections()
        try:
            assert product_locked.wait(timeout=10)
            state.checkout = True
            return create_store_checkout(user=active_user, payload=serializer.validated_data)[0].pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(approve)
        purchase = pool.submit(checkout)
        assert approval.result(timeout=30) == order.pk
        checkout_id = purchase.result(timeout=30)
    assert checkout_id != order.pk
    assert StoreStockMovement.objects.filter(actor=active_user).exists()
    assert CouponUsage.objects.filter(store_order_id=checkout_id).count() == 1


def test_csa_confirm_locks_expiring_and_new_vouchers_in_uuid_order(
    context, catalog, active_user, monkeypatch
):
    from django.db.models.query import QuerySet

    low, high = ordered_coupon_pair()
    old, _ = confirm_csa_payment_quote(
        quote_token=csa_quote(catalog, active_user, high), user=active_user
    )
    token = csa_quote(catalog, active_user, low)
    CSAPurchaseRequest.objects.filter(pk=old.pk).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    locked = []
    fetch = QuerySet._fetch_all

    def observe_locks(queryset):
        fetch(queryset)
        if queryset.model is CSACoupon and queryset.query.select_for_update:
            locked.extend(row.pk for row in queryset._result_cache)

    monkeypatch.setattr(QuerySet, "_fetch_all", observe_locks)
    item, _ = confirm_csa_payment_quote(quote_token=token, user=active_user)
    # This verifies requested lock order, not actual SQLite row locking.
    assert list(dict.fromkeys(locked)) == [low.pk, high.pk]
    assert item.coupon_id == low.pk
    old.refresh_from_db()
    assert old.status == "expired"
    low.refresh_from_db()
    high.refresh_from_db()
    assert (low.used_count, high.used_count) == (1, 0)


@pytest.mark.parametrize("expiry_path", ["user", "worker"])
def test_csa_bulk_expiry_locks_vouchers_in_uuid_order(
    context, catalog, active_user, monkeypatch, expiry_path
):
    from django.db.models.query import QuerySet

    from memberships.expiry import (
        expire_csa_purchase_requests,
        expire_open_csa_purchase_requests_for_user,
    )

    low, high = ordered_coupon_pair()
    first, _ = confirm_csa_payment_quote(
        quote_token=csa_quote(catalog, active_user, high), user=active_user
    )
    ShippingAddress.objects.filter(user=active_user).update(phone_number="0987654321")
    second, _ = confirm_csa_payment_quote(
        quote_token=csa_quote(catalog, active_user, low), user=active_user
    )
    now = timezone.now()
    CSAPurchaseRequest.objects.filter(pk=first.pk).update(expires_at=now - timedelta(seconds=2))
    CSAPurchaseRequest.objects.filter(pk=second.pk).update(expires_at=now - timedelta(seconds=1))
    locked = []
    fetch = QuerySet._fetch_all

    def observe_locks(queryset):
        fetch(queryset)
        if queryset.model is CSACoupon and queryset.query.select_for_update:
            locked.extend(row.pk for row in queryset._result_cache)

    monkeypatch.setattr(QuerySet, "_fetch_all", observe_locks)
    if expiry_path == "user":
        assert expire_open_csa_purchase_requests_for_user(user_id=active_user.pk, now=now) == 2
    else:
        assert expire_csa_purchase_requests(now=now) == 2
    assert list(dict.fromkeys(locked)) == [low.pk, high.pk]
    assert not CouponUsage.objects.filter(released_at__isnull=True).exists()
    assert list(
        CSACoupon.objects.filter(pk__in=[low.pk, high.pk]).values_list("used_count", flat=True)
    ) == [0, 0]


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
def test_mysql_concurrent_csa_confirms_swap_vouchers_while_expiring_old_requests(
    context, catalog, active_user
):
    from memberships.models import MembershipPackage, MembershipPackagePriceOption, QuotaPolicy

    low, high = ordered_coupon_pair()
    second = User.objects.create_user(email="swap-voucher@example.com", name="Second")
    ShippingAddress.objects.create(
        user=second,
        recipient_name="Second",
        phone_number="0987654321",
        province_code="66",
        ward_code="22015",
        address="Second address",
    )
    package = MembershipPackage.objects.create(name="Second CSA", quota_policy=QuotaPolicy.EXPIRE)
    option = MembershipPackagePriceOption.objects.create(
        package=package, duration_months=3, monthly_price_vnd=400_000
    )
    second_catalog = (package, option)
    first_old, _ = confirm_csa_payment_quote(
        quote_token=csa_quote(catalog, active_user, high), user=active_user
    )
    second_old, _ = confirm_csa_payment_quote(
        quote_token=csa_quote(second_catalog, second, low), user=second
    )
    operations = [
        (active_user, csa_quote(catalog, active_user, low)),
        (second, csa_quote(second_catalog, second, high)),
    ]
    CSAPurchaseRequest.objects.filter(pk__in=[first_old.pk, second_old.pk]).update(
        expires_at=timezone.now() - timedelta(seconds=1)
    )
    barrier = Barrier(2)

    def confirm(operation):
        close_old_connections()
        try:
            user, token = operation
            barrier.wait(timeout=10)
            item, _ = confirm_csa_payment_quote(quote_token=token, user=user)
            return item.pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(confirm, operations))
    assert len(set(ids)) == 2
    assert CSAPurchaseRequest.objects.filter(status="expired").count() == 2
    assert CSAPurchaseRequest.objects.filter(status="payment_confirmed").count() == 2
    assert CouponUsage.objects.filter(released_at__isnull=True).count() == 2
    low.refresh_from_db()
    high.refresh_from_db()
    assert (low.used_count, high.used_count) == (1, 1)


@pytest.mark.parametrize(
    "vendor,clause", [("mysql", "LOCK IN SHARE MODE"), ("postgresql", "FOR SHARE")]
)
def test_checkout_identity_uses_shared_lock(active_user, monkeypatch, vendor, clause):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from commerce import store_orders

    cursor = MagicMock()
    cursor.fetchone.return_value = (active_user.pk,)
    db = SimpleNamespace(
        vendor=vendor,
        ops=connection.ops,
        features=SimpleNamespace(has_native_uuid_field=vendor == "postgresql"),
        cursor=MagicMock(),
    )
    db.cursor.return_value.__enter__.return_value = cursor
    monkeypatch.setattr(store_orders, "connection", db)
    store_orders._lock_checkout_identity(active_user)
    sql, params = cursor.execute.call_args.args
    assert sql.endswith(clause)
    assert "FOR UPDATE" not in sql
    assert params == [User._meta.pk.get_db_prep_value(active_user.pk, db)]
    cursor.fetchone.return_value = None
    with pytest.raises(User.DoesNotExist):
        store_orders._lock_checkout_identity(active_user)


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
def test_mysql_checkout_and_quota_cancel_by_same_user_do_not_deadlock(
    active_user, access_token, store_product, payment_settings, supported_area, monkeypatch
):
    from commerce import store_orders
    from commerce.serializers import StoreCheckoutSerializer
    from tests.test_store_orders import (
        approve_review_order,
        attach_active_membership,
        create_review_quota_checkout,
        quota_checkout_payload,
    )

    attach_active_membership(active_user, store_product)
    data = create_review_quota_checkout(active_user, access_token, store_product)
    order = approve_review_order(data, active_user)
    serializer = StoreCheckoutSerializer(
        data=quota_checkout_payload(
            store_product,
            ShippingAddress.objects.get(user=active_user),
            quantity=1,
            expected_available=9,
        )
    )
    serializer.is_valid(raise_exception=True)
    member_locked, identity_locked = Event(), Event()
    state = local()
    lock_membership = store_orders._lock_order_memberships
    lock_identity = store_orders._lock_checkout_identity

    def hold_cancel_membership(item):
        lock_membership(item)
        if getattr(state, "cancel", False):
            member_locked.set()
            assert identity_locked.wait(timeout=10)

    def observe_checkout_identity(user):
        lock_identity(user)
        if getattr(state, "checkout", False):
            identity_locked.set()

    monkeypatch.setattr(store_orders, "_lock_order_memberships", hold_cancel_membership)
    monkeypatch.setattr(store_orders, "_lock_checkout_identity", observe_checkout_identity)

    def cancel():
        close_old_connections()
        try:
            state.cancel = True
            return cancel_store_order(
                order_id=order.pk, actor=active_user, idempotency_key=uuid.uuid4(), reason="test"
            ).pk
        finally:
            close_old_connections()

    def checkout():
        close_old_connections()
        try:
            assert member_locked.wait(timeout=10)
            state.checkout = True
            return create_store_checkout(user=active_user, payload=serializer.validated_data)[0].pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancellation, purchase = pool.submit(cancel), pool.submit(checkout)
        assert cancellation.result(timeout=30) == order.pk
        assert purchase.result(timeout=30) != order.pk
