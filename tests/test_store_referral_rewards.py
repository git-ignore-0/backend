"""Store completion earns points; cash snapshots and the toggle bound eligibility."""

import uuid
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from commerce.models import StoreOrder, StoreOrderLine, StoreOrderStatus
from commerce.store_orders import deliver_store_order, record_store_payment
from identity.models import AccountStatus, User
from referrals.models import PointTransaction
from referrals.services import assign_referrer, point_balance
from tests.test_store_orders import payment_settings as payment_settings
from tests.test_store_orders import store_product as store_product


@pytest.fixture
def reward_program(settings):
    settings.STORE_REFERRAL_REWARDS_ENABLED = True


@pytest.fixture
def referral_pair(active_user, reward_program):
    buyer = User.objects.create_user(
        email="buyer@example.com", name="Buyer", status=AccountStatus.ACTIVE
    )
    referral = assign_referrer(referee=buyer, code=active_user.referral_account.code)
    return active_user, buyer, referral


@pytest.fixture
def order_factory(store_product, referral_pair):
    def create(*, cash=300_000, quota=0, buyer=None):
        order = StoreOrder.objects.create(
            reference_code=f"SO{uuid.uuid4().hex[:16]}",
            buyer_user=buyer or referral_pair[1],
            create_idempotency_key=uuid.uuid4(),
            request_fingerprint="a" * 64,
            referral_rewards_eligible=True,
            locale="vi",
            recipient_name_snapshot="Buyer",
            phone_e164_snapshot="+84901234567",
            province_code_snapshot="66",
            ward_code_snapshot="22015",
            address_snapshot="Private",
            status=StoreOrderStatus.PENDING_DELIVERY,
            subtotal_vnd=cash,
            shipping_fee_vnd=30_000,
            total_vnd=cash + 30_000,
        )
        cash_quantity = 0 if cash == 0 and quota else 1
        StoreOrderLine.objects.create(
            order=order,
            product=store_product,
            position=0,
            sku_snapshot="SKU",
            name_snapshot="Product",
            unit_size_snapshot=1,
            unit_label_snapshot="kg",
            unit_price_vnd_snapshot=cash if cash_quantity else 30_000,
            quantity=cash_quantity + quota,
            quota_quantity=quota,
            cash_quantity=cash_quantity,
            cash_subtotal_vnd=cash,
        )
        return order

    return create


def complete(order, actor, *, payment_first=True):
    actions = [record_store_payment, deliver_store_order]
    if not payment_first:
        actions.reverse()
    for action in actions:
        action(order_id=order.pk, actor=actor, idempotency_key=uuid.uuid4())


@pytest.mark.django_db
@pytest.mark.parametrize("payment_first", [True, False])
def test_completion_awards_two_distinct_milestones_once(
    order_factory, referral_pair, payment_first
):
    referrer, buyer, referral = referral_pair
    for expected in [100, 150, 150]:
        order = order_factory()
        complete(order, referrer, payment_first=payment_first)
        record_store_payment(order_id=order.pk, actor=referrer, idempotency_key=uuid.uuid4())
        assert point_balance(referrer) == expected
    ledger = list(PointTransaction.objects.filter(user=referrer).order_by("created_at"))
    assert [item.amount for item in ledger] == [100, 50]
    assert all(item.related_referral_id == referral.pk for item in ledger)
    assert ledger[0].message.startswith("Thưởng giới thiệu:")
    assert "SO" in ledger[0].message
    assert buyer.email not in ledger[0].message


@pytest.mark.django_db
@pytest.mark.parametrize(
    "cash,quota,want",
    [(299_999, 0, 0), (300_000, 0, 100), (0, 10, 0), (299_999, 10, 0), (300_000, 10, 100)],
)
def test_only_cash_goods_count(order_factory, referral_pair, cash, quota, want):
    order = order_factory(cash=cash, quota=quota)
    complete(order, referral_pair[0])
    assert point_balance(referral_pair[0]) == want


@pytest.mark.django_db
@pytest.mark.parametrize(
    "milestone,days,extra,want",
    [
        (100, 30, 0, 100),
        (100, 30, 1, 0),
        (50, 60, 0, 150),
        (50, 60, 1, 100),
    ],
)
def test_deadlines_are_inclusive(
    order_factory, referral_pair, monkeypatch, milestone, days, extra, want
):
    referrer, _, referral = referral_pair
    if milestone == 50:
        complete(order_factory(), referrer)
    order = order_factory()
    finish = referral.created_at + timedelta(days=days, microseconds=extra)
    monkeypatch.setattr(timezone, "now", lambda: finish)
    complete(order, referrer)
    assert point_balance(referrer) == want


@pytest.mark.django_db
@pytest.mark.parametrize("payment_first", [True, False])
def test_deadline_uses_later_of_delivery_and_receipt(
    order_factory, referral_pair, monkeypatch, payment_first
):
    referrer, _, referral = referral_pair
    order = order_factory()
    first, second = (
        (record_store_payment, deliver_store_order)
        if payment_first
        else (deliver_store_order, record_store_payment)
    )
    monkeypatch.setattr(timezone, "now", lambda: referral.created_at + timedelta(days=29))
    first(order_id=order.pk, actor=referrer, idempotency_key=uuid.uuid4())
    monkeypatch.setattr(timezone, "now", lambda: referral.created_at + timedelta(days=31))
    second(order_id=order.pk, actor=referrer, idempotency_key=uuid.uuid4())
    assert point_balance(referrer) == 0


@pytest.mark.django_db
@pytest.mark.parametrize("action", [record_store_payment, deliver_store_order])
def test_one_completion_condition_is_insufficient(order_factory, referral_pair, action):
    action(order_id=order_factory().pk, actor=referral_pair[0], idempotency_key=uuid.uuid4())
    assert point_balance(referral_pair[0]) == 0


@pytest.mark.django_db
@pytest.mark.parametrize("payment", ["unconfirmed", "received", "refund_required", "refunded"])
def test_cancelled_orders_never_earn_points(order_factory, referral_pair, payment):
    from referrals.store_rewards import evaluate_store_referral_reward

    order = order_factory()
    StoreOrder.objects.filter(pk=order.pk).update(
        status="cancelled",
        payment_status=payment,
        delivered_at=timezone.now(),
        payment_received_at=timezone.now(),
    )
    assert evaluate_store_referral_reward(order_id=order.pk) is None
    assert point_balance(referral_pair[0]) == 0


@pytest.mark.django_db
@pytest.mark.parametrize("target", [0, 1])
@pytest.mark.parametrize("status", [AccountStatus.SUSPENDED, AccountStatus.PENDING_VERIFICATION])
def test_inactive_accounts_do_not_earn_points(order_factory, referral_pair, target, status):
    from identity.services import transition_account

    transition_account(referral_pair[target], status)
    complete(order_factory(), referral_pair[0])
    assert point_balance(referral_pair[0]) == 0


@pytest.mark.django_db
@pytest.mark.parametrize(
    "case",
    [
        "guest",
        "missing",
        "late",
        "unmarked_referral",
        "unmarked_order",
        "disabled",
    ],
)
def test_legacy_and_unqualified_relations_are_skipped(order_factory, referral_pair, settings, case):
    from referrals.models import Referral
    from referrals.services import create_point_transaction

    referrer, buyer, referral = referral_pair
    old, _ = create_point_transaction(
        user=referrer, actor=referrer, direction="credit", amount=37, message="Legacy manual reward"
    )
    order = order_factory()
    if case == "guest":
        StoreOrder.objects.filter(pk=order.pk).update(buyer_user=None)
    elif case == "missing":
        referral.delete()
    elif case == "late":
        Referral.objects.filter(pk=referral.pk).update(
            created_at=order.created_at + timedelta(microseconds=1)
        )
    elif case == "unmarked_referral":
        Referral.objects.filter(pk=referral.pk).update(store_rewards_eligible=False)
    elif case == "unmarked_order":
        StoreOrder.objects.filter(pk=order.pk).update(referral_rewards_eligible=False)
    elif case == "disabled":
        settings.STORE_REFERRAL_REWARDS_ENABLED = False
    complete(order, referrer)
    assert point_balance(referrer) == 37
    old.refresh_from_db()
    assert old.message == "Legacy manual reward"
    assert PointTransaction.objects.filter(user=referrer).count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("fail_after_ledger", [False, True])
def test_ledger_failure_rolls_back_completion_and_reward(
    order_factory, referral_pair, monkeypatch, fail_after_ledger
):
    from commerce.models import StoreOrderAction
    from referrals import services
    from referrals.models import StoreReferralReward

    order = order_factory()
    record_store_payment(order_id=order.pk, actor=referral_pair[0], idempotency_key=uuid.uuid4())
    original = services._create_locked_point_transaction

    def fail(**kwargs):
        if fail_after_ledger:
            original(**kwargs)
        raise RuntimeError("injected storage failure")

    monkeypatch.setattr(services, "_create_locked_point_transaction", fail)
    key = uuid.uuid4()
    with pytest.raises(RuntimeError, match="injected storage failure"):
        deliver_store_order(order_id=order.pk, actor=referral_pair[0], idempotency_key=key)
    order.refresh_from_db()
    assert order.status == StoreOrderStatus.PENDING_DELIVERY
    assert order.delivered_at is None
    assert not StoreOrderAction.objects.filter(order=order, action="deliver").exists()
    assert not StoreReferralReward.objects.exists()
    assert point_balance(referral_pair[0]) == 0
    monkeypatch.setattr(services, "_create_locked_point_transaction", original)
    deliver_store_order(order_id=order.pk, actor=referral_pair[0], idempotency_key=key)
    assert point_balance(referral_pair[0]) == 100


@pytest.mark.django_db
def test_replay_same_keys_and_equal_completion_timestamps(
    order_factory, referral_pair, monkeypatch
):
    referrer = referral_pair[0]
    finish = timezone.now() + timedelta(seconds=1)
    monkeypatch.setattr(timezone, "now", lambda: finish)
    for _ in range(2):
        order = order_factory()
        for action in [record_store_payment, deliver_store_order]:
            key = uuid.uuid4()
            for _ in range(2):
                action(order_id=order.pk, actor=referrer, idempotency_key=key)
    assert point_balance(referrer) == 150


def make_invitee(referrer):
    buyer = User.objects.create_user(
        email=f"{uuid.uuid4().hex}@example.com", name="Invitee", status=AccountStatus.ACTIVE
    )
    assign_referrer(referee=buyer, code=referrer.referral_account.code)
    return buyer


@pytest.mark.django_db
def test_monthly_limit_bonus_and_timezone(order_factory, referral_pair, monkeypatch, settings):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from referrals.models import StoreReferralReward
    from referrals.store_rewards import evaluate_store_referral_reward

    settings.TIME_ZONE = "Asia/Ho_Chi_Minh"
    # 23:59 in Django's timezone, still October 31 in UTC.
    now = datetime(2026, 10, 31, 16, 59, tzinfo=ZoneInfo("UTC"))
    monkeypatch.setattr(timezone, "now", lambda: now)
    referrer = referral_pair[0]
    # Existing fixture relation is irrelevant here; create new relations at this test's clock.
    buyers = [make_invitee(referrer) for _ in range(11)]
    orders = [order_factory(buyer=buyer) for buyer in buyers]
    for order in orders:
        complete(order, referrer)
    assert point_balance(referrer) == 1000
    assert (
        StoreReferralReward.objects.filter(milestone=100, reward_month="2026-10-01").count() == 10
    )
    # The bonus remains available even after all ten first-milestone slots are used.
    complete(order_factory(buyer=buyers[0]), referrer)
    assert point_balance(referrer) == 1050
    now += timedelta(minutes=2)  # November locally, October in UTC.
    evaluate_store_referral_reward(order_id=orders[-1].pk)
    record_store_payment(order_id=orders[-1].pk, actor=referrer, idempotency_key=uuid.uuid4())
    assert point_balance(referrer) == 1050  # No carry-over of rejected order.
    complete(order_factory(buyer=buyers[-1]), referrer)
    assert point_balance(referrer) == 1150
    assert StoreReferralReward.objects.filter(milestone=100, reward_month="2026-11-01").count() == 1


@pytest.mark.django_db
def test_points_expose_reference_but_do_not_grant_buyer_order_access(
    order_factory, referral_pair, access_token
):
    from rest_framework.test import APIClient

    referrer, buyer, _ = referral_pair
    order = order_factory()
    complete(order, referrer)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.get("/api/v1/referrals/points")
    assert response.status_code == 200
    item = response.json()["data"][0]
    assert set(item) == {"id", "direction", "amount", "template", "message", "created_at"}
    assert item["amount"] == 100
    assert order.reference_code in item["message"]
    assert all(secret not in item["message"] for secret in [buyer.email, "Private", "+84901234567"])
    assert client.get(f"/api/v1/store-orders/{order.pk}").status_code == 404
    assert client.get(f"/api/v1/public/store-orders/{order.pk}").status_code == 404


@pytest.mark.django_db
def test_referrer_locale_controls_ledger_message(order_factory, referral_pair):
    referrer = referral_pair[0]
    referrer.locale = "en"
    referrer.save(update_fields=["locale"])
    order = order_factory()
    complete(order, referrer)
    item = PointTransaction.objects.get(user=referrer)
    assert item.message.startswith("Referral reward: first eligible order")
    assert order.reference_code in item.message


@pytest.mark.django_db
def test_activation_stamps_checkout_and_referral_only_while_enabled(
    store_product, payment_settings, default_supported_shipping_area, settings, reward_program
):
    from commerce.serializers import StoreCheckoutSerializer
    from commerce.store_orders import create_store_checkout
    from tests.test_store_orders import guest_payload

    referrer = User.objects.create_user(
        email="inviter@example.com", name="Inviter", status=AccountStatus.ACTIVE
    )
    for enabled in [False, True]:
        settings.STORE_REFERRAL_REWARDS_ENABLED = enabled
        buyer = make_invitee(referrer)
        assert buyer.referral_received.store_rewards_eligible is enabled
        payload = guest_payload(store_product, expected_available=10 if not enabled else 8)
        serializer = StoreCheckoutSerializer(data=payload)
        serializer.is_valid(raise_exception=True)
        order, _, _ = create_store_checkout(user=None, payload=serializer.validated_data)
        assert order.referral_rewards_eligible is enabled


@pytest.mark.django_db
def test_orders_completed_while_disabled_cannot_be_rewarded_on_reenable(
    order_factory, referral_pair, settings
):
    from referrals.store_rewards import evaluate_store_referral_reward

    order = order_factory()
    settings.STORE_REFERRAL_REWARDS_ENABLED = False
    complete(order, referral_pair[0])
    settings.STORE_REFERRAL_REWARDS_ENABLED = True
    evaluate_store_referral_reward(order_id=order.pk)
    assert point_balance(referral_pair[0]) == 0


@pytest.mark.django_db
def test_database_rejects_duplicate_milestone_and_order(order_factory, referral_pair):
    from django.db import IntegrityError, transaction

    from referrals.models import StoreReferralReward
    from referrals.services import create_point_transaction

    referrer = referral_pair[0]
    first_order, second_order = order_factory(), order_factory()
    complete(first_order, referrer)
    first = StoreReferralReward.objects.get(order=first_order)
    ledger, _ = create_point_transaction(
        user=referrer,
        actor=referrer,
        direction="credit",
        amount=1,
        message="Unrelated manual adjustment",
    )
    for order, milestone in [(second_order, 100), (first_order, 50)]:
        with pytest.raises(IntegrityError), transaction.atomic():
            StoreReferralReward.objects.create(
                referral=referral_pair[2],
                order=order,
                milestone=milestone,
                point_transaction=ledger,
                completed_at=first.completed_at,
                reward_month=first.reward_month,
            )
    assert StoreReferralReward.objects.count() == 1
    assert point_balance(referrer) == 101


@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("scenario", ["same_order", "two_orders", "monthly_cap"])
def test_mysql_concurrent_completion(order_factory, referral_pair, scenario):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections, connections

    from referrals.models import StoreReferralReward

    referrer, buyer, _ = referral_pair
    if scenario == "monthly_cap":
        for _ in range(9):
            complete(order_factory(buyer=make_invitee(referrer)), referrer)
        orders = [order_factory(buyer=make_invitee(referrer)) for _ in range(2)]
    else:
        orders = [order_factory() for _ in range(1 if scenario == "same_order" else 2)]
    for order in orders:
        record_store_payment(order_id=order.pk, actor=referrer, idempotency_key=uuid.uuid4())
    if scenario == "same_order":
        orders *= 2
    barrier = Barrier(2)
    key = uuid.uuid4()

    def deliver(order_id):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            deliver_store_order(order_id=order_id, actor=referrer, idempotency_key=key)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(deliver, order.pk) for order in orders]
        for future in futures:
            future.result(timeout=30)
    assert (
        point_balance(referrer)
        == {"same_order": 100, "two_orders": 150, "monthly_cap": 1000}[scenario]
    )
    assert StoreReferralReward.objects.filter(milestone=100).count() == (
        10 if scenario == "monthly_cap" else 1
    )


@pytest.mark.django_db
def test_existing_manual_ledger_is_not_inferred_or_modified(order_factory, referral_pair):
    referrer, _, referral = referral_pair
    old = PointTransaction.objects.create(
        user=referrer,
        direction="credit",
        amount=100,
        template="referral_successful",
        message="Old unverified manual credit",
        related_referral=referral,
    )
    complete(order_factory(), referrer)
    complete(order_factory(), referrer)
    assert point_balance(referrer) == 250
    old.refresh_from_db()
    assert old.message == "Old unverified manual credit"
    assert old.amount == 100


@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
@pytest.mark.django_db(transaction=True)
def test_mysql_completion_clock_runs_under_referrer_lock(order_factory, referral_pair, monkeypatch):
    """A blocked account must not let another order timestamp overtake its award."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from django.db import close_old_connections, connections, transaction

    from commerce import store_orders
    from referrals.models import ReferralAccount

    referrer = referral_pair[0]
    order = order_factory()
    record_store_payment(order_id=order.pk, actor=referrer, idempotency_key=uuid.uuid4())
    started, clock_read = Event(), Event()

    def now():
        clock_read.set()
        return timezone.now()

    monkeypatch.setattr(store_orders, "timezone", SimpleNamespace(now=now))

    def deliver():
        close_old_connections()
        try:
            started.set()
            deliver_store_order(order_id=order.pk, actor=referrer, idempotency_key=uuid.uuid4())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            ReferralAccount.objects.select_for_update().get(user=referrer)
            future = pool.submit(deliver)
            assert started.wait(timeout=5)
            premature_clock = clock_read.wait(timeout=1)
        future.result(timeout=15)
    assert not premature_clock
    assert clock_read.is_set()
    assert point_balance(referrer) == 100


@pytest.mark.django_db
def test_database_requires_milestone_for_awarded_ledger(order_factory, referral_pair):
    from django.db import IntegrityError, transaction

    from referrals.models import StoreReferralReward
    from referrals.services import create_point_transaction

    referrer, _, referral = referral_pair
    ledger, _ = create_point_transaction(
        user=referrer, actor=referrer, direction="credit", amount=100, message="Credit"
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        StoreReferralReward.objects.create(
            referral=referral,
            order=order_factory(),
            milestone=None,
            point_transaction=ledger,
            reward_month=timezone.localdate().replace(day=1),
            completed_at=timezone.now(),
            skip_reason="",
        )


@pytest.mark.skipif(connection.vendor != "mysql", reason="Requires real MySQL row locks")
@pytest.mark.django_db(transaction=True)
def test_mysql_delivery_and_redemption_rejection_do_not_deadlock(
    order_factory, referral_pair, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from django.db import close_old_connections, connections

    from commerce import store_orders
    from referrals import services
    from referrals.models import RedemptionStatus, StoreReferralReward
    from tests.test_redemptions import create_open_redemption, create_reward

    referrer, actor, _ = referral_pair
    redemption, _ = create_open_redemption(referrer, create_reward())
    order = order_factory()
    record_store_payment(order_id=order.pk, actor=actor, idempotency_key=uuid.uuid4())
    redemption_locked, account_locked = Event(), Event()
    original_redemption_lock = services._locked_redemption
    original_referrer_lock = store_orders.lock_store_referral_referrer

    def lock_redemption(redemption_id):
        item = original_redemption_lock(redemption_id)
        redemption_locked.set()
        assert account_locked.wait(timeout=10)
        return item

    def lock_referrer(order):
        relation = original_referrer_lock(order)
        assert relation is not None
        account_locked.set()
        return relation

    monkeypatch.setattr(services, "_locked_redemption", lock_redemption)
    monkeypatch.setattr(store_orders, "lock_store_referral_referrer", lock_referrer)

    def reject():
        close_old_connections()
        try:
            services.reject_redemption(
                redemption_id=redemption.pk, actor=actor, rejection_message="Unavailable"
            )
        finally:
            connections.close_all()

    def deliver():
        close_old_connections()
        try:
            assert redemption_locked.wait(timeout=10)
            deliver_store_order(order_id=order.pk, actor=actor, idempotency_key=uuid.uuid4())
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(reject), pool.submit(deliver)]
        for future in futures:
            future.result(timeout=30)
    redemption.refresh_from_db()
    order.refresh_from_db()
    assert redemption.status == RedemptionStatus.REJECTED
    assert order.status == StoreOrderStatus.DELIVERED
    assert point_balance(referrer) == 200
    assert StoreReferralReward.objects.get(order=order).milestone == 100
    assert PointTransaction.objects.filter(user=referrer, template="reward_refund").count() == 1
    assert (
        PointTransaction.objects.filter(user=referrer, template="referral_successful").count() == 1
    )
