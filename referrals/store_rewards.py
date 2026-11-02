"""Store referral rewards. Lock order: StoreOrder, then referrer's ReferralAccount.

No inventory, referee account, reward, or identity row locks are taken here.
The account lock is the existing ledger serialization point (also used by manual
adjustments and redemptions). Call only from a completion transition, never a read.
"""

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from commerce.models import StoreOrder, StoreOrderStatus, StorePaymentStatus
from identity.models import AccountStatus, User
from referrals.models import (
    PointDirection,
    PointTemplate,
    Referral,
    ReferralAccount,
    StoreReferralReward,
)


def store_referral_rewards_active():
    return settings.STORE_REFERRAL_REWARDS_ENABLED


def lock_store_referral_referrer(order):
    """Caller owns the order lock and transaction; serialize before completion time.

    Evaluating only after stamping time permits concurrent orders to acquire the
    account lock in reverse completion order and lose the second milestone.
    """
    if not store_referral_rewards_active():
        return None
    if not order.referral_rewards_eligible or order.buyer_user_id is None:
        return None
    referral = Referral.objects.filter(referee_id=order.buyer_user_id).first()
    if (
        referral is None
        or not referral.store_rewards_eligible
        or referral.created_at > order.created_at
    ):
        return None

    ReferralAccount.objects.select_for_update().get(user_id=referral.referrer_id)
    return referral


@transaction.atomic
def evaluate_store_referral_reward(*, order_id):
    # Re-lock for safe direct calls; commerce already owns this lock.
    order = StoreOrder.objects.select_for_update().get(pk=order_id)
    if (
        order.status != StoreOrderStatus.DELIVERED
        or order.payment_status != StorePaymentStatus.RECEIVED
        or order.delivered_at is None
        or order.payment_received_at is None
    ):
        return None
    if not store_referral_rewards_active():
        # Completion while disabled is terminal for automation, even after re-enabling.
        if order.referral_rewards_eligible:
            order.referral_rewards_eligible = False
            order.save(update_fields=["referral_rewards_eligible"])
        return None
    referral = lock_store_referral_referrer(order)
    if referral is None:
        return None
    existing = StoreReferralReward.objects.filter(order=order).first()
    if existing:
        return existing
    completed_at = max(order.delivered_at, order.payment_received_at)
    awards = StoreReferralReward.objects.filter(referral=referral, milestone__isnull=False)
    first = awards.filter(milestone=100).first()
    reason = ""
    milestone = 50 if first else 100
    month = timezone.localtime(completed_at, timezone.get_default_timezone()).date().replace(day=1)
    users = list(User.objects.filter(pk__in=[referral.referrer_id, referral.referee_id]))
    if any(not user.is_active or user.status != AccountStatus.ACTIVE for user in users):
        reason = "inactive_account"
    elif order.subtotal_vnd < 300_000:
        # subtotal_vnd is the sum of cash_subtotal_vnd line snapshots, excluding CSA and ship.
        reason = "cash_below_threshold"
    elif awards.filter(milestone=50).exists():
        reason = "milestones_complete"
    elif completed_at > referral.created_at + timedelta(days=60 if first else 30):
        reason = "deadline_passed"
    elif first and completed_at < first.completed_at:
        reason = "not_subsequent_order"
    elif (
        not first
        and StoreReferralReward.objects.filter(
            referral__referrer_id=referral.referrer_id, milestone=100, reward_month=month
        ).count()
        >= 10
    ):
        reason = "monthly_limit"

    decision = StoreReferralReward.objects.create(
        referral=referral,
        order=order,
        completed_at=completed_at,
        skip_reason=reason or "pending",
    )
    if reason:
        return decision

    from referrals.services import _create_locked_point_transaction

    referrer = next(user for user in users if user.pk == referral.referrer_id)
    if referrer.locale == "en":
        ordinal = "first" if milestone == 100 else "second"
        message = f"Referral reward: {ordinal} eligible order — Order {order.reference_code}"
    else:
        ordinal = "đầu tiên" if milestone == 100 else "thứ hai"
        message = f"Thưởng giới thiệu: đơn {ordinal} đủ điều kiện — Mã đơn {order.reference_code}"
    ledger = _create_locked_point_transaction(
        user=referrer,
        actor=None,
        direction=PointDirection.CREDIT,
        amount=milestone,
        template=PointTemplate.REFERRAL_SUCCESSFUL
        if milestone == 100
        else PointTemplate.REFERRAL_BONUS,
        message=message,
        related_referral=referral,
    )
    decision.skip_reason = ""
    decision.milestone = milestone
    decision.point_transaction = ledger
    decision.reward_month = month
    decision.save(update_fields=["milestone", "point_transaction", "reward_month", "skip_reason"])
    return decision
