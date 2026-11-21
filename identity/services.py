from django.db import transaction

from identity.models import AccountStatus, AuditLog, IdentityEvent, User

EVENT_BY_STATUS = {
    AccountStatus.ACTIVE: "user.activated",
    AccountStatus.SUSPENDED: "user.suspended",
    AccountStatus.DELETION_REQUESTED: "user.deletion_requested",
    AccountStatus.ANONYMIZED: "user.anonymized",
}


class InvalidAccountTransition(ValueError):
    pass


def _activation_evidence(user: User, source: str, actor: User | None) -> dict:
    if source == "email_verified":
        if not user.email_verified:
            raise InvalidAccountTransition("Email verification is required before activation")
    elif source == "admin":
        # Reload persisted privileges: is_staff alone only grants Admin site access.
        admin = User.objects.filter(pk=actor.pk).first() if actor is not None else None
        if (
            admin is None
            or not admin.can_access_core()
            or not (admin.is_superuser or admin.groups.filter(name="website_admin").exists())
        ):
            raise InvalidAccountTransition("An authorized admin actor is required")
    else:
        raise InvalidAccountTransition("Unknown activation source")
    context = {"source": source}
    if actor is not None:
        context["actor_user_id"] = str(actor.pk)
    return context


def _write_transition(user: User, status: AccountStatus, *, activation_context=None) -> User:
    user.status = status
    user.is_active = status in {AccountStatus.PENDING_ACTIVATION, AccountStatus.ACTIVE}
    user.save(update_fields=["status", "is_active", "updated_at"])
    event_type = EVENT_BY_STATUS.get(status)
    if event_type:
        IdentityEvent.objects.create(
            user=user,
            type=event_type,
            payload={"user_id": str(user.pk), "status": status},
        )
    if activation_context is not None:
        AuditLog.objects.create(user=user, action="user.activated", context=activation_context)
    return user


def _activate_locked(user: User, *, source: str, actor: User | None, reactivate=False) -> User:
    context = _activation_evidence(user, source, actor)
    if user.status == AccountStatus.ACTIVE:
        return user
    if user.status != AccountStatus.PENDING_ACTIVATION:
        if reactivate and user.status == AccountStatus.SUSPENDED and source == "admin":
            return _write_transition(user, AccountStatus.ACTIVE, activation_context=context)
        # Email confirmation is harmless on locked/deleted identities, including stale signals.
        if source == "email_verified" and not reactivate:
            return user
        raise InvalidAccountTransition("Only pending accounts can be activated")
    return _write_transition(user, AccountStatus.ACTIVE, activation_context=context)


@transaction.atomic
def activate_account(*, user: User, source: str, actor: User | None = None) -> User:
    locked_user = User.objects.select_for_update().get(pk=user.pk)
    return _activate_locked(locked_user, source=source, actor=actor)


@transaction.atomic
def transition_account(
    user: User, status: AccountStatus, *, source: str | None = None, actor: User | None = None
) -> User:
    try:
        status = AccountStatus(status)
    except ValueError as error:
        raise InvalidAccountTransition(f"Unknown account status: {status}") from error

    locked_user = User.objects.select_for_update().get(pk=user.pk)
    if locked_user.status == AccountStatus.ANONYMIZED and status != locked_user.status:
        raise InvalidAccountTransition("An anonymized identity cannot change status")
    if locked_user.status == status and source is None and actor is None:
        return locked_user
    if status == AccountStatus.ACTIVE:
        # Legacy pending+verified callers still work; suspended requires explicit admin evidence.
        return _activate_locked(
            locked_user,
            source=source if source is not None else "email_verified",
            actor=actor,
            reactivate=True,
        )
    if locked_user.status == status:
        return locked_user
    if (
        locked_user.status == AccountStatus.DELETION_REQUESTED
        and status != AccountStatus.ANONYMIZED
    ):
        raise InvalidAccountTransition("A deletion-requested identity can only be anonymized")
    if locked_user.status == AccountStatus.SUSPENDED and status == AccountStatus.PENDING_ACTIVATION:
        raise InvalidAccountTransition("Only an admin can unlock a suspended identity")
    return _write_transition(locked_user, status)
