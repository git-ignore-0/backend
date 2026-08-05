import io
import threading
import uuid

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Group
from django.core.exceptions import FieldDoesNotExist
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models.deletion import ProtectedError
from django.db.models.query import QuerySet
from django.utils import timezone
from oauth2_provider.models import AccessToken
from PIL import Image
from rest_framework.test import APIClient

from content.models import MediaAsset
from identity.models import AccountStatus, User
from members.models import MemberProfile
from referrals.models import (
    PointDirection,
    PointTemplate,
    PointTransaction,
    RedemptionRequest,
    RedemptionStatus,
    ReferralAccount,
    Reward,
    RewardTranslation,
)
from referrals.services import (
    InsufficientRewardPoints,
    RedemptionTransitionConflict,
    complete_redemption,
    create_point_transaction,
    create_redemption,
    mark_redemption_contacted,
    point_balance,
    reject_redemption,
)


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


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


def create_reward(*, point_cost=100, is_active=True, position=0):
    asset = MediaAsset.objects.create(
        file=f"rewards/test-{uuid.uuid4()}.webp",
        variants=[],
        width=640,
        height=480,
        original_name="reward.webp",
    )
    reward = Reward.objects.create(
        point_cost=point_cost,
        image=asset,
        position=position,
        is_active=is_active,
    )
    RewardTranslation.objects.bulk_create(
        [
            RewardTranslation(
                reward=reward,
                locale="vi",
                name="Rau theo mùa",
                short_description="Rau tươi từ nông trại",
            ),
            RewardTranslation(
                reward=reward,
                locale="en",
                name="Seasonal vegetables",
                short_description="Fresh produce from the farm",
            ),
        ]
    )
    return reward


def grant_points(user, amount=100):
    create_point_transaction(
        user=user,
        actor=user,
        direction=PointDirection.CREDIT,
        amount=amount,
        message="Referral purchase confirmed",
    )


def create_open_redemption(user, reward, *, points=100):
    grant_points(user, points)
    item, balance, created = create_redemption(
        user=user,
        reward_id=reward.pk,
        idempotency_key=uuid.uuid4(),
    )
    assert created
    return item, balance


@pytest.fixture
def reward(db):
    return create_reward()


@pytest.fixture
def admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


def png_upload(name="reward.png"):
    output = io.BytesIO()
    Image.new("RGB", (80, 60), color=(40, 100, 60)).save(output, "PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


def run_transition(operation, *, redemption_id, actor):
    if operation == "contact":
        return mark_redemption_contacted(redemption_id=redemption_id, actor=actor)
    if operation == "complete":
        return complete_redemption(redemption_id=redemption_id, actor=actor)
    return reject_redemption(
        redemption_id=redemption_id,
        actor=actor,
        rejection_message="Unavailable",
    )


@pytest.mark.django_db
def test_public_catalog_only_returns_active_rewards_with_locale_fallback(client):
    active = create_reward(position=2)
    create_reward(is_active=False, position=1)
    RewardTranslation.objects.filter(reward=active, locale="en").delete()
    without_translation = create_reward(position=3)
    without_translation.translations.all().delete()

    response = client.get("/api/v1/public/rewards?locale=en")

    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response.json()["meta"]["page_size"] == 30
    assert response.json()["meta"]["total"] == 1
    assert response.json()["data"] == [
        {
            "id": str(active.pk),
            "point_cost": 100,
            "image": response.json()["data"][0]["image"],
            "position": 2,
            "requested_locale": "en",
            "content_locale": "vi",
            "is_fallback": True,
            "name": "Rau theo mùa",
            "short_description": "Rau tươi từ nông trại",
        }
    ]


@pytest.mark.django_db
def test_reward_contract_has_no_inventory_fields(client):
    reward = create_reward()

    with pytest.raises(FieldDoesNotExist):
        Reward._meta.get_field("quantity")
    with pytest.raises(FieldDoesNotExist):
        RedemptionRequest._meta.get_field("stock_reserved_at")
    with pytest.raises(FieldDoesNotExist):
        RedemptionRequest._meta.get_field("stock_restored_at")

    payload = client.get("/api/v1/public/rewards?locale=vi").json()["data"][0]
    assert payload["id"] == str(reward.pk)
    assert "quantity" not in payload
    assert "is_available" not in payload


@pytest.mark.django_db
def test_create_redemption_rejects_missing_inactive_and_insufficient_rewards(
    access_token, active_user
):
    client = bearer_client(access_token)
    missing = client.post(
        "/api/v1/referrals/redemptions",
        {"reward_id": uuid.uuid4(), "idempotency_key": uuid.uuid4()},
        format="json",
    )
    assert missing.status_code == 404
    assert missing.json()["errors"][0]["code"] == "reward_not_found"

    inactive = create_reward(is_active=False)
    response = client.post(
        "/api/v1/referrals/redemptions",
        {"reward_id": inactive.pk, "idempotency_key": uuid.uuid4()},
        format="json",
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "reward_inactive"

    active = create_reward(point_cost=100)
    response = client.post(
        "/api/v1/referrals/redemptions",
        {"reward_id": active.pk, "idempotency_key": uuid.uuid4()},
        format="json",
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "insufficient_points"
    assert not RedemptionRequest.objects.filter(user=active_user).exists()


@pytest.mark.django_db
def test_create_only_accepts_reward_and_idempotency_key(access_token, active_user, reward):
    grant_points(active_user)
    response = bearer_client(access_token).post(
        "/api/v1/referrals/redemptions",
        {
            "reward_id": reward.pk,
            "idempotency_key": uuid.uuid4(),
            "user_id": uuid.uuid4(),
            "point_cost": 1,
            "direction": "credit",
            "status": "completed",
        },
        format="json",
    )

    assert response.status_code == 400
    assert not RedemptionRequest.objects.exists()
    assert PointTransaction.objects.filter(user=active_user).count() == 1


@pytest.mark.django_db
def test_same_idempotency_key_returns_same_request_and_one_debit(access_token, active_user, reward):
    grant_points(active_user)
    key = uuid.uuid4()
    payload = {"reward_id": reward.pk, "idempotency_key": key}
    client = bearer_client(access_token)

    first = client.post("/api/v1/referrals/redemptions", payload, format="json")
    second = client.post("/api/v1/referrals/redemptions", payload, format="json")

    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["data"]["redemption"]["id"] == second.json()["data"]["redemption"]["id"]
    assert second.json()["data"]["idempotent_replay"] is True
    assert RedemptionRequest.objects.filter(user=active_user).count() == 1
    assert (
        PointTransaction.objects.filter(
            user=active_user,
            template=PointTemplate.REWARD_REDEMPTION,
        ).count()
        == 1
    )
    assert point_balance(active_user) == 0


@pytest.mark.django_db
def test_idempotency_key_cannot_be_reused_for_another_reward(access_token, active_user, reward):
    other_reward = create_reward()
    grant_points(active_user, 200)
    key = uuid.uuid4()
    client = bearer_client(access_token)
    assert (
        client.post(
            "/api/v1/referrals/redemptions",
            {"reward_id": reward.pk, "idempotency_key": key},
            format="json",
        ).status_code
        == 201
    )

    response = client.post(
        "/api/v1/referrals/redemptions",
        {"reward_id": other_reward.pk, "idempotency_key": key},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "idempotency_conflict"
    assert RedemptionRequest.objects.count() == 1


@pytest.mark.django_db
def test_user_can_only_list_and_view_own_redemptions(
    access_token, active_user, oauth_application, reward
):
    item, _balance = create_open_redemption(active_user, reward)
    other = create_active_user("other-redemption@example.com")
    other_token = AccessToken.objects.create(
        user=other,
        application=oauth_application,
        token="other-redemption-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )
    other_client = bearer_client(other_token)

    assert other_client.get("/api/v1/referrals/redemptions").json()["data"] == []
    assert other_client.get(f"/api/v1/referrals/redemptions/{item.pk}").status_code == 404
    own = bearer_client(access_token).get(f"/api/v1/referrals/redemptions/{item.pk}?locale=en")
    assert own.status_code == 200
    assert own.json()["data"]["reward_name"] == "Seasonal vegetables"


@pytest.mark.django_db
def test_reward_price_changes_do_not_change_redemption_snapshot(active_user, reward):
    item, _balance = create_open_redemption(active_user, reward)
    reward.point_cost = 999
    reward.save(update_fields=["point_cost", "updated_at"])
    RewardTranslation.objects.filter(reward=reward, locale="vi").update(name="Tên mới")

    item.refresh_from_db()

    assert item.point_cost_snapshot == 100
    assert item.reward_name_vi_snapshot == "Rau theo mùa"
    assert item.debit_transaction.amount == 100


@pytest.mark.django_db
def test_database_constraints_reject_invalid_redemption_states(active_user, reward):
    item = RedemptionRequest.objects.create(
        user=active_user,
        reward=reward,
        reward_name_vi_snapshot="Rau theo mùa",
        reward_name_en_snapshot="Seasonal vegetables",
        point_cost_snapshot=100,
        idempotency_key=uuid.uuid4(),
    )
    unrelated_credit, _balance = create_point_transaction(
        user=active_user,
        actor=active_user,
        direction=PointDirection.CREDIT,
        amount=100,
        message="Constraint fixture",
    )

    invalid_updates = [
        {"status": "invalid"},
        {"status": RedemptionStatus.COMPLETED},
        {
            "status": RedemptionStatus.REJECTED,
            "rejected_at": timezone.now(),
            "rejection_message": "Missing refund",
        },
        {"refund_transaction": unrelated_credit},
    ]
    for update in invalid_updates:
        with pytest.raises(IntegrityError), transaction.atomic():
            RedemptionRequest.objects.filter(pk=item.pk).update(**update)


@pytest.mark.parametrize("operation", ["contact", "complete", "reject"])
@pytest.mark.parametrize(
    "corruption",
    ["missing", "wrong_user", "wrong_direction", "wrong_amount", "wrong_template"],
)
@pytest.mark.django_db
def test_admin_transitions_reject_invalid_debit_transactions(
    operation,
    corruption,
    active_user,
    reward,
):
    item, _balance = create_open_redemption(active_user, reward)
    debit = item.debit_transaction

    if corruption == "missing":
        RedemptionRequest.objects.filter(pk=item.pk).update(debit_transaction=None)
    elif corruption == "wrong_user":
        other_user = create_active_user(f"wrong-debit-{operation}@example.com")
        PointTransaction.objects.filter(pk=debit.pk).update(user=other_user)
    elif corruption == "wrong_direction":
        PointTransaction.objects.filter(pk=debit.pk).update(direction=PointDirection.CREDIT)
    elif corruption == "wrong_amount":
        PointTransaction.objects.filter(pk=debit.pk).update(amount=item.point_cost_snapshot + 1)
    else:
        PointTransaction.objects.filter(pk=debit.pk).update(
            template=PointTemplate.MANUAL_ADJUSTMENT
        )

    with pytest.raises(RedemptionTransitionConflict, match="debit transaction is invalid"):
        run_transition(operation, redemption_id=item.pk, actor=active_user)

    item.refresh_from_db()
    assert item.status == RedemptionStatus.PENDING
    assert item.refund_transaction_id is None
    assert not PointTransaction.objects.filter(template=PointTemplate.REWARD_REFUND).exists()


@pytest.mark.parametrize("operation", ["contact", "complete", "reject"])
@pytest.mark.parametrize(
    "corruption",
    ["wrong_user", "wrong_direction", "wrong_amount", "wrong_template"],
)
@pytest.mark.django_db
def test_terminal_redemptions_reject_invalid_refund_transactions(
    operation,
    corruption,
    active_user,
    reward,
):
    item, _balance = create_open_redemption(active_user, reward)
    rejected, _balance = reject_redemption(
        redemption_id=item.pk,
        actor=active_user,
        rejection_message="Unavailable",
    )
    refund = rejected.refund_transaction

    if corruption == "wrong_user":
        other_user = create_active_user(f"wrong-refund-{operation}@example.com")
        PointTransaction.objects.filter(pk=refund.pk).update(user=other_user)
    elif corruption == "wrong_direction":
        PointTransaction.objects.filter(pk=refund.pk).update(direction=PointDirection.DEBIT)
    elif corruption == "wrong_amount":
        PointTransaction.objects.filter(pk=refund.pk).update(amount=item.point_cost_snapshot + 1)
    else:
        PointTransaction.objects.filter(pk=refund.pk).update(
            template=PointTemplate.MANUAL_ADJUSTMENT
        )

    with pytest.raises(RedemptionTransitionConflict, match="refund transaction is invalid"):
        run_transition(operation, redemption_id=item.pk, actor=active_user)

    item.refresh_from_db()
    assert item.status == RedemptionStatus.REJECTED
    assert PointTransaction.objects.filter(template=PointTemplate.REWARD_REFUND).count() <= 1


@pytest.mark.parametrize(
    ("action", "payload"),
    [
        ("mark-contacted", {}),
        ("complete", {}),
        ("reject", {"rejection_message": "Unavailable"}),
    ],
)
@pytest.mark.django_db
def test_admin_api_returns_safe_conflict_for_invalid_redemption_debit(
    action,
    payload,
    admin_api,
    active_user,
    reward,
):
    item, _balance = create_open_redemption(active_user, reward)
    RedemptionRequest.objects.filter(pk=item.pk).update(debit_transaction=None)

    response = admin_api.post(
        f"/api/v1/admin/redemptions/{item.pk}/{action}",
        payload,
        format="json",
    )

    assert response.status_code == 409
    assert response.json() == {"errors": [{"code": "redemption_transition_conflict"}]}


@pytest.mark.django_db
def test_request_and_debit_roll_back_together(monkeypatch, active_user, reward):
    grant_points(active_user)
    original_save = RedemptionRequest.save

    def fail_when_linking_debit(instance, *args, **kwargs):
        if instance.debit_transaction_id:
            raise RuntimeError("simulated link failure")
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(RedemptionRequest, "save", fail_when_linking_debit)

    with pytest.raises(RuntimeError, match="simulated link failure"):
        create_redemption(
            user=active_user,
            reward_id=reward.pk,
            idempotency_key=uuid.uuid4(),
        )

    assert not RedemptionRequest.objects.exists()
    assert not PointTransaction.objects.filter(template=PointTemplate.REWARD_REDEMPTION).exists()
    assert point_balance(active_user) == 100


@pytest.mark.django_db
def test_redemption_services_acquire_locks_in_required_order(monkeypatch, active_user, reward):
    seen_models = []
    original_select_for_update = QuerySet.select_for_update

    def track_select_for_update(queryset, *args, **kwargs):
        seen_models.append(queryset.model)
        return original_select_for_update(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", track_select_for_update)
    grant_points(active_user)
    seen_models.clear()

    item, _balance, _created = create_redemption(
        user=active_user,
        reward_id=reward.pk,
        idempotency_key=uuid.uuid4(),
    )
    assert seen_models == [ReferralAccount, Reward]

    seen_models.clear()
    reject_redemption(
        redemption_id=item.pk,
        actor=active_user,
        rejection_message="Unavailable",
    )
    assert seen_models == [RedemptionRequest, ReferralAccount]


@pytest.mark.django_db
def test_reject_refunds_once_and_exposes_message_without_internal_note(
    access_token, active_user, reward
):
    item, _balance = create_open_redemption(active_user, reward)

    rejected, balance = reject_redemption(
        redemption_id=item.pk,
        actor=active_user,
        rejection_message="Quà đang tạm hết.",
        internal_note="Do not expose this note",
    )

    assert balance == 100
    assert rejected.status == RedemptionStatus.REJECTED
    assert rejected.refund_transaction.amount == 100
    assert rejected.refund_transaction.template == PointTemplate.REWARD_REFUND
    assert point_balance(active_user) == 100
    with pytest.raises(RedemptionTransitionConflict):
        reject_redemption(
            redemption_id=item.pk,
            actor=active_user,
            rejection_message="Retry",
        )
    assert PointTransaction.objects.filter(template=PointTemplate.REWARD_REFUND).count() == 1

    public = (
        bearer_client(access_token).get(f"/api/v1/referrals/redemptions/{item.pk}").json()["data"]
    )
    assert public["rejection_message"] == "Quà đang tạm hết."
    assert "internal_note" not in public


@pytest.mark.django_db
def test_complete_only_changes_status_and_terminal_states_cannot_reverse(active_user, reward):
    completed, _balance = create_open_redemption(active_user, reward)
    complete_redemption(redemption_id=completed.pk, actor=active_user)
    assert PointTransaction.objects.filter(user=active_user).count() == 2
    assert point_balance(active_user) == 0
    with pytest.raises(RedemptionTransitionConflict):
        complete_redemption(redemption_id=completed.pk, actor=active_user)
    with pytest.raises(RedemptionTransitionConflict):
        mark_redemption_contacted(redemption_id=completed.pk, actor=active_user)
    with pytest.raises(RedemptionTransitionConflict):
        reject_redemption(
            redemption_id=completed.pk,
            actor=active_user,
            rejection_message="Cannot reverse",
        )

    second_reward = create_reward()
    grant_points(active_user)
    rejected, _balance, _created = create_redemption(
        user=active_user,
        reward_id=second_reward.pk,
        idempotency_key=uuid.uuid4(),
    )
    reject_redemption(
        redemption_id=rejected.pk,
        actor=active_user,
        rejection_message="Out of stock",
    )
    with pytest.raises(RedemptionTransitionConflict):
        complete_redemption(redemption_id=rejected.pk, actor=active_user)


@pytest.mark.django_db
def test_admin_reward_crud_media_and_no_hard_delete(admin_api, reward):
    upload = admin_api.post(
        "/api/v1/admin/rewards/media",
        {"image": png_upload()},
        format="multipart",
    )
    assert upload.status_code == 201
    asset = MediaAsset.objects.get(pk=upload.json()["data"]["id"])
    assert asset.file.name.startswith("rewards/")

    created = admin_api.post(
        "/api/v1/admin/rewards",
        {
            "point_cost": 250,
            "image_id": str(asset.pk),
            "is_active": True,
            "translations": [
                {"locale": "vi", "name": "Gà tươi", "short_description": ""},
                {"locale": "en", "name": "Fresh chicken", "short_description": ""},
            ],
        },
        format="json",
    )
    assert created.status_code == 201
    reward_id = created.json()["data"]["id"]
    updated = admin_api.patch(
        f"/api/v1/admin/rewards/{reward_id}",
        {
            "point_cost": 300,
            "is_active": False,
            "translations": [
                {"locale": "vi", "name": "Gà thả vườn", "short_description": "Mới"},
                {"locale": "en", "name": "Free-range chicken", "short_description": "New"},
            ],
        },
        format="json",
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["point_cost"] == 300
    assert "quantity" not in updated.json()["data"]
    assert "is_available" not in updated.json()["data"]
    assert updated.json()["data"]["is_active"] is False
    assert {item["name"] for item in updated.json()["data"]["translations"]} == {
        "Gà thả vườn",
        "Free-range chicken",
    }
    assert RewardTranslation.objects.filter(reward_id=reward_id).count() == 2
    assert (
        admin_api.patch(
            f"/api/v1/admin/rewards/{reward_id}",
            {"quantity": 8},
            format="json",
        ).status_code
        == 400
    )
    assert admin_api.delete(f"/api/v1/admin/rewards/{reward_id}").status_code == 405

    item_user = create_active_user("protected-reward@example.com")
    item, _balance = create_open_redemption(item_user, reward)
    assert item.reward_id == reward.pk
    with pytest.raises(ProtectedError):
        reward.delete()


@pytest.mark.django_db
def test_admin_reorders_rewards_atomically_and_rejects_manual_position(admin_api):
    first = create_reward(position=0)
    second = create_reward(position=1)
    third = create_reward(position=2)

    response = admin_api.post(
        "/api/v1/admin/rewards/reorder",
        {"page": 1, "reward_ids": [str(third.pk), str(first.pk), str(second.pk)]},
        format="json",
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [
        str(third.pk),
        str(first.pk),
        str(second.pk),
    ]
    assert list(Reward.objects.order_by("position").values_list("pk", flat=True)) == [
        third.pk,
        first.pk,
        second.pk,
    ]

    stale = admin_api.post(
        "/api/v1/admin/rewards/reorder",
        {"page": 1, "reward_ids": [str(first.pk), str(second.pk)]},
        format="json",
    )
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "reward_reorder_conflict"
    assert list(Reward.objects.order_by("position").values_list("pk", flat=True)) == [
        third.pk,
        first.pk,
        second.pk,
    ]

    invalid = admin_api.patch(
        f"/api/v1/admin/rewards/{first.pk}",
        {"position": 99},
        format="json",
    )
    assert invalid.status_code == 400


@pytest.mark.django_db
def test_admin_permissions_search_filter_pagination_actions_and_private_note(
    admin_api, access_token, active_user, reward
):
    no_permission = bearer_client(access_token)
    assert no_permission.get("/api/v1/admin/rewards").status_code == 200

    target = create_active_user(
        "redemption-search@example.com",
        name="Reward Member",
        phone="+84 912 345 678",
    )
    item, _balance = create_open_redemption(target, reward)
    contacted = admin_api.post(
        f"/api/v1/admin/redemptions/{item.pk}/mark-contacted",
        {"internal_note": "Call after 5 PM"},
        format="json",
    )
    assert contacted.status_code == 200
    assert contacted.json()["data"]["redemption"]["status"] == "contacted"
    assert contacted.json()["data"]["redemption"]["internal_note"] == "Call after 5 PM"

    searched = admin_api.get("/api/v1/admin/redemptions?search=912%20345&status=contacted")
    assert searched.status_code == 200
    assert searched.json()["data"][0]["id"] == str(item.pk)
    assert searched.json()["meta"]["page_size"] == 30
    for _index in range(30):
        RedemptionRequest.objects.create(
            user=active_user,
            reward=reward,
            reward_name_vi_snapshot="Rau theo mùa",
            reward_name_en_snapshot="Seasonal vegetables",
            point_cost_snapshot=100,
            idempotency_key=uuid.uuid4(),
        )
    assert admin_api.get("/api/v1/admin/redemptions?page=2").json()["data"]

    completed = admin_api.post(
        f"/api/v1/admin/redemptions/{item.pk}/complete",
        {},
        format="json",
    )
    assert completed.status_code == 200
    conflict = admin_api.post(
        f"/api/v1/admin/redemptions/{item.pk}/reject",
        {"rejection_message": "Too late"},
        format="json",
    )
    assert conflict.status_code == 409
    assert point_balance(target) == 0

    rejected_item, _balance = create_open_redemption(target, reward)
    rejected = admin_api.post(
        f"/api/v1/admin/redemptions/{rejected_item.pk}/reject",
        {"rejection_message": "Quà tạm hết", "internal_note": "Restock next week"},
        format="json",
    )
    assert rejected.status_code == 200
    assert rejected.json()["data"]["redemption"]["status"] == RedemptionStatus.REJECTED
    assert rejected.json()["data"]["redemption"]["points_balance"] == 100
    repeated = admin_api.post(
        f"/api/v1/admin/redemptions/{rejected_item.pk}/reject",
        {"rejection_message": "Retry"},
        format="json",
    )
    assert repeated.status_code == 409
    assert (
        PointTransaction.objects.filter(user=target, template=PointTemplate.REWARD_REFUND).count()
        == 1
    )


@pytest.mark.django_db
def test_admin_endpoints_require_both_scope_and_group(access_token, active_user):
    client = bearer_client(access_token)
    assert client.get("/api/v1/admin/rewards").status_code == 403
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    assert client.get("/api/v1/admin/rewards").status_code == 403

    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    assert client.get("/api/v1/admin/rewards").status_code == 200


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_two_concurrent_creates_are_serialized(reward):
    user = create_active_user("concurrent-create@example.com")
    grant_points(user)
    barrier = threading.Barrier(3)
    outcomes = []

    def redeem():
        close_old_connections()
        try:
            local_user = User.objects.get(pk=user.pk)
            barrier.wait()
            create_redemption(
                user=local_user,
                reward_id=reward.pk,
                idempotency_key=uuid.uuid4(),
            )
            outcomes.append("created")
        except InsufficientRewardPoints:
            outcomes.append("insufficient")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=redeem) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "insufficient"]
    assert RedemptionRequest.objects.count() == 1
    assert point_balance(user) == 0


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_complete_and_reject_only_commit_one_transition(reward):
    user = create_active_user("concurrent-action@example.com")
    item, _balance = create_open_redemption(user, reward)
    actor = create_active_user("concurrent-admin@example.com")
    barrier = threading.Barrier(3)
    outcomes = []

    def complete():
        close_old_connections()
        try:
            barrier.wait()
            complete_redemption(redemption_id=item.pk, actor=User.objects.get(pk=actor.pk))
            outcomes.append("completed")
        except RedemptionTransitionConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    def reject():
        close_old_connections()
        try:
            barrier.wait()
            reject_redemption(
                redemption_id=item.pk,
                actor=User.objects.get(pk=actor.pk),
                rejection_message="Unavailable",
            )
            outcomes.append("rejected")
        except RedemptionTransitionConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=complete), threading.Thread(target=reject)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert outcomes.count("conflict") == 1
    item.refresh_from_db()
    assert item.status in {RedemptionStatus.COMPLETED, RedemptionStatus.REJECTED}
    assert PointTransaction.objects.filter(template=PointTemplate.REWARD_REFUND).count() <= 1
