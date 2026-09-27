"""Validated administration of CSA coupon configurations."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from memberships.coupons import CouponError, normalize_coupon_code
from memberships.models import CSACoupon, MembershipPackage, MembershipPackagePriceOption

SCOPE_FIELDS = ("package_ids", "price_option_ids", "payment_types", "installment_counts")
LOCKED_AFTER_USE = (
    "code",
    "discount_type",
    "discount_value",
    "starts_at",
    "expires_at",
    "minimum_amount",
    "maximum_discount_amount",
    *SCOPE_FIELDS,
)


def coupon_status(coupon, *, now=None):
    now = now or timezone.now()
    if not coupon.is_active:
        return "inactive"
    if now < coupon.starts_at:
        return "scheduled"
    if now >= coupon.expires_at:
        return "expired"
    if coupon.used_count >= coupon.usage_limit:
        return "exhausted"
    return "active"


def _scope_ids(coupon, plural, legacy):
    values = getattr(coupon, plural)
    if values:
        return values
    old_id = getattr(coupon, f"{legacy}_id", None)
    return [str(old_id)] if old_id else []


def coupon_scope(coupon):
    return {
        "package_ids": _scope_ids(coupon, "package_ids", "package"),
        "price_option_ids": _scope_ids(coupon, "price_option_ids", "price_option"),
        "payment_types": coupon.payment_types
        or ([coupon.payment_type] if coupon.payment_type else []),
        "installment_counts": coupon.installment_counts
        or ([coupon.installment_count] if coupon.installment_count else []),
    }


def coupon_payload(coupon, *, packages=None, options=None):
    scope = coupon_scope(coupon)
    packages = (
        packages
        if packages is not None
        else {
            str(item.pk): item.name
            for item in MembershipPackage.objects.filter(pk__in=scope["package_ids"])
        }
    )
    options = (
        options
        if options is not None
        else {
            str(item.pk): item
            for item in MembershipPackagePriceOption.objects.filter(
                pk__in=scope["price_option_ids"]
            )
        }
    )
    return {
        "id": str(coupon.pk),
        "code": coupon.code,
        "discount_type": coupon.discount_type,
        "discount_value": format(coupon.discount_value, "f"),
        "usage_limit": coupon.usage_limit,
        "used_count": coupon.used_count,
        "starts_at": coupon.starts_at.isoformat(),
        "expires_at": coupon.expires_at.isoformat(),
        "is_active": coupon.is_active,
        "status": coupon_status(coupon),
        **scope,
        "minimum_amount": format(coupon.minimum_amount, "f")
        if coupon.minimum_amount is not None
        else None,
        "maximum_discount_amount": format(coupon.maximum_discount_amount, "f")
        if coupon.maximum_discount_amount is not None
        else None,
        "scope": {
            "packages": [
                {"id": value, "name": packages[value]}
                for value in scope["package_ids"]
                if value in packages
            ],
            "price_options": [
                {
                    "id": value,
                    "package_id": str(options[value].package_id),
                    "duration_months": options[value].duration_months,
                }
                for value in scope["price_option_ids"]
                if value in options
            ],
            "payment_types": scope["payment_types"],
            "installment_counts": scope["installment_counts"],
        },
        "created_at": coupon.created_at.isoformat(),
        "updated_at": coupon.updated_at.isoformat(),
    }


def coupon_payloads(coupons):
    rows = list(coupons)
    scopes = [coupon_scope(coupon) for coupon in rows]
    package_ids = {value for scope in scopes for value in scope["package_ids"]}
    option_ids = {value for scope in scopes for value in scope["price_option_ids"]}
    packages = {
        str(item.pk): item.name for item in MembershipPackage.objects.filter(pk__in=package_ids)
    }
    options = {
        str(item.pk): item
        for item in MembershipPackagePriceOption.objects.filter(pk__in=option_ids)
    }
    return [coupon_payload(coupon, packages=packages, options=options) for coupon in rows]


def validate_coupon_configuration(coupon, *, activating=False):
    if coupon.discount_type not in CSACoupon.DiscountType.values:
        raise CouponError("coupon_invalid_type")
    value = Decimal(str(coupon.discount_value))
    if (
        value <= 0
        or (coupon.discount_type == "percent" and value > 100)
        or (coupon.discount_type == "fixed" and value != value.to_integral_value())
    ):
        raise CouponError("coupon_invalid_value")
    if coupon.maximum_discount_amount is not None and (
        coupon.discount_type != "percent" or coupon.maximum_discount_amount <= 0
    ):
        raise CouponError("coupon_invalid_value")
    if coupon.usage_limit < 1 or coupon.usage_limit < coupon.used_count:
        raise CouponError("coupon_invalid_usage_limit")
    if not coupon.starts_at or not coupon.expires_at or coupon.expires_at <= coupon.starts_at:
        raise CouponError("coupon_invalid_date_range")
    scope = coupon_scope(coupon)
    for field in SCOPE_FIELDS:
        if len(scope[field]) != len(set(scope[field])):
            raise CouponError("coupon_not_applicable")
    if any(value not in {"full", "installment"} for value in scope["payment_types"]):
        raise CouponError("coupon_not_applicable")
    counts = scope["installment_counts"]
    if any(
        not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 120
        for value in counts
    ):
        raise CouponError("coupon_not_applicable")
    if scope["payment_types"] == ["full"] and any(value != 1 for value in counts):
        raise CouponError("coupon_not_applicable")
    if scope["payment_types"] == ["installment"] and any(value < 2 for value in counts):
        raise CouponError("coupon_not_applicable")
    if coupon.minimum_amount is not None and coupon.minimum_amount <= 0:
        raise CouponError("coupon_invalid_value")
    package_ids = scope["package_ids"]
    option_ids = scope["price_option_ids"]
    packages = list(MembershipPackage.objects.filter(pk__in=package_ids))
    options = list(MembershipPackagePriceOption.objects.filter(pk__in=option_ids))
    if len(packages) != len(package_ids) or len(options) != len(option_ids):
        raise CouponError("coupon_not_applicable")
    if package_ids and any(str(option.package_id) not in package_ids for option in options):
        raise CouponError("coupon_not_applicable")
    if (
        options
        and counts
        and any(count > option.duration_months for count in counts for option in options)
    ):
        raise CouponError("coupon_not_applicable")
    if activating:
        if timezone.now() >= coupon.expires_at or coupon.used_count >= coupon.usage_limit:
            raise CouponError("coupon_cannot_activate")
        if any(not item.is_active for item in packages) or any(
            not item.is_active for item in options
        ):
            raise CouponError("coupon_cannot_activate")


@transaction.atomic
def save_admin_coupon(*, data, coupon_id=None):
    if coupon_id is None:
        coupon = CSACoupon()
    else:
        try:
            coupon = CSACoupon.objects.select_for_update().get(pk=coupon_id)
        except CSACoupon.DoesNotExist as error:
            raise CouponError("coupon_not_found") from error
    values = dict(data)
    if "code" in values:
        values["code"] = normalize_coupon_code(values["code"])
    if "discount_type" in values and values["discount_type"] not in CSACoupon.DiscountType.values:
        raise CouponError("coupon_invalid_type")
    if "usage_limit" in values and (
        values["usage_limit"] < 1 or values["usage_limit"] < coupon.used_count
    ):
        raise CouponError("coupon_invalid_usage_limit")
    for field in SCOPE_FIELDS[:2]:
        if field in values:
            values[field] = [str(value) for value in values[field]]
    if coupon.used_count:
        current = {
            **coupon_scope(coupon),
            **{
                field: getattr(coupon, field)
                for field in LOCKED_AFTER_USE
                if field not in SCOPE_FIELDS
            },
        }
        if any(field in values and values[field] != current[field] for field in LOCKED_AFTER_USE):
            raise CouponError("coupon_cannot_edit_used")
    if (
        "code" in values
        and CSACoupon.objects.filter(code__iexact=values["code"]).exclude(pk=coupon.pk).exists()
    ):
        raise CouponError("coupon_duplicate_code")
    for field, value in values.items():
        setattr(coupon, field, value)
    # Admin list scopes replace legacy single-value scopes only when a new array is submitted.
    for field, legacy in (
        ("package_ids", "package"),
        ("price_option_ids", "price_option"),
        ("payment_types", "payment_type"),
        ("installment_counts", "installment_count"),
    ):
        if field in values:
            setattr(coupon, legacy, None)
    validate_coupon_configuration(coupon)
    try:
        with transaction.atomic():
            coupon.save()
    except IntegrityError as error:
        raise CouponError("coupon_duplicate_code") from error
    except ValidationError as error:
        raise CouponError("coupon_invalid") from error
    return coupon


@transaction.atomic
def set_admin_coupon_active(*, coupon_id, active):
    try:
        coupon = CSACoupon.objects.select_for_update().get(pk=coupon_id)
    except CSACoupon.DoesNotExist as error:
        raise CouponError("coupon_not_found") from error
    if coupon.is_active == active:
        if active:
            raise CouponError("coupon_already_active")
        return coupon
    if active:
        try:
            validate_coupon_configuration(coupon, activating=True)
            coupon.full_clean()
        except ValidationError as error:
            raise CouponError("coupon_cannot_activate") from error
    coupon.is_active = active
    coupon.save(update_fields=["is_active", "updated_at"])
    return coupon
