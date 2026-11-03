"""Transactional voucher ledger. Coupon locks follow existing source/inventory locks.

Never lock an existing source, identity, catalog or inventory after taking a coupon lock.
All consumers and releasers serialize on the same coupon row, then its usage row.
"""

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from memberships.coupons import check_coupon_customer
from memberships.models import CouponUsage, CSACoupon


def lock_coupons(coupon_ids):
    """Caller holds source locks and an atomic block; acquire the whole set in PK order.

    Re-locking a row already in this set is safe. Do not acquire another coupon
    later unless it is already in the set: that would reintroduce lock inversion.
    """
    return list(
        CSACoupon.objects.select_for_update()
        .filter(pk__in={value for value in coupon_ids if value is not None})
        .order_by("pk")
    )


@transaction.atomic
def consume_coupon(*, coupon, user, purchase_request=None, store_order=None):
    if (purchase_request is None) == (store_order is None):
        raise ValueError("Exactly one voucher source is required")
    coupon = CSACoupon.objects.select_for_update().get(pk=coupon.pk)
    check_coupon_customer(coupon=coupon, user=user)
    if coupon.used_count >= coupon.usage_limit:
        from memberships.coupons import CouponError

        raise CouponError("coupon_usage_exhausted")
    source = purchase_request if purchase_request is not None else store_order
    owner_id = source.user_id if purchase_request is not None else source.buyer_user_id
    if source.coupon_id != coupon.pk or owner_id != user.pk:
        raise ValueError("Voucher source and owner must match")
    usage = CouponUsage.objects.create(
        coupon=coupon, user=user, purchase_request=purchase_request, store_order=store_order
    )
    # Only the locked counter changes. Live CSA catalog references are irrelevant
    # to this Store/CSA consumption; DB constraints still enforce the count bounds.
    CSACoupon.objects.filter(pk=coupon.pk).update(
        used_count=F("used_count") + 1, updated_at=timezone.now()
    )
    return usage


@transaction.atomic
def release_coupon(*, purchase_request=None, store_order=None, reason):
    if (purchase_request is None) == (store_order is None):
        raise ValueError("Exactly one voucher source is required")
    source = (
        {"purchase_request": purchase_request}
        if purchase_request is not None
        else {"store_order": store_order}
    )
    usage = CouponUsage.objects.filter(**source).first()
    if usage is None:
        return False
    coupon = CSACoupon.objects.select_for_update().get(pk=usage.coupon_id)
    usage = CouponUsage.objects.select_for_update().get(pk=usage.pk)
    if usage.released_at is not None:
        return False
    if coupon.used_count < 1:
        raise ValueError("Voucher usage counter is inconsistent")
    usage.active_slot = None
    usage.released_at = timezone.now()
    usage.release_reason = reason
    usage.save(update_fields=["active_slot", "released_at", "release_reason"])
    CSACoupon.objects.filter(pk=coupon.pk).update(
        used_count=F("used_count") - 1, updated_at=timezone.now()
    )
    return True
