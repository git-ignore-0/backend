"""Regression coverage for CSA checkout, audit and quota domain boundaries."""

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from commerce.models import ShippingAddress, StoreOrder
from memberships import csa_rounds as service
from memberships.models import (
    CSAQuotaHold,
    CSARound,
    CSASelection,
    MembershipCycleQuota,
    MembershipUsageAllocation,
    MembershipUsageLine,
    MembershipUsageOrder,
)
from tests.test_csa_rounds import context as context
from tests.test_csa_rounds import save
from tests.test_store_orders import quota_checkout_payload
from tests.test_store_orders import store_product as store_product
from tests.test_store_orders import supported_area as supported_area

pytestmark = pytest.mark.django_db


def _reopen(context, selection):
    service.process_round(context[3].pk, now=context[3].closes_at)
    return service.reopen_selection(
        selection_id=selection.pk,
        actor=context[0],
        deadline=context[3].delivery_at - timedelta(hours=1),
        now=context[3].closes_at,
    )


def test_store_client_selection_uuid_cannot_block_round_close(context):
    user, product, _, round_, _ = context
    selected = save(context)
    payload = quota_checkout_payload(
        product, ShippingAddress.objects.get(user=user), quantity=1, expected_available=8
    )
    payload["idempotency_key"] = str(selected.pk)
    client = APIClient()
    client.force_authenticate(user, token=SimpleNamespace(scope="profile email"))
    response = client.post("/api/v1/public/store-checkouts", payload, format="json")
    assert response.status_code == 201, response.json()
    service.process_round(round_.pk, now=round_.closes_at)
    service.process_round(round_.pk, now=round_.closes_at)
    selected.refresh_from_db()
    assert selected.state == "locked"
    assert selected.order.create_idempotency_key != selected.pk
    assert StoreOrder.objects.count() == 2


@pytest.mark.parametrize("reselect", [False, True])
def test_existing_selection_reuses_shipping_snapshot_after_profile_deletion(context, reselect):
    selected = save(context)
    original = selected.address_snapshot
    if reselect:
        _reopen(context, selected)
    ShippingAddress.objects.filter(user=context[0]).delete()
    saved = save(context, 3, **({"now": context[3].closes_at} if reselect else {}))
    assert saved.address_snapshot == original
    if reselect:
        service.confirm_selection(selection_id=saved.pk, user=context[0], now=context[3].closes_at)
    else:
        service.process_round(context[3].pk, now=context[3].closes_at)
    saved.refresh_from_db()
    assert saved.order.address_snapshot == original["address_snapshot"]


def test_new_selection_locks_checkout_identity_and_address_before_membership(context, monkeypatch):
    # Instrument real lock helpers to assert the inter-service deadlock boundary.
    from commerce.store_orders import _lock_checkout_identity

    calls = []
    for name, original in (
        ("_lock_resources", service._lock_resources),
        ("_lock_checkout_identity", _lock_checkout_identity),
        ("_address_for_checkout", service._address_for_checkout),
        ("_entitled_membership", service._entitled_membership),
    ):

        def track(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, track, raising=False)
    save(context)
    assert calls == [
        "_lock_resources",
        "_lock_checkout_identity",
        "_address_for_checkout",
        "_entitled_membership",
    ]


def test_latest_selection_locale_is_persisted_before_close(context):
    save(context, locale="vi")
    selected = save(context, locale="en")
    selected.refresh_from_db()
    assert selected.locale == "en"
    service.process_round(context[3].pk, now=context[3].closes_at)
    selected.refresh_from_db()
    assert selected.order.locale == "en"


def test_reconfirmed_order_adopts_latest_selection_locale(context):
    selected = save(context, locale="vi")
    _reopen(context, selected)
    save(context, locale="en", now=context[3].closes_at)
    service.confirm_selection(selection_id=selected.pk, user=context[0], now=context[3].closes_at)
    selected.refresh_from_db()
    assert selected.order.locale == "en"


@pytest.mark.parametrize("published", [False, True])
def test_cancellation_audit_survives_without_selections(context, published):
    user, _, _, round_, _ = context
    if not published:
        round_.published_at = None
        round_.save(update_fields=["published_at"])
    cancelled = service.cancel_round(round_id=round_.pk, actor=user, reason="No harvest")
    cancelled.refresh_from_db()
    assert cancelled.cancellation_reason == "No harvest"
    assert cancelled.cancelled_by_id == user.pk
    # Retrying cancellation must retain its original audit evidence.
    service.cancel_round(round_id=round_.pk, actor=None, reason="Replacement")
    cancelled.refresh_from_db()
    assert cancelled.cancellation_reason == "No harvest"
    assert cancelled.cancelled_by_id == user.pk


def test_reserved_quota_bulk_credits_need_one_aggregate(context):
    selected = save(context)
    quota = MembershipCycleQuota.objects.get(cycle__membership=context[2])
    selected.state = "reselecting"
    selected.reopen_until = context[3].delivery_at
    usage = MembershipUsageOrder.objects.create(
        membership=context[2],
        customer=context[2].customer,
        csa_selection=selected,
        idempotency_key=uuid.uuid4(),
    )
    selected.current_usage = usage
    selected.save()
    line = MembershipUsageLine.objects.create(
        order=usage, product_snapshot=quota.product_snapshot, units=1
    )
    MembershipUsageAllocation.objects.create(line=line, cycle_quota=quota, units=1)
    for sequence in (2, 3):
        round_ = CSARound.objects.create(
            sequence=sequence,
            name="Other",
            opens_at=context[3].opens_at,
            closes_at=context[3].closes_at,
            delivery_at=context[3].delivery_at,
        )
        other = CSASelection.objects.create(
            round=round_,
            membership=context[2],
            state="reselecting",
            reopen_until=context[3].delivery_at,
        )
        usage = MembershipUsageOrder.objects.create(
            membership=context[2],
            customer=context[2].customer,
            csa_selection=other,
            idempotency_key=uuid.uuid4(),
        )
        other.current_usage = usage
        other.save()
        line = MembershipUsageLine.objects.create(
            order=usage, product_snapshot=quota.product_snapshot, units=1
        )
        MembershipUsageAllocation.objects.create(line=line, cycle_quota=quota, units=1)
        CSAQuotaHold.objects.create(selection=other, cycle_quota=quota, units=2)
    with CaptureQueriesContext(connection) as queries:
        held = service.reserved_quota([quota.pk])
    assert held[quota.pk] == Decimal(3)
    credit_queries = [q for q in queries if "memberships_membershipusageallocation" in q["sql"]]
    assert len(credit_queries) == 1


def test_quota_candidates_query_own_credit_once(context):
    selected = _reopen(context, save(context))
    selected = save(context, 3, now=context[3].closes_at)
    with CaptureQueriesContext(connection) as queries:
        candidates = service._quota_candidates(
            context[2], selected, {context[1].csa_quota_group_id}
        )
    assert candidates[context[1].csa_quota_group_id][0][1] == 4
    credit_queries = [q for q in queries if "memberships_membershipusageallocation" in q["sql"]]
    assert len(credit_queries) == 1


def test_user_scoped_due_processing_does_not_refresh_other_members(context):
    from identity.models import AccountStatus, User
    from memberships.models import Membership

    user = User.objects.create_user(
        email="unrelated-csa@example.com",
        name="Unrelated",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    old = context[4] - timedelta(days=60)
    Membership.objects.filter(pk=context[2].pk).update(
        start_date=old.date(),
        end_date=(old + timedelta(days=30)).date(),
        status="ended",
        current_slot=None,
        updated_at=old,
    )
    assert service.process_due_rounds(user=user) == 0
    context[2].refresh_from_db()
    assert context[2].updated_at == old


@pytest.mark.parametrize("reselect", [False, True])
def test_save_rechecks_deadline_after_inventory_lock(context, monkeypatch, reselect):
    from django.utils import timezone

    if reselect:
        selected = save(context)
        _reopen(context, selected)
        cutoff = context[3].delivery_at - timedelta(hours=1)
        monkeypatch.setattr(timezone, "now", lambda: context[3].closes_at)
    else:
        cutoff = context[3].closes_at
    original = service.active_product_stock

    def delayed_stock(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(timezone, "now", lambda: cutoff)
        return result

    monkeypatch.setattr(service, "active_product_stock", delayed_stock)
    code = "csa_reselection_expired" if reselect else "csa_selection_locked"
    with pytest.raises(service.CSARoundError, match=code):
        save(context, 3)


def test_save_aggregates_csa_stock_only_once(context):
    save(context)
    with CaptureQueriesContext(connection) as queries:
        save(context, 3)
    sums = [
        q["sql"] for q in queries if "memberships_csastockhold" in q["sql"] and "SUM(" in q["sql"]
    ]
    assert len(sums) == 1


def test_save_rechecks_membership_end_date_after_inventory_lock(context, monkeypatch):
    from django.utils import timezone

    _, _, member, round_, now = context
    member.end_date = timezone.localdate(now) + timedelta(days=1)
    member.save(update_fields=["end_date"])
    round_.closes_at = now + timedelta(days=2)
    round_.delivery_at = now + timedelta(days=3)
    round_.save(update_fields=["closes_at", "delivery_at"])
    original = service.active_product_stock

    def delayed_stock(*args, **kwargs):
        result = original(*args, **kwargs)
        monkeypatch.setattr(timezone, "now", lambda: now + timedelta(days=1))
        return result

    monkeypatch.setattr(service, "active_product_stock", delayed_stock)
    with pytest.raises(service.CSARoundError, match="csa_membership_unavailable"):
        save(context)
    assert not CSASelection.objects.filter(round=round_).exists()


def test_store_order_refresh_only_processes_its_own_csa_selection(context):
    import copy

    from commerce.store_orders import expire_order_if_needed

    selected = save(context)
    service.process_round(context[3].pk, now=context[3].closes_at)
    selected.refresh_from_db()
    other_member = copy.copy(context[2])
    other_member.pk = uuid.uuid4()
    other_member.current_slot = None
    other_member.farmbrite_order_reference = str(uuid.uuid4())
    other_member.farmbrite_order_reference_key = str(uuid.uuid4())
    other_member.save(force_insert=True)
    other = CSASelection.objects.create(round=context[3], membership=other_member)
    expire_order_if_needed(selected.order_id, now=context[3].closes_at)
    other.refresh_from_db()
    assert other.state == "selecting"
