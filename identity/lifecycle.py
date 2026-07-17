import hashlib
import hmac
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests
from django.conf import settings

from identity.models import IdentityEvent

SUPPORTED_LIFECYCLE_EVENTS = {
    "user.activated",
    "user.suspended",
    "user.deletion_requested",
    "user.anonymized",
}


class LifecycleDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LifecycleDeliveryResult:
    event_id: str
    matched: bool


def serialize_event(event: IdentityEvent) -> bytes:
    payload = {
        "id": str(event.pk),
        "type": event.type,
        "occurred_at": event.created_at.isoformat(),
        "data": event.payload,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def sign_lifecycle_payload(*, secret: str, event_id: str, timestamp: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + event_id.encode() + b"." + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def deliver_identity_event(
    event: IdentityEvent,
    *,
    post: Callable = requests.post,
    timestamp: int | None = None,
) -> LifecycleDeliveryResult:
    if event.type not in SUPPORTED_LIFECYCLE_EVENTS:
        raise LifecycleDeliveryError(f"Unsupported lifecycle event: {event.type}")

    url = settings.FLARUM_LIFECYCLE_URL
    secret = settings.FLARUM_LIFECYCLE_SECRET
    if not url:
        raise LifecycleDeliveryError("FLARUM_LIFECYCLE_URL is not configured")
    if len(secret) < 32:
        raise LifecycleDeliveryError("FLARUM_LIFECYCLE_SECRET must contain at least 32 characters")

    event_id = str(event.pk)
    body = serialize_event(event)
    timestamp_value = str(timestamp if timestamp is not None else int(time.time()))
    signature = sign_lifecycle_payload(
        secret=secret,
        event_id=event_id,
        timestamp=timestamp_value,
        body=body,
    )

    try:
        response = post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-NFV-Event-ID": event_id,
                "X-NFV-Timestamp": timestamp_value,
                "X-NFV-Signature": f"sha256={signature}",
            },
            timeout=settings.FLARUM_LIFECYCLE_TIMEOUT,
        )
    except requests.RequestException as error:
        raise LifecycleDeliveryError("Flarum lifecycle endpoint is unavailable") from error

    if not 200 <= response.status_code < 300:
        raise LifecycleDeliveryError(
            f"Flarum lifecycle endpoint returned HTTP {response.status_code}"
        )

    try:
        response_data = response.json()["data"]
    except (KeyError, TypeError, ValueError) as error:
        raise LifecycleDeliveryError("Flarum lifecycle response is invalid") from error

    if response_data.get("event_id") != event_id:
        raise LifecycleDeliveryError("Flarum lifecycle response event ID does not match")

    return LifecycleDeliveryResult(
        event_id=event_id,
        matched=bool(response_data.get("matched", False)),
    )
