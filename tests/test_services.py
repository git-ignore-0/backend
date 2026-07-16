import pytest
from django.db import IntegrityError, transaction

from identity.models import AccountStatus, IdentityEvent, User
from identity.services import InvalidAccountTransition, transition_account


@pytest.mark.django_db
def test_suspension_is_atomic_and_emits_event(active_user):
    updated = transition_account(active_user, AccountStatus.SUSPENDED)

    assert updated.status == AccountStatus.SUSPENDED
    assert updated.is_active is False
    assert IdentityEvent.objects.filter(
        user=active_user,
        type="user.suspended",
        status="pending",
    ).exists()


@pytest.mark.django_db
def test_repeating_same_transition_does_not_duplicate_event(active_user):
    transition_account(active_user, AccountStatus.SUSPENDED)
    transition_account(active_user, AccountStatus.SUSPENDED)

    assert (
        IdentityEvent.objects.filter(
            user=active_user,
            type="user.suspended",
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_unverified_identity_cannot_be_activated():
    user = User.objects.create_user(
        email="unverified@example.com",
        name="Unverified",
        password="StrongPassword12",
    )

    with pytest.raises(InvalidAccountTransition, match="Email verification"):
        transition_account(user, AccountStatus.ACTIVE)


@pytest.mark.django_db
def test_anonymized_identity_is_terminal(active_user):
    transition_account(active_user, AccountStatus.ANONYMIZED)

    with pytest.raises(InvalidAccountTransition, match="cannot change status"):
        transition_account(active_user, AccountStatus.ACTIVE)


@pytest.mark.django_db
def test_database_rejects_status_and_is_active_mismatch(active_user):
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.filter(pk=active_user.pk).update(
            status=AccountStatus.SUSPENDED,
            is_active=True,
        )
