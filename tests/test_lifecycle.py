import hashlib
import hmac
import json
import uuid
from io import StringIO
from types import SimpleNamespace

import pytest
import requests
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from identity.lifecycle import LifecycleDeliveryError, deliver_identity_event
from identity.models import EventStatus, IdentityEvent

LIFECYCLE_SETTINGS = {
    "FLARUM_LIFECYCLE_URL": "https://forum.example.test/api/nfv/core-lifecycle",
    "FLARUM_LIFECYCLE_SECRET": "test-secret-with-at-least-thirty-two-characters",
    "FLARUM_LIFECYCLE_TIMEOUT": 7,
    "FLARUM_LIFECYCLE_MAX_ATTEMPTS": 3,
}


def create_event(active_user, event_type="user.suspended", status="suspended"):
    return IdentityEvent.objects.create(
        user=active_user,
        type=event_type,
        payload={"user_id": str(active_user.pk), "status": status},
    )


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_delivery_signs_exact_request_body(active_user):
    event = create_event(active_user)
    captured = {}

    def post(url, **kwargs):
        captured.update(url=url, **kwargs)
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": {"event_id": str(event.pk), "matched": True}},
        )

    result = deliver_identity_event(event, post=post, timestamp=1_700_000_000)

    assert result.matched is True
    assert captured["url"] == LIFECYCLE_SETTINGS["FLARUM_LIFECYCLE_URL"]
    assert captured["timeout"] == 7
    body = captured["data"]
    assert json.loads(body) == {
        "id": str(event.pk),
        "type": "user.suspended",
        "occurred_at": event.created_at.isoformat(),
        "data": {"user_id": str(active_user.pk), "status": "suspended"},
    }
    message = b"1700000000." + str(event.pk).encode() + b"." + body
    expected = hmac.new(
        LIFECYCLE_SETTINGS["FLARUM_LIFECYCLE_SECRET"].encode(),
        message,
        hashlib.sha256,
    ).hexdigest()
    assert captured["headers"]["X-NFV-Signature"] == f"sha256={expected}"


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_delivery_rejects_mismatched_response_event(active_user):
    event = create_event(active_user)

    def post(*args, **kwargs):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"data": {"event_id": str(uuid.uuid4()), "matched": True}},
        )

    with pytest.raises(LifecycleDeliveryError, match="event ID does not match"):
        deliver_identity_event(event, post=post)


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_delivery_hides_transport_details(active_user):
    event = create_event(active_user)

    def post(*args, **kwargs):
        raise requests.ConnectionError("secret upstream details")

    with pytest.raises(LifecycleDeliveryError, match="endpoint is unavailable") as error:
        deliver_identity_event(event, post=post)

    assert "secret upstream details" not in str(error.value)


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_dispatch_command_marks_success(monkeypatch, active_user):
    event = create_event(active_user)
    monkeypatch.setattr(
        "identity.management.commands.dispatch_identity_events.deliver_identity_event",
        lambda pending_event: SimpleNamespace(event_id=str(pending_event.pk), matched=True),
    )

    output = StringIO()
    call_command("dispatch_identity_events", stdout=output)

    event.refresh_from_db()
    assert event.status == EventStatus.PROCESSED
    assert event.attempts == 1
    assert event.processed_at is not None
    assert "Delivered 1 lifecycle event(s); 0 failed." in output.getvalue()


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_dispatch_command_schedules_bounded_retry(monkeypatch, active_user):
    event = create_event(active_user)

    def fail(_event):
        raise LifecycleDeliveryError("temporary failure")

    monkeypatch.setattr(
        "identity.management.commands.dispatch_identity_events.deliver_identity_event",
        fail,
    )

    call_command("dispatch_identity_events", stderr=StringIO())

    event.refresh_from_db()
    assert event.status == EventStatus.FAILED
    assert event.attempts == 1
    assert event.available_at is not None
    assert event.available_at > timezone.now()


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_dispatch_command_does_not_send_unrelated_events(monkeypatch, active_user):
    event = create_event(active_user, "profile.updated", "active")
    delivered = []
    monkeypatch.setattr(
        "identity.management.commands.dispatch_identity_events.deliver_identity_event",
        delivered.append,
    )

    call_command("dispatch_identity_events", stdout=StringIO())

    event.refresh_from_db()
    assert delivered == []
    assert event.status == EventStatus.PENDING
    assert event.attempts == 0


@pytest.mark.django_db
@override_settings(**LIFECYCLE_SETTINGS)
def test_dispatch_command_stops_after_configured_attempts(monkeypatch, active_user):
    event = create_event(active_user)
    event.status = EventStatus.FAILED
    event.attempts = LIFECYCLE_SETTINGS["FLARUM_LIFECYCLE_MAX_ATTEMPTS"]
    event.available_at = None
    event.save(update_fields=["status", "attempts", "available_at", "updated_at"])
    delivered = []
    monkeypatch.setattr(
        "identity.management.commands.dispatch_identity_events.deliver_identity_event",
        delivered.append,
    )

    call_command("dispatch_identity_events", stdout=StringIO())

    event.refresh_from_db()
    assert delivered == []
    assert event.status == EventStatus.FAILED
    assert event.attempts == LIFECYCLE_SETTINGS["FLARUM_LIFECYCLE_MAX_ATTEMPTS"]
