"""Persist intent fingerprints and outcomes for keyed Admin resource creation."""

from hashlib import sha256
from json import dumps

from django.db import transaction

from content.models import AdminCreationReceipt


class CreationIdempotencyConflict(ValueError):
    pass


def find_creation_receipt(*, actor, action, key):
    return AdminCreationReceipt.objects.filter(actor=actor, action=action, key=key).first()


def creation_fingerprint(payload):
    material = dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(material.encode()).hexdigest()


def claim_creation_receipt(*, actor, action, key, fingerprint):
    """Claim inside the caller's atomic transaction; complete before it commits.

    get_or_create's unique constraint waits for an in-flight identical key. Any
    failure in the caller must roll back this claim together with its writes.
    """
    receipt, created = AdminCreationReceipt.objects.get_or_create(
        actor=actor,
        action=action,
        key=key,
        defaults={"request_fingerprint": fingerprint},
    )
    if receipt.request_fingerprint != fingerprint:
        raise CreationIdempotencyConflict
    return receipt, created


@transaction.atomic
def create_with_receipt(*, actor, action, key, payload, create):
    receipt, created = claim_creation_receipt(
        actor=actor, action=action, key=key, fingerprint=creation_fingerprint(payload)
    )
    if created:
        receipt.resource_id = create().pk
        receipt.save(update_fields=["resource_id"])
    return receipt, created


def creation_receipt_meta(receipt, *, replayed):
    return {
        "idempotent_replay": replayed,
        "action_receipt": {
            "action": receipt.action,
            "resource_id": str(receipt.resource_id),
            "idempotency_key": str(receipt.key),
        },
    }
