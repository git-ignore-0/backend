import re

import pytest
from allauth.account.models import EmailAddress
from allauth.account.signals import email_confirmed

from identity.models import AccountStatus, IdentityEvent, User


@pytest.mark.django_db
def test_registration_creates_pending_uuid_identity_and_sends_verification(client, mailoutbox):
    response = client.post(
        "/register",
        {
            "name": "  Nông Dân Xanh  ",
            "email": "HELLO@example.com",
            "locale": "vi",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
        },
    )

    user = User.objects.get()
    assert response.status_code == 302
    assert user.name == "Nông Dân Xanh"
    assert user.email == "hello@example.com"
    assert user.status == AccountStatus.PENDING_VERIFICATION
    assert re.fullmatch(r"[0-9a-f-]{36}", str(user.id))
    assert EmailAddress.objects.filter(user=user, email=user.email, verified=False).exists()
    assert len(mailoutbox) == 1


@pytest.mark.django_db
def test_confirmed_email_activates_identity_and_creates_outbox_event():
    user = User.objects.create_user(
        email="pending@example.com",
        name="Pending User",
        password="StrongPassword12",
    )
    email = EmailAddress.objects.create(
        user=user,
        email=user.email,
        primary=True,
        verified=True,
    )

    email_confirmed.send(sender=EmailAddress, request=None, email_address=email)

    user.refresh_from_db()
    assert user.status == AccountStatus.ACTIVE
    assert IdentityEvent.objects.filter(
        user=user,
        type="user.activated",
        status="pending",
    ).exists()
