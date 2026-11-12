"""Shared CSA response snapshots for member, admin and Store APIs."""

from django.utils import timezone

from memberships import csa_rounds as domain


def round_payload(row):
    return {
        "id": str(row.pk),
        "sequence": row.sequence,
        "name": row.name,
        "opens_at": row.opens_at.isoformat(),
        "closes_at": row.closes_at.isoformat(),
        "delivery_at": row.delivery_at.isoformat(),
        "status": domain.round_status(row),
        "server_time": timezone.now().isoformat(),
        "cancellation_reason": row.cancellation_reason or None,
        "cancelled_by_id": str(row.cancelled_by_id) if row.cancelled_by_id else None,
    }


def event_payload(event):
    return {
        "action": event.action,
        "context": event.context,
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "created_at": event.created_at.isoformat(),
    }


def selection_payload(row, *, admin=False):
    result = {
        "id": str(row.pk),
        "round_id": str(row.round_id),
        "membership_id": str(row.membership_id),
        "state": row.state,
        "revision": row.revision,
        "order_id": str(row.order_id) if row.order_id else None,
        "reopen_until": row.reopen_until.isoformat() if row.reopen_until else None,
        "lines": [
            {"product_id": str(line.product_id), "quantity": line.quantity, **line.snapshot}
            for line in row.lines.all()
        ],
        "server_time": timezone.now().isoformat(),
    }
    if admin:
        result["customer_id"] = str(row.membership.customer_id)
        result["customer_name"] = row.membership.customer.name
    return result
