import calendar
import re
import secrets
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from identity.models import User
from memberships.banks import BankDirectoryUnavailable, find_transfer_bank
from memberships.models import (
    OPEN_REQUEST_STATUSES,
    Membership,
    MembershipCycle,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPaymentInstruction,
    MembershipPaymentSettings,
    MembershipProductSnapshot,
    MembershipRequest,
    MembershipRequestEvent,
    MembershipRequestEventAction,
    MembershipRequestItemSnapshot,
    MembershipRequestStatus,
    MembershipStatus,
    MembershipUsageAllocation,
    MembershipUsageLine,
    MembershipUsageOrder,
    MembershipUsageReversal,
    QuotaPolicy,
    UsageOrderStatus,
)

SHORT_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


class MembershipError(ValueError):
    code = "invalid_membership_operation"


class PackageUnavailable(MembershipError):
    code = "package_unavailable"


class OpenRequestExists(MembershipError):
    code = "open_request_exists"


class ActiveMembershipExists(MembershipError):
    code = "active_membership_exists"


class PaymentSettingsInvalid(MembershipError):
    code = "payment_settings_invalid"


class DirectTransferDisabled(MembershipError):
    code = "direct_transfer_disabled"


class PhoneRequired(MembershipError):
    code = "phone_required"


class StartDateInPast(MembershipError):
    code = "start_date_in_past"


class RequestTransitionConflict(MembershipError):
    code = "request_transition_conflict"


class QuotaError(MembershipError):
    code = "invalid_quota_usage"


class ProductNotInMembership(QuotaError):
    code = "product_not_in_membership"


class InsufficientQuota(QuotaError):
    code = "insufficient_quota"


class UsageIdempotencyConflict(QuotaError):
    code = "usage_idempotency_conflict"


class UsageUndoConflict(QuotaError):
    code = "usage_undo_conflict"


class UsageIntegrityConflict(QuotaError):
    code = "usage_integrity_conflict"


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _short_code() -> str:
    return "".join(secrets.choice(SHORT_CODE_ALPHABET) for _ in range(8))


def _validated_payment_settings(*, require_direct_transfer=False) -> MembershipPaymentSettings:
    settings = MembershipPaymentSettings.objects.filter(pk="default").first()
    if require_direct_transfer and (not settings or not settings.direct_transfer_enabled):
        raise DirectTransferDisabled("Direct transfer is disabled")
    if not settings:
        raise PaymentSettingsInvalid("Membership payment settings are missing")
    values = (settings.bank_bin, settings.bank_name, settings.account_number, settings.account_name)
    if any(not value.strip() for value in values):
        raise PaymentSettingsInvalid("Membership payment settings are incomplete")
    if not re.fullmatch(r"\d{6,19}", settings.account_number):
        raise PaymentSettingsInvalid("Account number is invalid")
    try:
        bank = find_transfer_bank(settings.bank_bin)
    except BankDirectoryUnavailable as error:
        raise PaymentSettingsInvalid("Bank directory is unavailable") from error
    if not bank:
        raise PaymentSettingsInvalid("Bank BIN is invalid")
    if settings.bank_name.strip() != bank["name"].strip():
        raise PaymentSettingsInvalid("Bank name does not match the bank directory")
    return settings


def direct_transfer_is_available() -> bool:
    try:
        _validated_payment_settings(require_direct_transfer=True)
    except (DirectTransferDisabled, PaymentSettingsInvalid):
        return False
    return True


def _request_phone(user, supplied_phone: str | None) -> str:
    from memberships.validation import normalize_membership_phone

    profile = getattr(user, "member_profile", None)
    phone = supplied_phone if supplied_phone is not None else (
        profile.phone_number if profile else ""
    )
    try:
        return normalize_membership_phone(phone)
    except ValueError as error:
        raise PhoneRequired("A valid phone number is required for CSA requests") from error


def _record_request_event(*, request, action, actor, reason=""):
    return MembershipRequestEvent.objects.create(
        request=request,
        action=action,
        actor=actor,
        reason=reason.strip(),
    )


def _snapshot_package(request: MembershipRequest, package: MembershipPackage) -> None:
    items = list(package.items.select_related("product").order_by("id"))
    if not items:
        raise PackageUnavailable("Package has no quota items")
    if any(not item.product.is_active for item in items):
        raise PackageUnavailable("Package contains an inactive product")
    MembershipRequestItemSnapshot.objects.bulk_create(
        [
            MembershipRequestItemSnapshot(
                request=request,
                product=item.product,
                product_name=item.product.name,
                unit_size=item.product.unit_size,
                unit_label_vi=item.product.unit_label_vi,
                unit_label_en=item.product.unit_label_en,
                quota_units_per_cycle=item.quota_units,
            )
            for item in items
        ]
    )


def _create_instruction(
    *, request: MembershipRequest, actor=None, require_direct_transfer=False
):
    if hasattr(request, "payment_instruction"):
        return request.payment_instruction, False
    settings = _validated_payment_settings(require_direct_transfer=require_direct_transfer)
    phone = request.phone_snapshot
    if not phone:
        phone = _request_phone(request.user, None)
    instruction = MembershipPaymentInstruction.objects.create(
        request=request,
        bank_code="",
        bank_bin=settings.bank_bin,
        bank_name=settings.bank_name,
        account_number=settings.account_number.strip(),
        account_name=settings.account_name.strip(),
        amount=request.upfront_price_snapshot,
        phone_snapshot=phone,
        transfer_content=f"{phone}-{request.short_code}",
        issued_by=actor,
    )
    return instruction, True


@transaction.atomic
def create_membership_request(*, user, package_id, flow: str, phone: str | None = None):
    User.objects.select_for_update().get(pk=user.pk)
    phone_snapshot = _request_phone(user, phone)
    today = timezone.localdate()
    Membership.objects.filter(
        user=user,
        current_slot=True,
        end_date__lte=today,
    ).update(status=MembershipStatus.ENDED, current_slot=None)
    if Membership.objects.filter(user=user, current_slot=True).exists():
        raise ActiveMembershipExists("User already has a scheduled or active membership")
    if MembershipRequest.objects.filter(user=user, open_slot=True).exists():
        raise OpenRequestExists("User already has an open membership request")
    try:
        package = MembershipPackage.objects.select_for_update().get(pk=package_id, is_active=True)
    except MembershipPackage.DoesNotExist as error:
        raise PackageUnavailable("Package does not exist or is inactive") from error
    if flow not in {"consultation", "direct_transfer"}:
        raise RequestTransitionConflict("Invalid request flow")
    for _attempt in range(20):
        try:
            with transaction.atomic():
                request = MembershipRequest.objects.create(
                    short_code=_short_code(),
                    user=user,
                    package=package,
                    package_name_snapshot=package.name,
                    package_description_snapshot=package.description,
                    upfront_price_snapshot=package.upfront_price,
                    duration_months_snapshot=package.duration_months,
                    quota_policy_snapshot=package.quota_policy,
                    phone_snapshot=phone_snapshot,
                    status=(
                        MembershipRequestStatus.CONSULTATION_REQUESTED
                        if flow == "consultation"
                        else MembershipRequestStatus.PAYMENT_PENDING
                    ),
                )
            break
        except IntegrityError:
            if MembershipRequest.objects.filter(user=user, open_slot=True).exists():
                raise OpenRequestExists("User already has an open membership request") from None
    else:
        raise RuntimeError("Could not allocate a membership request short code")
    _snapshot_package(request, package)
    _record_request_event(
        request=request,
        action=MembershipRequestEventAction.REQUEST_CREATED,
        actor=user,
    )
    instruction = None
    if flow == "direct_transfer":
        instruction, _created = _create_instruction(
            request=request,
            require_direct_transfer=True,
        )
        _record_request_event(
            request=request,
            action=MembershipRequestEventAction.PAYMENT_INSTRUCTION_ISSUED,
            actor=user,
        )
    return request, instruction


def _locked_request(request_id) -> MembershipRequest:
    return (
        MembershipRequest.objects.select_for_update()
        .select_related("user", "user__member_profile", "package")
        .get(pk=request_id)
    )


@transaction.atomic
def mark_contacted(*, request_id, actor):
    item = _locked_request(request_id)
    if item.status != MembershipRequestStatus.CONSULTATION_REQUESTED:
        raise RequestTransitionConflict("Only consultation requests can be marked contacted")
    item.status = MembershipRequestStatus.CONTACTED
    item.contacted_by = actor
    item.contacted_at = timezone.now()
    item.save(update_fields=["status", "contacted_by", "contacted_at", "updated_at"])
    _record_request_event(
        request=item,
        action=MembershipRequestEventAction.CONTACTED,
        actor=actor,
    )
    return item


@transaction.atomic
def issue_payment_instruction(*, request_id, actor):
    item = _locked_request(request_id)
    if item.status not in {
        MembershipRequestStatus.CONSULTATION_REQUESTED,
        MembershipRequestStatus.CONTACTED,
    }:
        raise RequestTransitionConflict("Payment instructions cannot be issued in this state")
    instruction, _created = _create_instruction(request=item, actor=actor)
    item.status = MembershipRequestStatus.PAYMENT_PENDING
    item.save(update_fields=["status", "updated_at"])
    _record_request_event(
        request=item,
        action=MembershipRequestEventAction.PAYMENT_INSTRUCTION_ISSUED,
        actor=actor,
    )
    return item, instruction


@transaction.atomic
def submit_payment(*, request_id, user):
    item = MembershipRequest.objects.select_for_update().filter(pk=request_id, user=user).first()
    if not item:
        raise MembershipRequest.DoesNotExist
    if item.status == MembershipRequestStatus.PAYMENT_SUBMITTED:
        return item, False
    if item.status != MembershipRequestStatus.PAYMENT_PENDING:
        raise RequestTransitionConflict("Only payment-pending requests can be submitted")
    if not MembershipPaymentInstruction.objects.filter(request=item).exists():
        raise RequestTransitionConflict("Payment instruction has not been issued")
    item.status = MembershipRequestStatus.PAYMENT_SUBMITTED
    item.payment_submitted_at = timezone.now()
    item.save(update_fields=["status", "payment_submitted_at", "updated_at"])
    _record_request_event(
        request=item,
        action=MembershipRequestEventAction.PAYMENT_SUBMITTED,
        actor=user,
    )
    return item, True


@transaction.atomic
def return_to_payment_pending(*, request_id, actor, reason: str):
    if not reason.strip():
        raise RequestTransitionConflict("A return reason is required")
    item = _locked_request(request_id)
    if item.status != MembershipRequestStatus.PAYMENT_SUBMITTED:
        raise RequestTransitionConflict("Only submitted payments can be returned")
    item.status = MembershipRequestStatus.PAYMENT_PENDING
    item.return_reason = reason.strip()
    item.processed_by = actor
    item.payment_submitted_at = None
    item.returned_at = timezone.now()
    item.save(
        update_fields=[
            "status",
            "return_reason",
            "processed_by",
            "payment_submitted_at",
            "returned_at",
            "updated_at",
        ]
    )
    _record_request_event(
        request=item,
        action=MembershipRequestEventAction.PAYMENT_RETURNED,
        actor=actor,
        reason=reason,
    )
    return item


@transaction.atomic
def reject_request(*, request_id, actor, reason: str):
    if not reason.strip():
        raise RequestTransitionConflict("A rejection reason is required")
    item = _locked_request(request_id)
    if item.status not in OPEN_REQUEST_STATUSES:
        raise RequestTransitionConflict("Only open requests can be rejected")
    item.status = MembershipRequestStatus.REJECTED
    item.open_slot = None
    item.rejection_reason = reason.strip()
    item.processed_by = actor
    item.rejected_at = timezone.now()
    item.save(
        update_fields=[
            "status",
            "open_slot",
            "rejection_reason",
            "processed_by",
            "rejected_at",
            "updated_at",
        ]
    )
    _record_request_event(
        request=item,
        action=MembershipRequestEventAction.REJECTED,
        actor=actor,
        reason=reason,
    )
    return item


def _create_cycle(membership: Membership, sequence: int) -> MembershipCycle:
    start = add_months(membership.start_date, sequence - 1)
    end = min(add_months(membership.start_date, sequence), membership.end_date)
    cycle = MembershipCycle.objects.create(
        membership=membership, sequence=sequence, start_date=start, end_date=end
    )
    MembershipCycleQuota.objects.bulk_create(
        [
            MembershipCycleQuota(
                cycle=cycle,
                product_snapshot=snapshot,
                granted_units=snapshot.quota_units_per_cycle,
                remaining_units=snapshot.quota_units_per_cycle,
            )
            for snapshot in membership.product_snapshots.all()
        ]
    )
    return cycle


@transaction.atomic
def sync_membership_status(membership: Membership, *, as_of: date | None = None) -> Membership:
    as_of = as_of or timezone.localdate()
    membership = Membership.objects.select_for_update().get(pk=membership.pk)
    if membership.status == MembershipStatus.ENDED or membership.end_date <= as_of:
        expected_status = MembershipStatus.ENDED
        expected_slot = None
    elif membership.start_date <= as_of:
        expected_status = MembershipStatus.ACTIVE
        expected_slot = True
    else:
        expected_status = MembershipStatus.SCHEDULED
        expected_slot = True
    if membership.status != expected_status or membership.current_slot != expected_slot:
        membership.status = expected_status
        membership.current_slot = expected_slot
        membership.save(update_fields=["status", "current_slot", "updated_at"])
    return membership


@transaction.atomic
def refresh_membership_statuses(*, as_of: date | None = None) -> None:
    as_of = as_of or timezone.localdate()
    updated_at = timezone.now()
    Membership.objects.filter(current_slot=True, end_date__lte=as_of).update(
        status=MembershipStatus.ENDED,
        current_slot=None,
        updated_at=updated_at,
    )
    Membership.objects.filter(
        current_slot=True,
        status=MembershipStatus.SCHEDULED,
        start_date__lte=as_of,
        end_date__gt=as_of,
    ).update(status=MembershipStatus.ACTIVE, updated_at=updated_at)


@transaction.atomic
def ensure_due_cycles(
    membership: Membership, *, as_of: date | None = None
) -> list[MembershipCycle]:
    as_of = as_of or timezone.localdate()
    membership = sync_membership_status(membership, as_of=as_of)
    due = 0
    for sequence in range(1, membership.duration_months_snapshot + 1):
        if add_months(membership.start_date, sequence - 1) <= as_of:
            due = sequence
        else:
            break
    due = min(due, membership.duration_months_snapshot)
    existing = {item.sequence: item for item in membership.cycles.all()}
    created = []
    for sequence in range(1, due + 1):
        if sequence not in existing:
            created.append(_create_cycle(membership, sequence))
    if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE:
        expired = MembershipCycleQuota.objects.filter(
            cycle__membership=membership,
            cycle__end_date__lte=as_of,
            remaining_units__gt=0,
        )
        expired.update(expired_units=F("expired_units") + F("remaining_units"), remaining_units=0)
    return created


@transaction.atomic
def approve_request(*, request_id, actor, start_date: date):
    item = _locked_request(request_id)
    User.objects.select_for_update().get(pk=item.user_id)
    if hasattr(item, "membership"):
        return item.membership, False
    if item.status != MembershipRequestStatus.PAYMENT_SUBMITTED:
        raise RequestTransitionConflict("Only submitted payments can be approved")
    today = timezone.localdate()
    if start_date < today:
        raise StartDateInPast("Membership start date cannot be in the past")
    Membership.objects.filter(
        user=item.user,
        current_slot=True,
        end_date__lte=today,
    ).update(status=MembershipStatus.ENDED, current_slot=None)
    if Membership.objects.filter(user=item.user, current_slot=True).exists():
        raise ActiveMembershipExists("User already has a scheduled or active membership")
    if not MembershipPaymentInstruction.objects.filter(request=item).exists():
        raise RequestTransitionConflict("Payment instruction is missing")
    membership = Membership.objects.create(
        user=item.user,
        request=item,
        package=item.package,
        package_name_snapshot=item.package_name_snapshot,
        package_description_snapshot=item.package_description_snapshot,
        upfront_price_snapshot=item.upfront_price_snapshot,
        duration_months_snapshot=item.duration_months_snapshot,
        quota_policy_snapshot=item.quota_policy_snapshot,
        start_date=start_date,
        end_date=add_months(start_date, item.duration_months_snapshot),
        status=(MembershipStatus.SCHEDULED if start_date > today else MembershipStatus.ACTIVE),
        current_slot=True,
        activated_by=actor,
    )
    snapshots = [
        MembershipProductSnapshot(
            membership=membership,
            product=snapshot.product,
            product_name=snapshot.product_name,
            unit_size=snapshot.unit_size,
            unit_label_vi=snapshot.unit_label_vi,
            unit_label_en=snapshot.unit_label_en,
            quota_units_per_cycle=snapshot.quota_units_per_cycle,
        )
        for snapshot in item.item_snapshots.select_related("product")
    ]
    if not snapshots:
        raise RequestTransitionConflict("Request package snapshot is empty")
    MembershipProductSnapshot.objects.bulk_create(snapshots)
    if start_date <= today:
        ensure_due_cycles(membership, as_of=today)
    item.status = MembershipRequestStatus.APPROVED
    item.open_slot = None
    item.processed_by = actor
    item.approved_at = timezone.now()
    item.save(update_fields=["status", "open_slot", "processed_by", "approved_at", "updated_at"])
    _record_request_event(
        request=item,
        action=MembershipRequestEventAction.APPROVED,
        actor=actor,
    )
    return membership, True


def _normalized_usage_lines(lines: list[dict]) -> list[tuple[str, int]]:
    normalized = sorted((str(item["product_id"]), int(item["units"])) for item in lines)
    if not normalized or any(units <= 0 for _product_id, units in normalized):
        raise QuotaError("Usage lines and positive units are required")
    if len({product_id for product_id, _units in normalized}) != len(normalized):
        raise QuotaError("Each product may appear only once")
    return normalized


def validate_usage_integrity(order: MembershipUsageOrder) -> None:
    membership = order.membership
    if order.user_id != membership.user_id:
        raise UsageIntegrityConflict("Usage order user does not match membership user")
    lines = list(
        order.lines.select_related("product_snapshot").prefetch_related(
            "allocations__cycle_quota__cycle"
        )
    )
    if not lines:
        raise UsageIntegrityConflict("Usage order has no lines")
    for line in lines:
        if line.product_snapshot.membership_id != order.membership_id:
            raise UsageIntegrityConflict("Usage line product does not belong to membership")
        allocations = list(line.allocations.all())
        if not allocations or sum(item.units for item in allocations) != line.units:
            raise UsageIntegrityConflict("Usage line allocations do not match deducted units")
        for allocation in allocations:
            quota = allocation.cycle_quota
            if (
                quota.cycle.membership_id != order.membership_id
                or quota.product_snapshot_id != line.product_snapshot_id
            ):
                raise UsageIntegrityConflict("Usage allocation does not match its line")


@transaction.atomic
def create_usage_order(*, membership_id, actor, idempotency_key, lines: list[dict], note=""):
    membership = Membership.objects.select_for_update().select_related("user").get(pk=membership_id)
    requested = _normalized_usage_lines(lines)
    existing = (
        MembershipUsageOrder.objects.filter(membership=membership, idempotency_key=idempotency_key)
        .prefetch_related("lines__product_snapshot")
        .first()
    )
    if existing:
        validate_usage_integrity(existing)
        existing_lines = sorted(
            (str(line.product_snapshot.product_id), line.units) for line in existing.lines.all()
        )
        if existing_lines != requested or existing.note != note.strip():
            raise UsageIdempotencyConflict(
                "Idempotency key was used with a different usage payload"
            )
        return existing, False
    today = timezone.localdate()
    ensure_due_cycles(membership, as_of=today)
    membership.refresh_from_db()
    if membership.status != MembershipStatus.ACTIVE or not (
        membership.start_date <= today < membership.end_date
    ):
        raise QuotaError("Membership is not active today")
    snapshots = {
        str(item.product_id): item
        for item in MembershipProductSnapshot.objects.filter(membership=membership)
    }
    for product_id, _units in requested:
        if product_id not in snapshots:
            raise ProductNotInMembership("Product is not included in this membership")
    quotas = list(
        MembershipCycleQuota.objects.select_for_update()
        .filter(
            cycle__membership=membership,
            product_snapshot__product_id__in=[product_id for product_id, _units in requested],
            remaining_units__gt=0,
        )
        .select_related("cycle", "product_snapshot")
        .order_by("cycle__sequence", "id")
    )
    if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE:
        quotas = [
            quota for quota in quotas if quota.cycle.start_date <= today < quota.cycle.end_date
        ]
    by_product = {}
    for quota in quotas:
        by_product.setdefault(str(quota.product_snapshot.product_id), []).append(quota)
    if any(
        sum(q.remaining_units for q in by_product.get(pid, [])) < units for pid, units in requested
    ):
        raise InsufficientQuota("Requested units exceed available quota")
    order = MembershipUsageOrder.objects.create(
        membership=membership,
        user=membership.user,
        idempotency_key=idempotency_key,
        note=note.strip(),
        created_by=actor,
    )
    for product_id, units in requested:
        line = MembershipUsageLine.objects.create(
            order=order, product_snapshot=snapshots[product_id], units=units
        )
        needed = units
        for quota in by_product[product_id]:
            allocated = min(needed, quota.remaining_units)
            if not allocated:
                continue
            quota.remaining_units -= allocated
            quota.save(update_fields=["remaining_units"])
            MembershipUsageAllocation.objects.create(line=line, cycle_quota=quota, units=allocated)
            needed -= allocated
            if not needed:
                break
    return order, True


@transaction.atomic
def undo_usage_order(*, order_id, actor, reason: str):
    if not reason.strip():
        raise UsageUndoConflict("An undo reason is required")
    order = MembershipUsageOrder.objects.select_for_update().get(pk=order_id)
    if (
        order.status != UsageOrderStatus.APPLIED
        or MembershipUsageReversal.objects.filter(order=order).exists()
    ):
        raise UsageUndoConflict("Usage order was already undone")
    validate_usage_integrity(order)
    allocations = list(
        MembershipUsageAllocation.objects.filter(line__order=order)
        .select_related("cycle_quota__cycle__membership")
        .order_by("id")
    )
    quota_ids = [item.cycle_quota_id for item in allocations]
    locked_quotas = {
        item.pk: item
        for item in MembershipCycleQuota.objects.select_for_update().filter(pk__in=quota_ids)
    }
    today = timezone.localdate()
    for allocation in allocations:
        quota = locked_quotas[allocation.cycle_quota_id]
        membership = quota.cycle.membership
        if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE and quota.cycle.end_date <= today:
            quota.expired_units += allocation.units
            quota.save(update_fields=["expired_units"])
        else:
            quota.remaining_units += allocation.units
            quota.save(update_fields=["remaining_units"])
    MembershipUsageReversal.objects.create(order=order, actor=actor, reason=reason.strip())
    order.status = UsageOrderStatus.REVERSED
    order.reversed_at = timezone.now()
    order.save(update_fields=["status", "reversed_at"])
    return order
