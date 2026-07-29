import secrets

from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Sum, When

from referrals.models import (
    PointDirection,
    PointTemplate,
    PointTransaction,
    Referral,
    ReferralAccount,
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

    item = PointTransaction.objects.create(
        user=user,
        actor=actor,
        direction=direction,
        amount=amount,
        template=PointTemplate.MANUAL_ADJUSTMENT,
        message=message.strip(),
        internal_note="",
        related_referral=related_referral,
    )
    updated_balance = balance + (amount if direction == PointDirection.CREDIT else -amount)
    return item, updated_balance
