from django.db import transaction

from identity.models import AccountStatus, IdentityEvent, User

EVENT_BY_STATUS = {
    AccountStatus.ACTIVE: "user.activated",
    AccountStatus.SUSPENDED: "user.suspended",
    AccountStatus.DELETION_REQUESTED: "user.deletion_requested",
    AccountStatus.ANONYMIZED: "user.anonymized",
}


class InvalidAccountTransition(ValueError):
    pass


@transaction.atomic
def transition_account(user: User, status: AccountStatus) -> User:
    try:
        status = AccountStatus(status)
    except ValueError as error:
        raise InvalidAccountTransition(f"Unknown account status: {status}") from error

    locked_user = User.objects.select_for_update().get(pk=user.pk)
    if locked_user.status == status:
        return locked_user
    if locked_user.status == AccountStatus.ANONYMIZED:
        raise InvalidAccountTransition("An anonymized identity cannot change status")
    if status == AccountStatus.ACTIVE and not locked_user.email_verified:
        raise InvalidAccountTransition("Email verification is required before activation")

    locked_user.status = status
    locked_user.is_active = status in {
        AccountStatus.PENDING_VERIFICATION,
        AccountStatus.ACTIVE,
    }
    locked_user.save(update_fields=["status", "is_active", "updated_at"])

    event_type = EVENT_BY_STATUS.get(status)
    if event_type:
        IdentityEvent.objects.create(
            user=locked_user,
            type=event_type,
            payload={"user_id": str(locked_user.pk), "status": status},
        )
    return locked_user
