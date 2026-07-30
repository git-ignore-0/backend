import secrets

from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Sum, When
from django.utils import timezone

from referrals.models import (
    PointDirection,
    PointTemplate,
    PointTransaction,
    RedemptionRequest,
    RedemptionStatus,
    Referral,
    ReferralAccount,
    Reward,
)

REFERRAL_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
REFERRAL_CODE_LENGTH = 10


class ReferralCodeInvalid(ValueError):
    pass


class ReferralAlreadyAssigned(ValueError):
    pass


class SelfReferral(ValueError):
    pass


class InvalidPointTransaction(ValueError):
    pass


class RedemptionError(ValueError):
    code = "invalid_redemption"


class RewardNotFound(RedemptionError):
    code = "reward_not_found"


class RewardUnavailable(RedemptionError):
    code = "reward_inactive"


class InsufficientRewardPoints(RedemptionError):
    code = "insufficient_points"


class IdempotencyConflict(RedemptionError):
    code = "idempotency_conflict"


class RedemptionTransitionConflict(RedemptionError):
    code = "redemption_transition_conflict"


class RewardReorderConflict(ValueError):
    pass


def normalize_referral_code(value: str) -> str:
    return value.strip().upper()


def generate_referral_code() -> str:
    return "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))


def ensure_referral_account(user) -> ReferralAccount:
    existing = ReferralAccount.objects.filter(user=user).first()
    if existing:
        return existing
    for _attempt in range(20):
        try:
            with transaction.atomic():
                return ReferralAccount.objects.create(user=user, code=generate_referral_code())
        except IntegrityError:
            existing = ReferralAccount.objects.filter(user=user).first()
            if existing:
                return existing
    raise RuntimeError("Could not allocate a unique referral code")


def referral_code_exists(code: str) -> bool:
    return ReferralAccount.objects.filter(code=normalize_referral_code(code)).exists()


@transaction.atomic
def assign_referrer(*, referee, code: str) -> Referral:
    locked_referee_account = ReferralAccount.objects.select_for_update().get(user=referee)
    if Referral.objects.filter(referee=referee).exists():
        raise ReferralAlreadyAssigned("A referrer has already been assigned")
    try:
        referrer_account = ReferralAccount.objects.select_related("user").get(
            code=normalize_referral_code(code)
        )
    except ReferralAccount.DoesNotExist as error:
        raise ReferralCodeInvalid("Referral code is invalid") from error
    if referrer_account.user_id == locked_referee_account.user_id:
        raise SelfReferral("Self-referral is not allowed")
    try:
        return Referral.objects.create(referrer=referrer_account.user, referee=referee)
    except IntegrityError as error:
        raise ReferralAlreadyAssigned("A referrer has already been assigned") from error


def point_balance(user) -> int:
    result = user.point_transactions.aggregate(
        balance=Sum(
            Case(
                When(direction=PointDirection.CREDIT, then=F("amount")),
                When(direction=PointDirection.DEBIT, then=-F("amount")),
                default=0,
                output_field=IntegerField(),
            )
        )
    )["balance"]
    return result or 0


@transaction.atomic
def reorder_rewards(*, page: int, reward_ids: list, page_size: int = 30) -> None:
    rewards = list(Reward.objects.select_for_update().order_by("position", "created_at", "id"))
    start = (page - 1) * page_size
    current_page = rewards[start : start + page_size]
    if not current_page or {item.pk for item in current_page} != set(reward_ids):
        raise RewardReorderConflict("Reward order changed; reload the page and try again")

    by_id = {item.pk: item for item in current_page}
    rewards[start : start + page_size] = [by_id[reward_id] for reward_id in reward_ids]
    updated_at = timezone.now()
    for position, reward in enumerate(rewards):
        reward.position = position
        reward.updated_at = updated_at
    Reward.objects.bulk_update(rewards, ["position", "updated_at"])


def _create_locked_point_transaction(
    *,
    user,
    actor,
    direction: PointDirection,
    amount: int,
    template: PointTemplate,
    message: str,
    internal_note: str = "",
    related_referral=None,
) -> PointTransaction:
    return PointTransaction.objects.create(
        user=user,
        actor=actor,
        direction=direction,
        amount=amount,
        template=template,
        message=message.strip()[:240],
        internal_note=internal_note,
        related_referral=related_referral,
    )


@transaction.atomic
def create_point_transaction(
    *,
    user,
    actor,
    direction: str,
    amount: int,
    message: str,
    related_referral=None,
) -> tuple[PointTransaction, int]:
    try:
        direction = PointDirection(direction)
    except ValueError as error:
        raise InvalidPointTransaction("Invalid direction") from error
    if amount <= 0:
        raise InvalidPointTransaction("Amount must be positive")
    if not message.strip():
        raise InvalidPointTransaction("Message is required")

    if related_referral and user.pk not in {
        related_referral.referrer_id,
        related_referral.referee_id,
    }:
        raise InvalidPointTransaction("Related referral must involve the target user")

    ReferralAccount.objects.select_for_update().get(user=user)
    balance = point_balance(user)
    if direction == PointDirection.DEBIT and amount > balance:
        raise InvalidPointTransaction("Insufficient point balance")

    item = _create_locked_point_transaction(
        user=user,
        actor=actor,
        direction=direction,
        amount=amount,
        template=PointTemplate.MANUAL_ADJUSTMENT,
        message=message,
        related_referral=related_referral,
    )
    updated_balance = balance + (amount if direction == PointDirection.CREDIT else -amount)
    return item, updated_balance


def _reward_snapshot_names(reward: Reward) -> tuple[str, str]:
    by_locale = {item.locale: item.name.strip() for item in reward.translations.all()}
    name_vi = by_locale.get("vi") or by_locale.get("en")
    name_en = by_locale.get("en") or by_locale.get("vi")
    if not name_vi or not name_en:
        raise RewardUnavailable("Reward translations are incomplete")
    return name_vi, name_en


def _redemption_message(redemption: RedemptionRequest, *, refund: bool) -> str:
    if redemption.user.locale == "en":
        prefix = "Reward refund" if refund else "Reward redemption"
        name = redemption.reward_name_en_snapshot
    else:
        prefix = "Hoàn điểm đổi quà" if refund else "Đổi quà"
        name = redemption.reward_name_vi_snapshot
    return f"{prefix}: {name}"


@transaction.atomic
def create_redemption(
    *,
    user,
    reward_id,
    idempotency_key,
) -> tuple[RedemptionRequest, int, bool]:
    ReferralAccount.objects.select_for_update().get(user=user)
    existing = RedemptionRequest.objects.filter(user=user, idempotency_key=idempotency_key).first()
    if existing:
        if existing.reward_id != reward_id:
            raise IdempotencyConflict("Idempotency key was already used for another reward")
        return existing, point_balance(user), False

    try:
        reward = (
            Reward.objects.select_for_update()
            .prefetch_related("translations")
            .get(pk=reward_id)
        )
    except Reward.DoesNotExist as error:
        raise RewardNotFound("Reward does not exist") from error
    if not reward.is_active:
        raise RewardUnavailable("Reward is inactive")

    balance = point_balance(user)
    if reward.point_cost > balance:
        raise InsufficientRewardPoints("Insufficient point balance")

    name_vi, name_en = _reward_snapshot_names(reward)
    redemption = RedemptionRequest.objects.create(
        user=user,
        reward=reward,
        reward_name_vi_snapshot=name_vi,
        reward_name_en_snapshot=name_en,
        point_cost_snapshot=reward.point_cost,
        idempotency_key=idempotency_key,
    )
    debit = _create_locked_point_transaction(
        user=user,
        actor=user,
        direction=PointDirection.DEBIT,
        amount=redemption.point_cost_snapshot,
        template=PointTemplate.REWARD_REDEMPTION,
        message=_redemption_message(redemption, refund=False),
    )
    redemption.debit_transaction = debit
    redemption.save(update_fields=["debit_transaction", "updated_at"])
    return redemption, balance - redemption.point_cost_snapshot, True


def _locked_redemption(redemption_id) -> RedemptionRequest:
    return (
        RedemptionRequest.objects.select_for_update()
        .select_related("user", "debit_transaction", "refund_transaction")
        .get(pk=redemption_id)
    )


def _transaction_matches(
    transaction_item: PointTransaction,
    *,
    user_id,
    direction: PointDirection,
    amount: int,
    template: PointTemplate,
) -> bool:
    return (
        transaction_item.user_id == user_id
        and transaction_item.direction == direction
        and transaction_item.amount == amount
        and transaction_item.template == template
    )


def _validate_redemption_integrity(redemption: RedemptionRequest) -> None:
    if not redemption.debit_transaction_id or not _transaction_matches(
        redemption.debit_transaction,
        user_id=redemption.user_id,
        direction=PointDirection.DEBIT,
        amount=redemption.point_cost_snapshot,
        template=PointTemplate.REWARD_REDEMPTION,
    ):
        raise RedemptionTransitionConflict("Redemption debit transaction is invalid")

    if redemption.status == RedemptionStatus.REJECTED and (
        not redemption.refund_transaction_id
        or not _transaction_matches(
            redemption.refund_transaction,
            user_id=redemption.user_id,
            direction=PointDirection.CREDIT,
            amount=redemption.point_cost_snapshot,
            template=PointTemplate.REWARD_REFUND,
        )
    ):
        raise RedemptionTransitionConflict("Redemption refund transaction is invalid")


def _save_transition(redemption, update_fields, internal_note):
    if internal_note is not None:
        redemption.internal_note = internal_note.strip()
        update_fields.append("internal_note")
    redemption.save(update_fields=[*update_fields, "updated_at"])


@transaction.atomic
def mark_redemption_contacted(
    *,
    redemption_id,
    actor,
    internal_note: str | None = None,
) -> RedemptionRequest:
    redemption = _locked_redemption(redemption_id)
    _validate_redemption_integrity(redemption)
    if redemption.status != RedemptionStatus.PENDING:
        raise RedemptionTransitionConflict("Only pending redemptions can be marked contacted")
    redemption.status = RedemptionStatus.CONTACTED
    redemption.contacted_by = actor
    redemption.contacted_at = timezone.now()
    _save_transition(redemption, ["status", "contacted_by", "contacted_at"], internal_note)
    return redemption


@transaction.atomic
def complete_redemption(
    *,
    redemption_id,
    actor,
    internal_note: str | None = None,
) -> RedemptionRequest:
    redemption = _locked_redemption(redemption_id)
    _validate_redemption_integrity(redemption)
    if redemption.status not in {RedemptionStatus.PENDING, RedemptionStatus.CONTACTED}:
        raise RedemptionTransitionConflict("Only open redemptions can be completed")
    redemption.status = RedemptionStatus.COMPLETED
    redemption.processed_by = actor
    redemption.completed_at = timezone.now()
    _save_transition(redemption, ["status", "processed_by", "completed_at"], internal_note)
    return redemption


@transaction.atomic
def reject_redemption(
    *,
    redemption_id,
    actor,
    rejection_message: str,
    internal_note: str | None = None,
) -> tuple[RedemptionRequest, int]:
    if not rejection_message.strip():
        raise RedemptionTransitionConflict("A rejection message is required")
    redemption = _locked_redemption(redemption_id)
    _validate_redemption_integrity(redemption)
    ReferralAccount.objects.select_for_update().get(user=redemption.user)
    if redemption.status not in {RedemptionStatus.PENDING, RedemptionStatus.CONTACTED}:
        raise RedemptionTransitionConflict("Only open redemptions can be rejected")
    if redemption.refund_transaction_id is not None:
        raise RedemptionTransitionConflict("Redemption was already refunded")

    balance = point_balance(redemption.user)
    refund = _create_locked_point_transaction(
        user=redemption.user,
        actor=actor,
        direction=PointDirection.CREDIT,
        amount=redemption.point_cost_snapshot,
        template=PointTemplate.REWARD_REFUND,
        message=_redemption_message(redemption, refund=True),
    )
    redemption.refund_transaction = refund
    redemption.status = RedemptionStatus.REJECTED
    redemption.rejection_message = rejection_message.strip()
    redemption.processed_by = actor
    redemption.rejected_at = timezone.now()
    update_fields = [
        "refund_transaction",
        "status",
        "rejection_message",
        "processed_by",
        "rejected_at",
    ]
    _save_transition(
        redemption,
        update_fields,
        internal_note,
    )
    return redemption, balance + redemption.point_cost_snapshot
