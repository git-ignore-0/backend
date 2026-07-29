import importlib

import pytest
from allauth.account.models import EmailAddress
from django.apps import apps
from django.contrib.auth.models import Group
from django.utils import timezone
from oauth2_provider.models import AccessToken
from rest_framework.test import APIClient

from identity.models import AccountStatus, User
from members.models import MemberProfile
from referrals.models import PointTransaction, Referral, ReferralAccount
from referrals.services import (
    InvalidPointTransaction,
    ReferralAlreadyAssigned,
    ReferralCodeInvalid,
    SelfReferral,
    assign_referrer,
    create_point_transaction,
    point_balance,
)


def create_active_user(email, name="Member", phone=""):
    user = User.objects.create_user(
        email=email,
        name=name,
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    if phone:
        MemberProfile.objects.create(user=user, phone_number=phone)
    return user


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture
def admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


@pytest.mark.django_db
def test_new_users_receive_unique_referral_codes():
    first = create_active_user("first@example.com")
    second = create_active_user("second@example.com")

    assert len(first.referral_account.code) == 10
    assert first.referral_account.code != second.referral_account.code


@pytest.mark.django_db
def test_backfill_allocates_code_for_existing_user():
    user = create_active_user("legacy@example.com")
    ReferralAccount.objects.filter(user=user).delete()
    migration = importlib.import_module("referrals.migrations.0001_initial")

    migration.backfill_referral_accounts(apps, None)

    assert ReferralAccount.objects.filter(user=user, code__isnull=False).exists()


@pytest.mark.django_db
def test_signup_referral_code_is_optional_and_valid_code_links_once(client):
    referrer = create_active_user("referrer@example.com", "Referrer")
    payload = {
        "name": "New Member",
        "email": "new@example.com",
        "password1": "StrongPassword12",
        "password2": "StrongPassword12",
    }
    response = client.post("/register", payload)
    assert response.status_code == 302
    assert not Referral.objects.filter(referee__email="new@example.com").exists()

    payload["email"] = "referred@example.com"
    payload["referral_code"] = referrer.referral_account.code.lower()
    response = client.post("/register", payload)
    assert response.status_code == 302
    assert Referral.objects.filter(referrer=referrer, referee__email=payload["email"]).exists()


@pytest.mark.django_db
def test_invalid_signup_code_does_not_create_user(client):
    response = client.post(
        "/register",
        {
            "name": "New Member",
            "email": "invalid-code@example.com",
            "password1": "StrongPassword12",
            "password2": "StrongPassword12",
            "referral_code": "NOT-A-CODE",
        },
    )
    assert response.status_code == 200
    assert not User.objects.filter(email="invalid-code@example.com").exists()


@pytest.mark.django_db
def test_delayed_assignment_rejects_invalid_self_and_second_codes():
    first = create_active_user("first@example.com")
    second = create_active_user("second@example.com")
    referee = create_active_user("referee@example.com")

    with pytest.raises(ReferralCodeInvalid):
        assign_referrer(referee=referee, code="INVALID")
    assert not Referral.objects.filter(referee=referee).exists()
    with pytest.raises(SelfReferral):
        assign_referrer(referee=referee, code=referee.referral_account.code)

    relation = assign_referrer(referee=referee, code=first.referral_account.code)
    assert relation.referrer == first
    with pytest.raises(ReferralAlreadyAssigned):
        assign_referrer(referee=referee, code=second.referral_account.code)
    assert Referral.objects.get(referee=referee).referrer == first


@pytest.mark.django_db
def test_self_service_is_isolated_and_invitees_do_not_leak_private_data(
    access_token, active_user, oauth_application
):
    invitee = create_active_user("private@example.com", "Safe Name", "+84 900 000 000")
    assign_referrer(referee=invitee, code=active_user.referral_account.code)
    other = create_active_user("other@example.com")
    other_token = AccessToken.objects.create(
        user=other,
        application=oauth_application,
        token="other-user-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )

    response = bearer_client(access_token).get("/api/v1/referrals/invited-users")
    body = response.json()
    assert response.status_code == 200
    assert body["meta"]["page_size"] == 30
    assert body["data"][0]["name"] == "Safe Name"
    assert "email" not in body["data"][0]
    assert "phone" not in body["data"][0]
    assert bearer_client(other_token).get("/api/v1/referrals/invited-users").json()["data"] == []


@pytest.mark.django_db
def test_self_service_submit_code_and_pagination(access_token, active_user):
    referrer = create_active_user("referrer@example.com", "Referrer")
    client = bearer_client(access_token)

    invalid = client.post("/api/v1/referrals/submit-code", {"code": "INVALID"}, format="json")
    assert invalid.status_code == 400
    assert client.get("/api/v1/referrals/account").json()["data"]["can_submit_referral_code"]

    success = client.post(
        "/api/v1/referrals/submit-code",
        {"code": referrer.referral_account.code},
        format="json",
    )
    assert success.status_code == 200
    assert success.json()["data"]["referrer"]["name"] == "Referrer"
    assert success["Cache-Control"] == "private, no-store"


@pytest.mark.django_db
def test_self_service_requires_bearer_token_and_profile_email_scopes(
    client, active_user, oauth_application
):
    assert client.get("/api/v1/referrals/account").status_code == 401
    token = AccessToken.objects.create(
        user=active_user,
        application=oauth_application,
        token="profile-only-referral-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile",
    )
    assert bearer_client(token).get("/api/v1/referrals/account").status_code == 403


@pytest.mark.django_db
def test_points_start_at_zero_credit_and_debit(active_user):
    actor = create_active_user("admin@example.com")
    assert point_balance(active_user) == 0

    _credit, balance = create_point_transaction(
        user=active_user,
        actor=actor,
        direction="credit",
        amount=100,
        message="Giới thiệu thành công",
    )
    assert balance == 100
    debit, balance = create_point_transaction(
        user=active_user,
        actor=actor,
        direction="debit",
        amount=100,
        message="Đổi rau củ theo mùa",
    )
    assert debit.message == "Đổi rau củ theo mùa"
    assert balance == 0
    assert point_balance(active_user) == 0


@pytest.mark.django_db
def test_debit_at_zero_or_over_balance_is_rejected(active_user):
    actor = create_active_user("admin@example.com")
    with pytest.raises(InvalidPointTransaction, match="Insufficient"):
        create_point_transaction(
            user=active_user,
            actor=actor,
            direction="debit",
            amount=100,
            message="Đổi rau củ theo mùa",
        )
    assert not PointTransaction.objects.filter(user=active_user).exists()

    create_point_transaction(
        user=active_user,
        actor=actor,
        direction="credit",
        amount=50,
        message="Thưởng giới thiệu",
    )
    with pytest.raises(InvalidPointTransaction, match="Insufficient"):
        create_point_transaction(
            user=active_user,
            actor=actor,
            direction="debit",
            amount=100,
            message="Đổi rau củ theo mùa",
        )
    assert point_balance(active_user) == 50


@pytest.mark.django_db
def test_point_transaction_requires_message(active_user):
    actor = create_active_user("admin@example.com")
    with pytest.raises(InvalidPointTransaction, match="Message"):
        create_point_transaction(
            user=active_user,
            actor=actor,
            direction="credit",
            amount=25,
            message=" ",
        )


@pytest.mark.django_db
def test_admin_authorization_search_pagination_detail_and_point_creation(admin_api):
    target = create_active_user("search-target@example.com", "Target", "+84 912 345 678")
    for index in range(31):
        create_active_user(f"page-{index:02d}@example.com")

    searched = admin_api.get("/api/v1/admin/users?search=912%20345&page=1")
    assert searched.status_code == 200
    assert searched.json()["data"][0]["id"] == str(target.pk)
    assert searched.json()["meta"]["page_size"] == 30
    assert admin_api.get("/api/v1/admin/users?page=2").json()["data"]

    detail = admin_api.get(f"/api/v1/admin/users/{target.pk}").json()["data"]
    assert detail["source"] == "organic"
    assert detail["points_balance"] == 0
    assert "password" not in detail

    created = admin_api.post(
        f"/api/v1/admin/users/{target.pk}/points",
        {"direction": "credit", "amount": 50, "message": "Thưởng giới thiệu"},
        format="json",
    )
    assert created.status_code == 201
    assert created.json()["data"]["balance"] == 50
    too_large = admin_api.post(
        f"/api/v1/admin/users/{target.pk}/points",
        {
            "direction": "debit",
            "amount": 100,
            "message": "Đổi rau củ theo mùa",
        },
        format="json",
    )
    assert too_large.status_code == 400


@pytest.mark.django_db
def test_admin_endpoints_require_scope_and_group(access_token):
    client = bearer_client(access_token)
    assert client.get("/api/v1/admin/users").status_code == 403
