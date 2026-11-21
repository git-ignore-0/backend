"""Transactional account management for CMS administrators."""

from time import sleep

from allauth.account.models import EmailAddress
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, OperationalError, connection, transaction
from django.db.models import Q

from identity.models import AccountStatus, AuditLog, User
from identity.passwords import revoke_core_credentials
from identity.services import activate_account, transition_account


class AccountConflict(Exception):
    pass


def _lock_accounts(*, actor, user_id=None):
    ids = {actor.pk} if user_id is None else {actor.pk, user_id}
    accounts = {
        user.pk: user for user in User.objects.select_for_update().filter(pk__in=ids).order_by("pk")
    }
    current_actor = accounts.get(actor.pk)
    if (
        current_actor is None
        or not current_actor.can_access_core()
        or not current_actor.groups.filter(name="website_admin").exists()
    ):
        raise PermissionDenied("Website administrator access is required.")
    if user_id is not None and user_id not in accounts:
        raise User.DoesNotExist
    return current_actor, accounts.get(user_id)


def _audit(user, action, actor):
    AuditLog.objects.create(user=user, action=action, context={"actor_user_id": str(actor.pk)})


def _duplicate_identifiers(*, email, phone):
    errors = {}
    if User.objects.filter(login_phone_number=phone).exists():
        errors["login_phone_number"] = "This identifier is already in use."
    if email and (
        User.objects.filter(email__iexact=email).exists()
        or EmailAddress.objects.filter(email__iexact=email).exists()
    ):
        errors["email"] = "This identifier is already in use."
    return errors


def _validate_password(password, user):
    try:
        validate_password(password, user=user)
    except ValidationError as error:
        raise ValidationError({"password1": error.messages}) from error


def create_account(*, actor, name, login_phone_number, password, email=None, locale=None):
    fields = {"name": name, "login_phone_number": login_phone_number, "email": email}
    if locale is not None:
        fields["locale"] = locale

    for attempt in range(5):
        try:
            with transaction.atomic():
                current_actor, _ = _lock_accounts(actor=actor)
                duplicates = _duplicate_identifiers(email=email, phone=login_phone_number)
                if duplicates:
                    raise ValidationError(duplicates)
                _validate_password(password, User(**fields))
                user = User.objects.create_user(password=password, **fields)
                if email:
                    EmailAddress.objects.create(
                        user=user, email=email, primary=True, verified=False
                    )
                _audit(user, "user.created", current_actor)
                return user
        except IntegrityError as error:
            # Look up the winner after rollback. Do not mask unrelated write failures.
            duplicates = _duplicate_identifiers(email=email, phone=login_phone_number)
            if not duplicates:
                raise
            raise ValidationError(duplicates) from error
        except OperationalError as error:
            # SQLite cannot upgrade concurrent read transactions to writers. Retry
            # the entire operation after rollback so the next read sees the winner.
            sqlite_code = getattr(error.__cause__, "sqlite_errorcode", 0)
            if connection.vendor != "sqlite" or sqlite_code & 0xFF not in {5, 6} or attempt == 4:
                raise
            sleep(0.025 * (attempt + 1))


def _protect_target(user, actor):
    if user.pk == actor.pk or user.is_superuser:
        raise AccountConflict("account_target_protected")


STATE_ACTIONS = {
    "activate": (AccountStatus.PENDING_ACTIVATION, AccountStatus.ACTIVE),
    "suspend": (AccountStatus.ACTIVE, AccountStatus.SUSPENDED),
    "reactivate": (AccountStatus.SUSPENDED, AccountStatus.ACTIVE),
}


@transaction.atomic
def change_account_state(*, actor, user_id, action):
    current_actor, user = _lock_accounts(actor=actor, user_id=user_id)
    if action == "suspend":
        _protect_target(user, current_actor)
    start, end = STATE_ACTIONS[action]
    if user.status == end:
        return user
    if user.status != start:
        raise AccountConflict("account_state_conflict")
    if action == "activate":
        return activate_account(user=user, source="admin", actor=current_actor)
    if action == "reactivate":
        return transition_account(user, end, source="admin", actor=current_actor)
    user = transition_account(user, end)
    _audit(user, "user.suspended", current_actor)
    return user


@transaction.atomic
def reset_account_password(*, actor, user_id, password):
    current_actor, user = _lock_accounts(actor=actor, user_id=user_id)
    _protect_target(user, current_actor)
    # Inspect grants directly: Django returns no effective permissions for inactive users.
    if (
        user.is_staff
        or user.user_permissions.exists()
        or user.groups.filter(Q(name="website_admin") | Q(permissions__isnull=False)).exists()
    ):
        raise AccountConflict("account_target_protected")
    if user.status not in {
        AccountStatus.ACTIVE,
        AccountStatus.PENDING_ACTIVATION,
        AccountStatus.SUSPENDED,
    }:
        raise AccountConflict("account_state_conflict")
    if user.email_verified:
        raise AccountConflict("email_recovery_required")
    _validate_password(password, user)
    user.set_password(password)
    user.save(update_fields=["password", "updated_at"])
    revoke_core_credentials(user=user)
    _audit(user, "user.password_reset", current_actor)
    return user
