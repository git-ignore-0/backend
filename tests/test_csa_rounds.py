"""CSA rounds: shared-resource holds, deadlines and membership entitlement."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import StoreOrder
from memberships import csa_rounds as service
from memberships.models import CSASelection, MembershipCycleQuota
from tests.test_store_orders import attach_active_membership
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


@pytest.fixture
def context(active_user, store_product, monkeypatch):
    now = timezone.now().replace(microsecond=0)
    monkeypatch.setattr(timezone, "now", lambda: now)
    membership = attach_active_membership(active_user, store_product)
    store_product.csa_quota_cost = Decimal("1")
    store_product.save(update_fields=["csa_quota_cost"])
    round_ = service.create_round(
        name="CSA",
        opens_at=now - timedelta(hours=1),
        closes_at=now + timedelta(hours=1),
        delivery_at=now + timedelta(days=1),
        actor=active_user,
    )
    round_ = service.publish_round(round_id=round_.pk, actor=active_user)
    return active_user, store_product, membership, round_, now


def save(context, quantity=2, **kwargs):
    user, product, _, round_, _ = context
    return service.save_selection(
        round_id=round_.pk,
        user=user,
        lines=[{"product_id": product.pk, "quantity": quantity}],
        **kwargs,
    )


def test_round_schedule_no_overlapping_published_windows(context):
    user, _, _, round_, now = context
    later = service.create_round(
        name="Next",
        opens_at=now,
        closes_at=now + timedelta(hours=2),
        delivery_at=round_.delivery_at,
        actor=user,
    )
    with pytest.raises(service.CSARoundError, match="csa_round_schedule_conflict"):
        service.publish_round(round_id=later.pk, actor=user)


def test_save_holds_points_and_stock_then_close_creates_order_once(context):
    user, product, membership, round_, _ = context
    selected = save(context)
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == 4
    from commerce.store_orders import _active_reserved_stock
    from memberships.services import quota_availability_rows

    assert _active_reserved_stock([product.pk])[product.pk] == 2
    assert quota_availability_rows(membership)[0][2] == 2
    service.process_round(round_.pk, now=round_.closes_at)
    service.process_round(round_.pk, now=round_.closes_at)
    selected.refresh_from_db()
    quota.refresh_from_db()
    assert selected.order_id
    assert StoreOrder.objects.count() == 1
    assert quota.remaining_units == 2
    assert selected.order.total_vnd == 0
    assert selected.order.expires_at is None
    assert selected.state == "locked"


def test_reopen_confirm_twice_and_expiry_refunds_once(context):
    user, _, membership, round_, _ = context
    selection = save(context)
    service.process_round(round_.pk, now=round_.closes_at)
    deadline = round_.delivery_at - timedelta(hours=1)
    for _ in range(2):
        service.reopen_selection(
            selection_id=selection.pk, actor=user, deadline=deadline, now=round_.closes_at
        )
        service.save_selection(
            round_id=round_.pk,
            user=user,
            lines=[{"product_id": context[1].pk, "quantity": 3}],
            now=round_.closes_at,
        )
        service.confirm_selection(selection_id=selection.pk, user=user, now=round_.closes_at)
    assert MembershipCycleQuota.objects.get(cycle__membership=membership).remaining_units == 1
    service.reopen_selection(
        selection_id=selection.pk, actor=user, deadline=deadline, now=round_.closes_at
    )
    service.process_round(round_.pk, now=deadline)
    service.process_round(round_.pk, now=deadline)
    selection.refresh_from_db()
    assert selection.state == "cancelled"
    assert selection.order.status == "cancelled"
    assert MembershipCycleQuota.objects.get(cycle__membership=membership).remaining_units == 4
    with pytest.raises(service.CSARoundError):
        service.reopen_selection(
            selection_id=selection.pk, actor=user, deadline=deadline, now=round_.closes_at
        )


def test_insufficient_points_rolls_back_previous_selection(context):
    selected = save(context)
    with pytest.raises(service.CSARoundError, match="csa_quota_insufficient"):
        save(context, 5)
    selected.refresh_from_db()
    assert selected.lines.get().quantity == 2


def test_api_requires_scopes_and_owns_selection(context, access_token):
    client = APIClient()
    assert client.get("/api/v1/csa-rounds").status_code in (401, 403)
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.get("/api/v1/csa-rounds")
    assert response.status_code == 200
    assert response.json()["data"][0]["id"] == str(context[3].pk)
    response = client.put(
        f"/api/v1/csa-rounds/{context[3].pk}/selection",
        {"expected_revision": 0, "lines": [{"product_id": str(context[1].pk), "quantity": 1}]},
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert CSASelection.objects.count() == 1


def test_shared_store_and_manual_usage_cannot_spend_csa_holds(context):
    import uuid

    from commerce.public_catalog import public_product_queryset
    from memberships.services import InsufficientQuota, create_usage_order

    selected = save(context)
    assert public_product_queryset().get(pk=context[1].pk).available_quantity == 8
    with pytest.raises(InsufficientQuota):
        create_usage_order(
            membership_id=context[2].pk,
            actor=context[0],
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": str(context[1].csa_quota_group_id), "units": 3}],
        )
    assert selected.quota_holds.get().units == 2


def test_round_boundary_lock_stale_revision_and_clear(context):
    selection = save(context)
    with pytest.raises(service.CSARoundError, match="csa_selection_stale"):
        save(context, 1, expected_revision=0)
    with pytest.raises(service.CSARoundError, match="csa_selection_locked"):
        save(context, now=context[3].closes_at)
    service.save_selection(round_id=context[3].pk, user=context[0], lines=[])
    service.process_round(context[3].pk, now=context[3].closes_at)
    selection.refresh_from_db()
    assert selection.state == "cancelled"
    assert not StoreOrder.objects.exists()


def test_deadline_must_be_future_and_strictly_before_delivery(context):
    selection = save(context)
    round_ = context[3]
    service.process_round(round_.pk, now=round_.closes_at)
    for deadline in (
        round_.closes_at,
        round_.delivery_at,
        round_.delivery_at + timedelta(seconds=1),
    ):
        with pytest.raises(service.CSARoundError, match="csa_reselection_deadline_invalid"):
            service.reopen_selection(
                selection_id=selection.pk, actor=context[0], deadline=deadline, now=round_.closes_at
            )


def test_freeze_preserves_old_round_until_delivery_day_and_blocks_new_round(context, monkeypatch):
    from memberships.services import ensure_due_cycles, sync_membership_status

    user, product, member, round_, now = context
    tomorrow = timezone.localdate(now) + timedelta(days=1)
    member.end_date = tomorrow
    member.save(update_fields=["end_date"])
    quota = MembershipCycleQuota.objects.get(cycle__membership=member)
    quota.cycle.end_date = tomorrow
    quota.cycle.save(update_fields=["end_date"])
    round_.closes_at = now + timedelta(days=1, hours=1)
    round_.delivery_at = now + timedelta(days=3)
    round_.save()
    selection = save(context, 1)
    later = now + timedelta(days=1, hours=2)
    monkeypatch.setattr(timezone, "now", lambda: later)
    member = sync_membership_status(member)
    assert member.status == "frozen"
    ensure_due_cycles(member)
    quota.refresh_from_db()
    assert quota.remaining_units == 4
    service.process_round(round_.pk)
    service.reopen_selection(
        selection_id=selection.pk, actor=user, deadline=round_.delivery_at - timedelta(hours=1)
    )
    save(context, 3)
    service.confirm_selection(selection_id=selection.pk, user=user)
    quota.refresh_from_db()
    assert quota.remaining_units == 1
    next_ = service.create_round(
        name="Next",
        opens_at=later,
        closes_at=later + timedelta(hours=1),
        delivery_at=round_.delivery_at,
        actor=user,
    )
    service.publish_round(round_id=next_.pk, actor=user)
    with pytest.raises(service.CSARoundError, match="csa_membership_unavailable"):
        service.save_selection(
            round_id=next_.pk, user=user, lines=[{"product_id": product.pk, "quantity": 1}]
        )
    day_after = round_.delivery_at + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: day_after)
    assert sync_membership_status(member).status == "ended"


def test_order_can_be_allocated_and_delivered_without_double_debit(context):
    import uuid

    from commerce.store_orders import approve_store_order, deliver_store_order, source_inventory

    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    selected.refresh_from_db()
    order = selected.order
    allocations = source_inventory(order)["suggestion"]
    approve_store_order(
        order_id=order.pk,
        actor=context[0],
        idempotency_key=uuid.uuid4(),
        allocations=[
            {
                "line_id": uuid.UUID(a["line_id"]),
                "batch_id": uuid.UUID(a["batch_id"]),
                "quantity": a["quantity"],
            }
            for a in allocations
        ],
    )
    assert MembershipCycleQuota.objects.get(cycle__membership=context[2]).remaining_units == 2
    assert not selected.stock_holds.exists()
    with pytest.raises(service.CSARoundError):
        service.reopen_selection(
            selection_id=selected.pk,
            actor=context[0],
            deadline=context[3].delivery_at - timedelta(hours=1),
            now=context[3].closes_at,
        )
    deliver_store_order(order_id=order.pk, actor=context[0], idempotency_key=uuid.uuid4())
    selected.refresh_from_db()
    assert selected.state == "delivered"


def test_member_cannot_administer_or_access_another_selection(context, access_token, active_user):
    selected = save(context)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert client.post("/api/v1/admin/csa-rounds", {}, format="json").status_code == 403
    access_token.scope = "profile"
    access_token.save(update_fields=["scope"])
    assert client.get("/api/v1/csa-rounds").status_code == 403
    access_token.scope = "profile email"
    access_token.save(update_fields=["scope"])
    from identity.models import AccountStatus, User

    other = User.objects.create_user(
        email="other@example.com",
        name="Other",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    from allauth.account.models import EmailAddress

    EmailAddress.objects.create(user=other, email=other.email, verified=True, primary=True)
    access_token.user = other
    access_token.save(update_fields=["user"])
    assert (
        client.post(
            f"/api/v1/csa-selections/{selected.pk}/confirm",
            {"expected_revision": selected.revision},
            format="json",
        ).status_code
        == 404
    )


def test_month_rollover_edit_cannot_double_count_preserved_hold(context, monkeypatch):
    from memberships.services import add_months

    user, _, member, round_, now = context
    member.duration_months_snapshot = 2
    member.end_date = add_months(member.start_date, 2)
    member.total_price_vnd_snapshot *= 2
    member.save()
    boundary = add_months(member.start_date, 1)
    quota = MembershipCycleQuota.objects.get(cycle__membership=member)
    quota.cycle.end_date = boundary
    quota.cycle.save(update_fields=["end_date"])
    later = now.replace(year=boundary.year, month=boundary.month, day=boundary.day)
    round_.closes_at = later + timedelta(hours=1)
    round_.delivery_at = later + timedelta(days=1)
    round_.save()
    from farms.models import FarmProductStockBatch

    FarmProductStockBatch.objects.filter(farm_product__product=context[1]).update(
        end_date=timezone.localdate(round_.delivery_at)
    )
    save(context, 2)
    monkeypatch.setattr(timezone, "now", lambda: later)
    with pytest.raises(service.CSARoundError, match="csa_quota_insufficient"):
        save(context, 8)


def test_admin_api_round_crud_publish_cancel_and_private_contract(context, access_token):
    from django.contrib.auth.models import Group
    from django.core.management import call_command

    user, _, member, round_, now = context
    group, _ = Group.objects.get_or_create(name="website_admin")
    user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    selected = save(context)
    assert client.get(f"/api/v1/admin/csa-rounds/{round_.pk}/selections").status_code == 200
    request = {
        "name": "Later",
        "opens_at": (round_.closes_at + timedelta(seconds=1)).isoformat(),
        "closes_at": (round_.closes_at + timedelta(hours=1)).isoformat(),
        "delivery_at": round_.delivery_at.isoformat(),
    }
    response = client.post("/api/v1/admin/csa-rounds", {**request, "products": []}, format="json")
    assert response.status_code == 400
    response = client.post("/api/v1/admin/csa-rounds", request, format="json")
    assert response.status_code == 201, response.json()
    pk = response.json()["data"]["id"]
    assert (
        client.patch(
            f"/api/v1/admin/csa-rounds/{pk}", {"name": "Edited"}, format="json"
        ).status_code
        == 200
    )
    assert (
        client.post(f"/api/v1/admin/csa-rounds/{pk}/publish", {}, format="json").status_code == 200
    )
    response = client.get(f"/api/v1/csa-rounds/{round_.pk}/products")
    assert response.status_code == 200, response.json()
    assert response["Cache-Control"] == "private, no-store"
    assert response.json()["data"][0]["available_quantity"] == 10
    assert response.json()["quota"][0]["available_units"] == "4.00"
    # Command and normal reads may both reconcile; neither may double debit.
    monkey_now = round_.closes_at
    service.process_round(round_.pk, now=monkey_now)
    call_command("process_csa_rounds")
    response = client.post(
        f"/api/v1/admin/csa-rounds/{round_.pk}/cancel", {"reason": "No harvest"}, format="json"
    )
    assert response.status_code == 200, response.json()
    selected.refresh_from_db()
    assert selected.state == "cancelled"
    assert MembershipCycleQuota.objects.get(cycle__membership=member).remaining_units == 4


def test_delayed_save_rechecks_cutoff_after_waiting_for_locks(context, monkeypatch):
    original = service._lock_resources

    def wait_then_lock(selection, products):
        original(selection, products)
        monkeypatch.setattr(timezone, "now", lambda: context[3].closes_at)

    monkeypatch.setattr(service, "_lock_resources", wait_then_lock)
    with pytest.raises(service.CSARoundError, match="csa_selection_locked"):
        save(context)


def test_frozen_old_order_does_not_take_new_membership_points(context, monkeypatch):
    import copy
    import uuid

    from memberships.services import sync_membership_status

    user, product, member, round_, now = context
    member.end_date = timezone.localdate(now) + timedelta(days=1)
    member.save(update_fields=["end_date"])
    round_.closes_at = now + timedelta(hours=2)
    round_.delivery_at = now + timedelta(days=2)
    round_.save()
    selected = save(context, 1)
    service.process_round(round_.pk, now=round_.closes_at)
    later = now + timedelta(days=1)
    monkeypatch.setattr(timezone, "now", lambda: later)
    member = sync_membership_status(member)
    assert member.status == "frozen"
    fresh = copy.copy(member)
    fresh.pk = uuid.uuid4()
    fresh.start_date = member.end_date
    fresh.end_date = fresh.start_date + timedelta(days=30)
    fresh.extension_end_date = None
    fresh.status = "active"
    fresh.current_slot = True
    fresh.farmbrite_order_reference = str(uuid.uuid4())
    fresh.farmbrite_order_reference_key = str(uuid.uuid4())
    fresh.save(force_insert=True)
    service.reopen_selection(
        selection_id=selected.pk, actor=user, deadline=round_.delivery_at - timedelta(hours=1)
    )
    save(context, 3)
    service.confirm_selection(selection_id=selected.pk, user=user)
    selected.refresh_from_db()
    assert selected.membership_id == member.pk
    assert selected.current_usage.membership_id == member.pk
    assert not fresh.usage_orders.exists()


def test_order_detail_reconciles_expired_reselection_without_worker(
    context, access_token, monkeypatch
):
    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    selected.refresh_from_db()
    deadline = context[3].delivery_at - timedelta(hours=1)
    service.reopen_selection(
        selection_id=selected.pk, actor=context[0], deadline=deadline, now=context[3].closes_at
    )
    access_token.expires = deadline + timedelta(days=1)
    access_token.save(update_fields=["expires"])
    monkeypatch.setattr(timezone, "now", lambda: deadline)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.get(f"/api/v1/store-orders/{selected.order_id}")
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["status"] == "cancelled"
    assert MembershipCycleQuota.objects.get(cycle__membership=context[2]).remaining_units == 4


def test_store_release_at_cycle_boundary_preserves_csa_points(context, monkeypatch):
    import uuid

    from commerce.models import ShippingAddress, StoreOrder
    from commerce.store_orders import cancel_store_order
    from memberships.services import ensure_due_cycles
    from tests.test_store_orders import quota_checkout_payload

    user, product, member, round_, now = context
    tomorrow = now + timedelta(days=1)
    quota = MembershipCycleQuota.objects.get(cycle__membership=member)
    quota.cycle.end_date = timezone.localdate(tomorrow)
    quota.cycle.save(update_fields=["end_date"])
    round_.closes_at = tomorrow + timedelta(hours=1)
    round_.delivery_at = tomorrow + timedelta(days=1)
    round_.save()
    save(context, 1)
    from types import SimpleNamespace

    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    response = client.post(
        "/api/v1/public/store-checkouts",
        quota_checkout_payload(
            product, ShippingAddress.objects.get(user=user), quantity=1, expected_available=9
        ),
        format="json",
    )
    assert response.status_code == 201, response.json()
    order_id = response.json()["data"]["id"]
    StoreOrder.objects.filter(pk=order_id).update(expires_at=tomorrow + timedelta(hours=2))
    monkeypatch.setattr(timezone, "now", lambda: tomorrow)
    ensure_due_cycles(member)
    cancel_store_order(order_id=order_id, actor=user, idempotency_key=uuid.uuid4(), reason="cancel")
    quota.refresh_from_db()
    assert quota.remaining_units == 1
