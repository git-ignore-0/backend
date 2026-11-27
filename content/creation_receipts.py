"""Persist intent fingerprints and outcomes for keyed Admin resource creation."""

from hashlib import sha256
from json import dumps

from django.db import transaction

from content.models import AdminCreationReceipt


class CreationIdempotencyConflict(ValueError):
    pass


def find_creation_receipt(*, actor, action, key):
    return AdminCreationReceipt.objects.filter(actor=actor, action=action, key=key).first()


@transaction.atomic
def create_with_receipt(*, actor, action, key, payload, create):
    material = dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    fingerprint = sha256(material.encode()).hexdigest()
    receipt, created = AdminCreationReceipt.objects.get_or_create(
        actor=actor,
        action=action,
        key=key,
        defaults={"request_fingerprint": fingerprint},
    )
    if not created:
        if receipt.request_fingerprint != fingerprint:
            raise CreationIdempotencyConflict
        return receipt, False

    receipt.resource_id = create().pk
    receipt.save(update_fields=["resource_id"])
    return receipt, True


def creation_receipt_meta(receipt, *, replayed):
    return {
        "idempotent_replay": replayed,
        "action_receipt": {
            "action": receipt.action,
            "resource_id": str(receipt.resource_id),
            "idempotency_key": str(receipt.key),
        },
    }
