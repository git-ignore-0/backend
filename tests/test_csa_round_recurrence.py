"""Weekly scheduling for CSA rounds."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from memberships import csa_rounds as service
from memberships.management.commands import process_csa_rounds as command
from memberships.models import CSARound, CSARoundRecurrence

pytestmark = pytest.mark.django_db

OPENING = datetime(2027, 1, 4, 1, tzinfo=UTC)
CLOSING = datetime(2027, 1, 9, 15, tzinfo=UTC)
DELIVERY = date(2027, 1, 10)


@pytest.fixture
def clock(monkeypatch):
    current = OPENING
    monkeypatch.setattr(timezone, "now", lambda: current)

    def advance(now):
        nonlocal current
        current = now

    return advance


def create_root(*, actor, mode="draft"):
    return service.create_round(
        name="Weekly CSA",
        opens_at=OPENING,
        closes_at=CLOSING,
        delivery_date=DELIVERY,
        actor=actor,
        recurrence_mode=mode,
    )


@pytest.mark.parametrize(
    ("mode", "due_at"),
    [
        ("draft", datetime(2027, 1, 10, 1, tzinfo=UTC)),
        ("auto", datetime(2027, 1, 11, 1, tzinfo=UTC)),
    ],
)
def test_worker_generates_each_week_once_at_mode_boundary(active_user, clock, mode, due_at):
    root = create_root(actor=active_user, mode=mode)
    assert root.recurrence_schedule.next_create_at is None
    root = service.publish_round(round_id=root.pk, actor=active_user)
    assert root.recurrence_schedule.next_create_at == due_at

    clock(due_at - timedelta(microseconds=1))
    call_command("process_csa_rounds")
    assert CSARound.objects.count() == 1

    clock(due_at)
    service.process_due_rounds(user=active_user)
    assert CSARound.objects.count() == 1
    call_command("process_csa_rounds")
    call_command("process_csa_rounds")
    generated = CSARound.objects.exclude(pk=root.pk).get()
    assert generated.recurrence.root_round_id == root.pk
    assert generated.recurrence_week == 1
    assert generated.opens_at == datetime(2027, 1, 11, 1, tzinfo=UTC)
    assert generated.closes_at == datetime(2027, 1, 16, 15, tzinfo=UTC)
    assert generated.delivery_date == date(2027, 1, 17)
    assert generated.published_at == (due_at if mode == "auto" else None)
    assert generated.recurrence.next_create_at == due_at + timedelta(days=7)


def test_activation_uses_last_draft_edit_and_future_rounds_use_frozen_anchor(active_user, clock):
    root = create_root(actor=active_user, mode=None)
    service.update_round(round_id=root.pk, actor=active_user, recurrence_mode="auto")
    service.update_round(round_id=root.pk, actor=active_user, recurrence_mode=None)
    assert not hasattr(CSARound.objects.get(pk=root.pk), "recurrence_schedule")
    service.update_round(round_id=root.pk, actor=active_user, recurrence_mode="draft")
    service.update_round(
        round_id=root.pk,
        actor=active_user,
        name="CSA at publication",
        opens_at=OPENING + timedelta(hours=3),
        closes_at=CLOSING + timedelta(hours=1),
    )
    service.configure_companion_delivery(
        round_id=root.pk,
        actor=active_user,
        companion_delivery_enabled=True,
        companion_delivery_cutoff=CLOSING,
    )
    service.publish_round(round_id=root.pk, actor=active_user)
    service.update_round(
        round_id=root.pk,
        actor=active_user,
        name="Renamed",
        opens_at=OPENING - timedelta(hours=2),
        closes_at=CLOSING - timedelta(hours=2),
    )
    service.publish_round(round_id=root.pk, actor=active_user)
    service.cancel_round(round_id=root.pk, actor=active_user, reason="Rain")

    clock(datetime(2027, 1, 10, 4, tzinfo=UTC))
    call_command("process_csa_rounds")
    generated = CSARound.objects.exclude(pk=root.pk).get()
    assert generated.name == "CSA at publication"
    assert generated.opens_at == datetime(2027, 1, 11, 4, tzinfo=UTC)
    assert generated.closes_at == datetime(2027, 1, 16, 16, tzinfo=UTC)
    assert generated.companion_delivery_enabled is False
    assert generated.companion_delivery_cutoff is None
    with pytest.raises(service.CSARoundError, match="csa_recurrence_immutable"):
        service.update_round(round_id=generated.pk, actor=active_user, recurrence_mode="auto")


@pytest.mark.parametrize("mode", ["draft", "auto"])
def test_worker_skips_closed_weeks_and_catches_up_before_close(active_user, clock, mode):
    root = create_root(actor=active_user, mode=mode)
    service.publish_round(round_id=root.pk, actor=active_user)
    clock(datetime(2027, 1, 23, 15, tzinfo=UTC))
    call_command("process_csa_rounds")
    recurrence = CSARound.objects.get(pk=root.pk).recurrence_schedule
    assert not recurrence.generated_rounds.exists()
    assert recurrence.next_week == 3

    clock(datetime(2027, 1, 30, 14, 59, 59, tzinfo=UTC))
    call_command("process_csa_rounds")
    generated = recurrence.generated_rounds.get()
    assert generated.recurrence_week == 3
    assert generated.delivery_date == date(2027, 1, 31)
    if mode == "auto":
        assert generated.published_at == datetime(2027, 1, 30, 14, 59, 59, tzinfo=UTC)
    else:
        assert generated.published_at is None


@pytest.mark.parametrize("opening", [OPENING, datetime(9999, 12, 28, 1, tzinfo=UTC)])
def test_publish_rejects_recurrence_when_the_first_week_cannot_fit(active_user, clock, opening):
    root = service.create_round(
        name="Upper date boundary",
        opens_at=opening,
        closes_at=datetime(9999, 12, 30, 1, tzinfo=UTC),
        delivery_date=date.max,
        actor=active_user,
        recurrence_mode="auto",
    )

    with pytest.raises(service.CSARoundError, match="csa_round_schedule_invalid"):
        service.publish_round(round_id=root.pk, actor=active_user)

    root.refresh_from_db()
    assert root.published_at is None
    assert root.recurrence_schedule.next_create_at is None


def test_worker_stops_exhausted_dates_and_still_generates_other_schedules(active_user, clock):
    legacy = create_root(actor=active_user, mode="auto")
    last_week = create_root(actor=active_user, mode="auto")
    normal = create_root(actor=active_user, mode="auto")
    for round_ in (legacy, last_week, normal):
        service.publish_round(round_id=round_.pk, actor=active_user)
    # Existing active schedules may have been saved before date-bound validation.
    CSARoundRecurrence.objects.filter(root_round=legacy).update(anchor_delivery_date=date.max)
    CSARoundRecurrence.objects.filter(root_round=last_week).update(
        anchor_delivery_date=date.max - timedelta(weeks=1)
    )
    due_at = OPENING + timedelta(weeks=1)
    clock(due_at)

    call_command("process_csa_rounds")
    call_command("process_csa_rounds")

    schedules = {
        schedule.root_round_id: schedule
        for schedule in CSARoundRecurrence.objects.filter(
            root_round__in=[legacy, last_week, normal]
        )
    }
    assert not schedules[legacy.pk].generated_rounds.exists()
    assert schedules[last_week.pk].generated_rounds.get().delivery_date == date.max
    for pk in (legacy.pk, last_week.pk):
        assert schedules[pk].stopped_at == due_at
        assert schedules[pk].next_create_at is None
    assert schedules[normal.pk].generated_rounds.count() == 1
    assert schedules[normal.pk].next_create_at == due_at + timedelta(weeks=1)


def test_worker_reports_failed_schedule_and_processes_the_rest(active_user, clock, monkeypatch):
    failing = create_root(actor=active_user, mode="auto")
    healthy = create_root(actor=active_user, mode="auto")
    for round_ in (failing, healthy):
        service.publish_round(round_id=round_.pk, actor=active_user)
    due_at = OPENING + timedelta(weeks=1)
    clock(due_at)
    create = service._create_round

    def create_with_failure(**kwargs):
        if kwargs["recurrence"].root_round_id == failing.pk:
            raise IntegrityError("simulated insert failure")
        return create(**kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(service, "_create_round", create_with_failure)
        with pytest.raises(CommandError, match="1 CSA recurrence"):
            call_command("process_csa_rounds")

    failed_schedule = CSARoundRecurrence.objects.get(root_round=failing)
    assert failed_schedule.next_create_at == due_at
    assert not failed_schedule.generated_rounds.exists()
    assert healthy.recurrence_schedule.generated_rounds.count() == 1
    call_command("process_csa_rounds")
    assert failed_schedule.generated_rounds.count() == 1
    assert healthy.recurrence_schedule.generated_rounds.count() == 1


def test_reconciliation_failure_does_not_prevent_an_opening(active_user, clock, monkeypatch):
    root = create_root(actor=active_user, mode="auto")
    service.publish_round(round_id=root.pk, actor=active_user)
    due_at = OPENING + timedelta(weeks=1)
    clock(due_at)

    def fail_reconciliation():
        raise IntegrityError("simulated reconciliation failure")

    monkeypatch.setattr(command, "process_due_rounds", fail_reconciliation)
    with pytest.raises(CommandError, match="CSA selection reconciliation"):
        call_command("process_csa_rounds")

    assert root.recurrence_schedule.generated_rounds.get().published_at == due_at


def test_worker_skips_a_round_that_closes_while_waiting_for_sequence(
    active_user, clock, monkeypatch
):
    root = create_root(actor=active_user, mode="auto")
    service.publish_round(round_id=root.pk, actor=active_user)
    cutoff = CLOSING + timedelta(weeks=1)
    clock(cutoff - timedelta(seconds=1))
    allocate = service._next_round_sequence

    def allocate_after_cutoff():
        clock(cutoff)
        return allocate()

    monkeypatch.setattr(service, "_next_round_sequence", allocate_after_cutoff)
    call_command("process_csa_rounds")

    schedule = CSARoundRecurrence.objects.get(root_round=root)
    assert not schedule.generated_rounds.exists()
    assert schedule.next_week == 2
    assert schedule.next_create_at == OPENING + timedelta(weeks=2)


def test_worker_bounds_generation_per_schedule_and_resumes_next_run(active_user, clock):
    root = create_root(actor=active_user, mode="auto")
    service.update_round(
        round_id=root.pk,
        actor=active_user,
        closes_at=datetime(2027, 3, 31, 15, tzinfo=UTC),
        delivery_date=date(2027, 4, 1),
    )
    root = service.publish_round(round_id=root.pk, actor=active_user)
    clock(datetime(2027, 3, 15, 1, tzinfo=UTC))

    with CaptureQueriesContext(connection) as queries:
        call_command("process_csa_rounds")
    sequence_lookups = [
        query["sql"]
        for query in queries
        if "memberships_csaround" in query["sql"] and "DESC LIMIT 1" in query["sql"]
    ]
    assert len(sequence_lookups) == 1
    schedule = root.recurrence_schedule
    assert list(schedule.generated_rounds.values_list("recurrence_week", flat=True)) == list(
        range(1, 9)
    )
    assert schedule.next_create_at == datetime(2027, 3, 8, 1, tzinfo=UTC)

    call_command("process_csa_rounds")
    schedule.refresh_from_db()
    assert list(schedule.generated_rounds.values_list("recurrence_week", flat=True)) == list(
        range(1, 11)
    )
    assert schedule.next_create_at == datetime(2027, 3, 22, 1, tzinfo=UTC)


def test_stop_is_idempotent_and_preserves_existing_rounds(active_user, clock):
    root = create_root(actor=active_user, mode="auto")
    with pytest.raises(service.CSARoundError, match="csa_recurrence_not_active"):
        service.stop_round_recurrence(round_id=root.pk)
    service.publish_round(round_id=root.pk, actor=active_user)
    with pytest.raises(service.CSARoundError, match="csa_recurrence_immutable"):
        service.update_round(round_id=root.pk, actor=active_user, recurrence_mode=None)

    clock(datetime(2027, 1, 11, 1, tzinfo=UTC))
    call_command("process_csa_rounds")
    stopped = service.stop_round_recurrence(round_id=root.pk)
    assert stopped.recurrence_schedule.next_create_at is None
    assert stopped.recurrence_schedule.stopped_at == datetime(2027, 1, 11, 1, tzinfo=UTC)

    clock(datetime(2027, 1, 18, 1, tzinfo=UTC))
    service.stop_round_recurrence(round_id=root.pk)
    call_command("process_csa_rounds")
    recurrence = CSARound.objects.get(pk=root.pk).recurrence_schedule
    assert recurrence.stopped_at == stopped.recurrence_schedule.stopped_at
    assert recurrence.generated_rounds.get().published_at == datetime(2027, 1, 11, 1, tzinfo=UTC)


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="Row-lock concurrency requires a database with SELECT FOR UPDATE",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_workers_generate_one_round(active_user, clock):
    root = create_root(actor=active_user, mode="auto")
    service.publish_round(round_id=root.pk, actor=active_user)
    clock(datetime(2027, 1, 11, 1, tzinfo=UTC))
    barrier = Barrier(2)

    def run_worker():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            call_command("process_csa_rounds")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(run_worker) for _ in range(2)]
        for result in results:
            result.result(timeout=15)

    assert CSARound.objects.filter(recurrence__root_round=root).get().recurrence_week == 1
