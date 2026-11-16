"""Date-only CSA delivery boundaries and historical migration safety."""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from memberships import csa_rounds
from memberships.models import CSARound

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(active_user):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    client = APIClient()
    client.force_authenticate(
        active_user, token=SimpleNamespace(scope="profile email content:manage")
    )
    return client


def make_round(actor, *, closes_at=None, delivery_date=date(2027, 1, 1)):
    return csa_rounds.create_round(
        name="Date only",
        opens_at=datetime(2026, 12, 30, 2, tzinfo=UTC),
        closes_at=closes_at or datetime(2026, 12, 31, 16, 59, tzinfo=UTC),
        delivery_date=delivery_date,
        actor=actor,
    )


def test_round_stores_and_returns_date_only(active_user, admin_client):
    row = make_round(active_user)
    row.refresh_from_db()
    assert type(row.delivery_date) is date
    response = admin_client.get(f"/api/v1/admin/csa-rounds/{row.pk}")
    assert response.status_code == 200
    assert response.data["data"]["delivery_date"] == "2027-01-01"
    assert "delivery_at" not in response.data["data"]


@pytest.mark.parametrize(
    "close",
    [
        datetime(2026, 12, 31, 17, tzinfo=UTC),
        datetime(2026, 12, 31, 23, 59, tzinfo=UTC),
    ],
)
def test_close_must_precede_vietnam_delivery_day(active_user, close):
    with pytest.raises(csa_rounds.CSARoundError, match="csa_round_schedule_invalid"):
        make_round(active_user, closes_at=close)
    assert not CSARound.objects.exists()


def test_database_rejects_same_vietnam_day_close(active_user):
    row = make_round(active_user)
    with pytest.raises(IntegrityError), transaction.atomic():
        CSARound.objects.filter(pk=row.pk).update(closes_at=datetime(2026, 12, 31, 17, tzinfo=UTC))


def test_delivery_status_changes_at_vietnam_midnight_without_delivery(active_user):
    row = make_round(active_user)
    row.published_at = row.opens_at
    row.save(update_fields=["published_at"])
    assert csa_rounds.round_status(row, datetime(2026, 12, 31, 16, 59, tzinfo=UTC)) == "closed"
    assert csa_rounds.round_status(row, datetime(2026, 12, 31, 17, tzinfo=UTC)) == "delivery"
    assert not row.selections.exists()


def test_companion_cutoff_boundary_and_disable_after_cutoff(active_user, monkeypatch):
    now = datetime(2026, 12, 31, 12, tzinfo=UTC)
    monkeypatch.setattr(timezone, "now", lambda: now)
    row = make_round(active_user)
    for cutoff in [datetime(2026, 12, 31, 17, tzinfo=UTC), now]:
        with pytest.raises(csa_rounds.CSARoundError, match="csa_companion_deadline_invalid"):
            csa_rounds.configure_companion_delivery(
                round_id=row.pk,
                actor=active_user,
                companion_delivery_enabled=True,
                companion_delivery_cutoff=cutoff,
            )
    csa_rounds.configure_companion_delivery(
        round_id=row.pk,
        actor=active_user,
        companion_delivery_enabled=True,
        companion_delivery_cutoff=datetime(2026, 12, 31, 16, 59, tzinfo=UTC),
    )
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 12, 31, 17, tzinfo=UTC))
    result = csa_rounds.configure_companion_delivery(
        round_id=row.pk,
        actor=active_user,
        companion_delivery_enabled=False,
    )
    assert not result.companion_delivery_enabled
    assert result.companion_delivery_cutoff == datetime(2026, 12, 31, 16, 59, tzinfo=UTC)


def test_round_api_rejects_timestamp_delivery_date(admin_client):
    response = admin_client.post(
        "/api/v1/admin/csa-rounds",
        {
            "name": "No synthetic delivery hour",
            "opens_at": "2026-12-30T09:00:00+07:00",
            "closes_at": "2026-12-31T23:59:00+07:00",
            "delivery_date": "2027-01-01T00:00:00+07:00",
        },
        format="json",
    )
    assert response.status_code == 400
    assert "delivery_date" in response.data


def test_same_delivery_date_rounds_allowed(active_user, monkeypatch):
    monkeypatch.setattr(timezone, "now", lambda: datetime(2026, 12, 29, tzinfo=UTC))
    first = csa_rounds.create_round(
        name="First",
        opens_at=datetime(2026, 12, 30, 1, tzinfo=UTC),
        closes_at=datetime(2026, 12, 30, 2, tzinfo=UTC),
        delivery_date=date(2027, 1, 1),
        actor=active_user,
    )
    second = csa_rounds.create_round(
        name="Second",
        opens_at=datetime(2026, 12, 30, 3, tzinfo=UTC),
        closes_at=datetime(2026, 12, 30, 4, tzinfo=UTC),
        delivery_date=date(2027, 1, 1),
        actor=active_user,
    )
    csa_rounds.publish_round(round_id=first.pk, actor=active_user)
    csa_rounds.publish_round(round_id=second.pk, actor=active_user)
    assert CSARound.objects.filter(published_at__isnull=False).count() == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "instant,expected",
    [
        (datetime(2026, 12, 31, 17, 30, tzinfo=UTC), date(2027, 1, 1)),
        (datetime(2026, 11, 30, 16, 59, tzinfo=UTC), date(2026, 11, 30)),
        (datetime(2026, 11, 30, 17, tzinfo=UTC), date(2026, 12, 1)),
    ],
)
def test_migration_converts_vietnam_day_and_keeps_order_snapshot(instant, expected):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_target = [
        ("memberships", "0037_csaround_companion_delivery_cutoff_and_more"),
        ("commerce", "0009_storeorder_delivery_date_and_more"),
    ]
    try:
        executor.migrate(old_target)
        apps = executor.loader.project_state(old_target).apps
        OldRound = apps.get_model("memberships", "CSARound")
        OldOrder = apps.get_model("commerce", "StoreOrder")
        import uuid

        old = OldRound.objects.create(
            sequence=1,
            name="Old round",
            opens_at=instant - timedelta(days=3),
            closes_at=instant - timedelta(days=2),
            delivery_at=instant,
        )
        snapshot = {"delivery_at": instant.isoformat(), "round_id": str(old.pk)}
        order = OldOrder.objects.create(
            reference_code="LEGACY-CSA-DATE",
            create_idempotency_key=uuid.uuid4(),
            request_fingerprint="legacy",
            locale="vi",
            subtotal_vnd=0,
            shipping_fee_vnd=0,
            total_vnd=0,
            shipping_benefit_snapshot=snapshot,
        )
        MigrationExecutor(connection).migrate(latest)
        row = CSARound.objects.get(pk=old.pk)
        assert row.delivery_date == expected
        from commerce.models import StoreOrder

        assert StoreOrder.objects.get(pk=order.pk).shipping_benefit_snapshot == snapshot
        assert StoreOrder.objects.get(pk=order.pk).delivery_date is None
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("conflict", ["closes_at", "cutoff", "reopen"])
def test_migration_refuses_to_shorten_existing_same_day_deadlines(conflict):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old_target = [
        ("memberships", "0037_csaround_companion_delivery_cutoff_and_more"),
        ("commerce", "0009_storeorder_delivery_date_and_more"),
    ]
    old_round = None
    try:
        executor.migrate(old_target)
        apps = executor.loader.project_state(old_target).apps
        OldRound = apps.get_model("memberships", "CSARound")
        values = {
            "sequence": 1,
            "name": "Conflict",
            "opens_at": datetime(2026, 12, 30, tzinfo=UTC),
            "closes_at": datetime(2026, 12, 31, 12, tzinfo=UTC),
            "delivery_at": datetime(2027, 1, 1, 8, tzinfo=UTC),
        }
        same_day = datetime(2026, 12, 31, 20, tzinfo=UTC)
        if conflict == "closes_at":
            values["closes_at"] = same_day
        elif conflict == "cutoff":
            values["companion_delivery_cutoff"] = same_day
            values["companion_delivery_enabled"] = True
        old_round = OldRound.objects.create(**values)
        if conflict == "reopen":
            # Historical model fixture, no operational membership/identity reads.
            Customer = apps.get_model("memberships", "CSACustomer")
            Membership = apps.get_model("memberships", "Membership")
            # Use existing domain fixture setup in a dedicated isolated old-schema test.
            customer = Customer.objects.create(name="Legacy", phone_e164="+84912345678")
            package = apps.get_model("memberships", "MembershipPackage").objects.create(
                name="Legacy",
                quota_policy="expire",
            )
            option = apps.get_model("memberships", "MembershipPackagePriceOption").objects.create(
                package=package,
                duration_months=1,
                monthly_price_vnd=100,
                active_duration_key=1,
            )
            member = Membership.objects.create(
                customer=customer,
                package=package,
                price_option=option,
                package_name_snapshot="Legacy",
                monthly_price_vnd_snapshot=100,
                total_price_vnd_snapshot=100,
                duration_months_snapshot=1,
                quota_policy_snapshot="expire",
                farmbrite_order_reference="legacy",
                farmbrite_order_reference_key="legacy",
                status="active",
                start_date=date(2026, 12, 1),
                end_date=date(2027, 2, 1),
            )
            apps.get_model("memberships", "CSASelection").objects.create(
                round=old_round,
                membership=member,
                state="reselecting",
                reopen_until=same_day,
            )
        with pytest.raises(RuntimeError, match="CSA delivery_date conversion blocked") as error:
            MigrationExecutor(connection).migrate(latest)
        assert str(old_round.pk) in str(error.value) or "selection=" in str(error.value)
        unchanged = OldRound.objects.get(pk=old_round.pk)
        assert unchanged.delivery_at == values["delivery_at"]
        assert unchanged.closes_at == values["closes_at"]
        assert unchanged.companion_delivery_cutoff == values.get("companion_delivery_cutoff")
    finally:
        if old_round is not None:
            # Remove only disposable test fixture conflicts, then restore schema.
            apps.get_model("memberships", "CSASelection").objects.filter(
                round_id=old_round.pk
            ).delete()
            OldRound.objects.filter(pk=old_round.pk).delete()
        MigrationExecutor(connection).migrate(latest)
