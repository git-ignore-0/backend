import pytest

from identity.models import AccountStatus, IdentityEvent
from identity.services import transition_account


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
