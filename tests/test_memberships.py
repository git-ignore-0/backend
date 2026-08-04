import threading
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import requests
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from oauth2_provider.models import AccessToken
from rest_framework.test import APIClient

from identity.models import AccountStatus, User
from members.models import MemberProfile
from memberships.banks import (
    FRESH_BANKS_CACHE_KEY,
    STALE_BANKS_CACHE_KEY,
    UNAVAILABLE_BANKS_CACHE_KEY,
    BankDirectoryUnavailable,
    get_transfer_banks,
)
from memberships.models import (
    OPEN_REQUEST_STATUSES,
    Membership,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPaymentSettings,
    MembershipRequest,
    MembershipRequestEventAction,
    MembershipRequestStatus,
    MembershipStatus,
    MembershipUsageAllocation,
    MembershipUsageOrder,
    MembershipUsageReversal,
    Product,
    QuotaPolicy,
    UsageOrderStatus,
)
from memberships.services import (
    ActiveMembershipExists,
    DirectTransferDisabled,
    InsufficientQuota,
    OpenRequestExists,
    PaymentSettingsInvalid,
    ProductNotInMembership,
    RequestTransitionConflict,
    StartDateInPast,
    UsageIntegrityConflict,
    UsageUndoConflict,
    add_months,
    approve_request,
    create_membership_request,
    create_usage_order,
    ensure_due_cycles,
    issue_payment_instruction,
    mark_contacted,
    reject_request,
    return_to_payment_pending,
    submit_payment,
    sync_membership_status,
    undo_usage_order,
)


@pytest.fixture(autouse=True)
def clear_membership_bank_cache():
    cache.clear()
    yield
    cache.clear()


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


def create_active_user(email, *, phone="0901234567"):
    user = User.objects.create_user(
        email=email,
        name="Membership User",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    EmailAddress.objects.create(user=user, email=email, primary=True, verified=True)
    if phone is not None:
        MemberProfile.objects.create(user=user, phone_number=phone)
    return user


def create_package(*, policy=QuotaPolicy.EXPIRE, quota=2, price=Decimal("6000000")):
    product = Product.objects.create(
        name="Seasonal basket",
        short_description="Fresh vegetables",
        unit_size=Decimal("0.500"),
        unit_label_vi="kg",
        unit_label_en="kg",
    )
    package = MembershipPackage.objects.create(
        name="Six month membership",
        description="One delivery per cycle",
        upfront_price=price,
        duration_months=6,
        quota_policy=policy,
    )
    MembershipPackageItem.objects.create(package=package, product=product, quota_units=quota)
    return package, product


def cache_transfer_banks():
    banks = [
        {
            "bin": "970436",
            "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
            "short_name": "Vietcombank",
            "code": "VCB",
        }
    ]
    cache.set(FRESH_BANKS_CACHE_KEY, banks, timeout=86400)
    cache.set(STALE_BANKS_CACHE_KEY, banks, timeout=None)
    return banks


def configure_payment_settings(actor=None, *, enabled=True):
    cache_transfer_banks()
    item, _created = MembershipPaymentSettings.objects.update_or_create(
        key="default",
        defaults={
            "direct_transfer_enabled": enabled,
            "bank_bin": "970436",
            "bank_name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
            "account_number": "0123456789",
            "account_name": "NATURAL FARMING VIETNAM",
            "updated_by": actor,
        },
    )
    return item


def ensure_phone(user, phone="0901234567"):
    MemberProfile.objects.update_or_create(user=user, defaults={"phone_number": phone})


def activate_membership(user, *, policy=QuotaPolicy.EXPIRE, quota=2, start_date=None):
    ensure_phone(user)
    package, product = create_package(policy=policy, quota=quota)
    configure_payment_settings()
    request, instruction = create_membership_request(
        user=user, package_id=package.pk, flow="direct_transfer"
    )
    assert instruction is not None
    submit_payment(request_id=request.pk, user=user)
    requested_start = start_date or timezone.localdate()
    approval_start = max(requested_start, timezone.localdate())
    membership, created = approve_request(
        request_id=request.pk,
        actor=user,
        start_date=approval_start,
    )
    assert created
    if requested_start < approval_start:
        MembershipCycleQuota.objects.filter(cycle__membership=membership).delete()
        membership.cycles.all().delete()
        membership.start_date = requested_start
        membership.end_date = add_months(requested_start, package.duration_months)
        membership.status = MembershipStatus.ACTIVE
        membership.save(update_fields=["start_date", "end_date", "status", "updated_at"])
    return membership, product, request


@pytest.fixture
def admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


@pytest.mark.django_db
def test_current_membership_is_null_without_a_membership(admin_api):
    response = admin_api.get("/api/v1/memberships/current")

    assert response.status_code == 200
    assert response.json()["data"] is None


@pytest.mark.django_db
def test_current_active_membership_returns_database_package_id(admin_api, active_user):
    membership, product, _request = activate_membership(active_user)

    response = admin_api.get("/api/v1/memberships/current")
    quota_response = admin_api.get("/api/v1/memberships/quota")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == MembershipStatus.ACTIVE
    assert response.json()["data"]["package_id"] == str(membership.package_id)
    assert quota_response.status_code == 200
    assert quota_response.json()["data"]["membership_id"] == str(membership.pk)
    assert quota_response.json()["data"]["products"][0]["product_id"] == str(product.pk)


@pytest.mark.django_db
def test_consultation_and_direct_transfer_requests_snapshot_authoritative_data(active_user):
    ensure_phone(active_user)
    package, product = create_package()
    consultation, instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="consultation"
    )
    assert consultation.status == MembershipRequestStatus.CONSULTATION_REQUESTED
    assert instruction is None
    assert consultation.item_snapshots.get().product_id == product.pk

    mark_contacted(request_id=consultation.pk, actor=active_user)
    with pytest.raises(PaymentSettingsInvalid):
        issue_payment_instruction(request_id=consultation.pk, actor=active_user)
    configure_payment_settings(active_user)
    consultation, instruction = issue_payment_instruction(
        request_id=consultation.pk, actor=active_user
    )
    assert consultation.status == MembershipRequestStatus.PAYMENT_PENDING
    assert instruction.phone_snapshot == "0901234567"
    assert instruction.transfer_content == f"0901234567-{consultation.short_code}"
    assert instruction.amount == Decimal("6000000")


@pytest.mark.django_db
def test_direct_transfer_is_fail_closed_while_consultation_remains_available(active_user):
    ensure_phone(active_user)
    package, _product = create_package()
    consultation, instruction = create_membership_request(
        user=active_user,
        package_id=package.pk,
        flow="consultation",
    )
    assert consultation.status == MembershipRequestStatus.CONSULTATION_REQUESTED
    assert instruction is None

    other_user = create_active_user("direct-disabled@example.com")
    with pytest.raises(DirectTransferDisabled):
        create_membership_request(
            user=other_user,
            package_id=package.pk,
            flow="direct_transfer",
        )
    assert not MembershipRequest.objects.filter(user=other_user).exists()

    configure_payment_settings(enabled=False)
    third_user = create_active_user("direct-still-disabled@example.com")
    with pytest.raises(DirectTransferDisabled):
        create_membership_request(
            user=third_user,
            package_id=package.pk,
            flow="direct_transfer",
        )
    assert not MembershipRequest.objects.filter(user=third_user).exists()


@pytest.mark.django_db
def test_direct_transfer_requires_valid_enabled_payment_settings(active_user):
    ensure_phone(active_user)
    package, _product = create_package()
    MembershipPaymentSettings.objects.create(
        direct_transfer_enabled=True,
        bank_bin="",
        bank_name="",
        account_number="",
        account_name="",
    )
    with pytest.raises(PaymentSettingsInvalid):
        create_membership_request(user=active_user, package_id=package.pk, flow="direct_transfer")
    assert not MembershipRequest.objects.filter(user=active_user).exists()

    configure_payment_settings(enabled=True)
    settings = MembershipPaymentSettings.objects.get()
    settings.bank_bin = "999999"
    settings.bank_name = "Fake bank"
    settings.account_number = "0123456789"
    settings.account_name = "NATURAL FARMING VIETNAM"
    settings.save()
    with pytest.raises(PaymentSettingsInvalid):
        create_membership_request(user=active_user, package_id=package.pk, flow="direct_transfer")
    assert not MembershipRequest.objects.filter(user=active_user).exists()

    configure_payment_settings(enabled=True)
    request, instruction = create_membership_request(
        user=active_user,
        package_id=package.pk,
        flow="direct_transfer",
    )
    assert request.status == MembershipRequestStatus.PAYMENT_PENDING
    assert instruction is not None
    assert instruction.bank_bin == "970436"
    assert instruction.bank_name == "Ngân hàng TMCP Ngoại Thương Việt Nam"


@pytest.mark.django_db
def test_self_request_requires_valid_phone_and_snapshots_form_phone(
    access_token, active_user, admin_api
):
    package, _product = create_package()
    client = bearer_client(access_token)
    base_payload = {"package_id": str(package.pk), "flow": "consultation"}

    missing = client.post("/api/v1/memberships/requests", base_payload, format="json")
    assert missing.status_code == 400
    for phone in ("abc", "1234567", "+84 912 abc"):
        response = client.post(
            "/api/v1/memberships/requests",
            {**base_payload, "phone": phone},
            format="json",
        )
        assert response.status_code == 400
        assert "phone" in response.json()

    response = client.post(
        "/api/v1/memberships/requests",
        {**base_payload, "phone": "+84 912 345 678"},
        format="json",
    )
    assert response.status_code == 201
    request_id = response.json()["data"]["id"]
    item = MembershipRequest.objects.get(pk=request_id)
    assert item.phone_snapshot == "+84 912 345 678"
    assert not MemberProfile.objects.filter(user=active_user).exists()
    detail = admin_api.get(f"/api/v1/admin/membership-requests/{request_id}")
    assert detail.status_code == 200
    assert detail.json()["data"]["user"]["phone"] == "+84 912 345 678"


@pytest.mark.django_db
def test_package_changes_cannot_change_request_or_membership_snapshot(active_user):
    membership, product, request = activate_membership(active_user)
    package = request.package
    package.name = "Changed package"
    package.upfront_price = 1
    package.save()
    product.name = "Changed product"
    product.unit_size = Decimal("9.000")
    product.save()
    snapshot = membership.product_snapshots.get()
    assert membership.package_name_snapshot == "Six month membership"
    assert membership.upfront_price_snapshot == Decimal("6000000")
    assert snapshot.product_name == "Seasonal basket"
    assert snapshot.unit_size == Decimal("0.500")


@pytest.mark.django_db
def test_payment_submission_and_approval_are_idempotent(active_user):
    ensure_phone(active_user)
    package, _product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer"
    )
    first, changed = submit_payment(request_id=request.pk, user=active_user)
    second, changed_again = submit_payment(request_id=request.pk, user=active_user)
    assert first.pk == second.pk
    assert changed is True
    assert changed_again is False

    start = timezone.localdate()
    membership, created = approve_request(
        request_id=request.pk, actor=active_user, start_date=start
    )
    replay, replay_created = approve_request(
        request_id=request.pk, actor=active_user, start_date=start + timedelta(days=1)
    )
    assert created is True
    assert replay_created is False
    assert replay.pk == membership.pk
    assert replay.start_date == start
    assert Membership.objects.filter(user=active_user).count() == 1
    assert membership.end_date == add_months(start, 6)


@pytest.mark.django_db
def test_approval_rejects_past_start_date_in_api_and_service(admin_api, active_user):
    package, _product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer", phone="0901234567"
    )
    submit_payment(request_id=request.pk, user=active_user)
    past = timezone.localdate() - timedelta(days=1)

    response = admin_api.post(
        f"/api/v1/admin/membership-requests/{request.pk}/approve",
        {"start_date": past.isoformat()},
        format="json",
    )
    assert response.status_code == 400
    assert "start_date" in response.json()
    with pytest.raises(StartDateInPast):
        approve_request(request_id=request.pk, actor=active_user, start_date=past)
    assert not Membership.objects.filter(request=request).exists()


@pytest.mark.django_db
def test_database_and_services_enforce_one_open_request_and_active_membership(active_user):
    ensure_phone(active_user)
    package, _product = create_package()
    first, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="consultation"
    )
    with pytest.raises(OpenRequestExists):
        create_membership_request(user=active_user, package_id=package.pk, flow="consultation")
    duplicate = MembershipRequest(
        short_code="DUPLICATE",
        user=active_user,
        package=package,
        package_name_snapshot=package.name,
        package_description_snapshot="",
        upfront_price_snapshot=package.upfront_price,
        duration_months_snapshot=6,
        quota_policy_snapshot=QuotaPolicy.EXPIRE,
        status=MembershipRequestStatus.CONTACTED,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate.save()
    assert (
        MembershipRequest.objects.filter(user=active_user, status__in=OPEN_REQUEST_STATUSES).get()
        == first
    )

    reject_request(request_id=first.pk, actor=active_user, reason="Closed")
    membership, _product, _request = activate_membership(active_user)
    with pytest.raises(ActiveMembershipExists):
        create_membership_request(user=active_user, package_id=package.pk, flow="consultation")
    assert membership.status == MembershipStatus.ACTIVE

    terminal_request = MembershipRequest.objects.create(
        short_code="TERMINAL",
        user=active_user,
        package=package,
        package_name_snapshot=package.name,
        package_description_snapshot=package.description,
        upfront_price_snapshot=package.upfront_price,
        duration_months_snapshot=package.duration_months,
        quota_policy_snapshot=package.quota_policy,
        status=MembershipRequestStatus.REJECTED,
        open_slot=None,
        rejection_reason="Constraint fixture",
        rejected_at=timezone.now(),
    )
    duplicate_membership = Membership(
        user=active_user,
        request=terminal_request,
        package=package,
        package_name_snapshot=package.name,
        package_description_snapshot=package.description,
        upfront_price_snapshot=package.upfront_price,
        duration_months_snapshot=package.duration_months,
        quota_policy_snapshot=package.quota_policy,
        start_date=timezone.localdate(),
        end_date=add_months(timezone.localdate(), package.duration_months),
        activated_by=active_user,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        duplicate_membership.save()


@pytest.mark.django_db
def test_expire_and_rollover_cycles_follow_start_anniversary():
    expire_user = create_active_user("expire@example.com")
    expire_membership, _product, _request = activate_membership(
        expire_user, policy=QuotaPolicy.EXPIRE, start_date=date(2026, 7, 31)
    )
    ensure_due_cycles(expire_membership, as_of=date(2026, 9, 1))
    expire_quotas = list(
        MembershipCycleQuota.objects.filter(cycle__membership=expire_membership).order_by(
            "cycle__sequence"
        )
    )
    assert [item.cycle.start_date for item in expire_quotas] == [
        date(2026, 7, 31),
        date(2026, 8, 31),
    ]
    assert [(item.remaining_units, item.expired_units) for item in expire_quotas] == [
        (0, 2),
        (2, 0),
    ]

    rollover_user = create_active_user("rollover@example.com")
    rollover_membership, _product, _request = activate_membership(
        rollover_user, policy=QuotaPolicy.ROLLOVER, start_date=date(2026, 7, 31)
    )
    ensure_due_cycles(rollover_membership, as_of=date(2026, 9, 1))
    assert list(
        MembershipCycleQuota.objects.filter(cycle__membership=rollover_membership)
        .order_by("cycle__sequence")
        .values_list("remaining_units", flat=True)
    ) == [2, 2]


@pytest.mark.django_db
def test_ensure_due_cycles_is_idempotent(active_user):
    membership, _product, _request = activate_membership(active_user)
    MembershipCycleQuota.objects.filter(cycle__membership=membership).delete()
    membership.cycles.all().delete()

    first_created = ensure_due_cycles(membership)
    second_created = ensure_due_cycles(membership)

    assert [cycle.sequence for cycle in first_created] == [1]
    assert second_created == []
    assert list(membership.cycles.values_list("sequence", flat=True)) == [1]
    assert MembershipCycleQuota.objects.filter(cycle__membership=membership).count() == 1


@pytest.mark.django_db
def test_usage_deducts_exact_product_never_negative_and_undo_restores_once(active_user):
    membership, product, _request = activate_membership(
        active_user, policy=QuotaPolicy.ROLLOVER, quota=3
    )
    key = uuid.uuid4()
    order, created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=key,
        lines=[{"product_id": product.pk, "units": 2}],
    )
    replay, replay_created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=key,
        lines=[{"product_id": product.pk, "units": 2}],
    )
    assert created is True and replay_created is False and replay.pk == order.pk
    allocation = MembershipUsageAllocation.objects.get(line__order=order)
    allocation.cycle_quota.refresh_from_db()
    assert allocation.units == 2
    assert allocation.cycle_quota.remaining_units == 1

    with pytest.raises(InsufficientQuota):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": product.pk, "units": 2}],
        )
    other_package, other_product = create_package()
    assert other_package.pk
    with pytest.raises(ProductNotInMembership):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": other_product.pk, "units": 1}],
        )
    undo_usage_order(order_id=order.pk, actor=active_user, reason="Entered by mistake")
    allocation.cycle_quota.refresh_from_db()
    order.refresh_from_db()
    assert allocation.cycle_quota.remaining_units == 3
    assert order.status == UsageOrderStatus.REVERSED
    assert MembershipUsageReversal.objects.get(order=order).reason == "Entered by mistake"
    with pytest.raises(UsageUndoConflict):
        undo_usage_order(order_id=order.pk, actor=active_user, reason="Double undo")


@pytest.mark.django_db
def test_self_usage_payload_uses_membership_product_unit_snapshot(active_user, access_token):
    membership, product, _request = activate_membership(active_user, quota=3)
    create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 2}],
    )
    product.unit_size = Decimal("9.000")
    product.unit_label_vi = "túi"
    product.unit_label_en = "bags"
    product.save(update_fields=["unit_size", "unit_label_vi", "unit_label_en", "updated_at"])

    response = bearer_client(access_token).get("/api/v1/memberships/usage?page=1")

    assert response.status_code == 200
    assert response.json()["data"][0]["lines"][0] == {
        "product_id": str(product.pk),
        "product_name": "Seasonal basket",
        "unit_size": "0.500",
        "unit_label_vi": "kg",
        "unit_label_en": "kg",
        "units": 2,
    }


@pytest.mark.django_db
def test_return_payment_pending_requires_reason_and_keeps_instruction(active_user):
    ensure_phone(active_user)
    package, _product = create_package()
    configure_payment_settings()
    request, instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer"
    )
    submit_payment(request_id=request.pk, user=active_user)
    returned = return_to_payment_pending(
        request_id=request.pk, actor=active_user, reason="Amount is short"
    )
    assert returned.status == MembershipRequestStatus.PAYMENT_PENDING
    assert returned.payment_instruction.pk == instruction.pk
    assert returned.return_reason == "Amount is short"


@pytest.mark.django_db
def test_future_membership_is_visible_without_quota_then_becomes_active_and_ended(
    admin_api, active_user
):
    start_date = timezone.localdate() + timedelta(days=2)
    ensure_phone(active_user)
    package, product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer"
    )
    submit_payment(request_id=request.pk, user=active_user)
    approve_response = admin_api.post(
        f"/api/v1/admin/membership-requests/{request.pk}/approve",
        {"start_date": start_date.isoformat()},
        format="json",
    )
    assert approve_response.status_code == 201
    membership = Membership.objects.get(pk=approve_response.json()["data"]["id"])
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.SCHEDULED
    assert membership.current_slot is True
    assert not membership.cycles.exists()

    current_response = admin_api.get("/api/v1/memberships/current")
    assert current_response.status_code == 200
    assert current_response.json()["data"]["status"] == MembershipStatus.SCHEDULED
    assert current_response.json()["data"]["package_id"] == str(package.pk)
    assert current_response.json()["data"]["start_date"] == start_date.isoformat()
    assert current_response.json()["data"]["end_date"] == membership.end_date.isoformat()
    assert "user" not in current_response.json()["data"]
    assert admin_api.get("/api/v1/memberships/quota").json()["data"] is None
    usage_response = admin_api.post(
        "/api/v1/admin/membership-usage",
        {
            "membership_id": str(membership.pk),
            "idempotency_key": str(uuid.uuid4()),
            "lines": [{"product_id": str(product.pk), "units": 1}],
        },
        format="json",
    )
    assert usage_response.status_code == 409
    assert usage_response.json()["errors"] == [{"code": "invalid_quota_usage"}]
    assert not membership.cycles.exists()

    membership = sync_membership_status(membership, as_of=start_date)
    ensure_due_cycles(membership, as_of=start_date)
    membership.refresh_from_db()
    assert membership.status == MembershipStatus.ACTIVE
    assert membership.cycles.count() == 1

    membership = sync_membership_status(membership, as_of=membership.end_date)
    assert membership.status == MembershipStatus.ENDED
    assert membership.current_slot is None
    assert admin_api.get("/api/v1/memberships/current").json()["data"] is None


@pytest.mark.django_db
def test_admin_membership_user_payload_and_phone_search(admin_api, active_user):
    phone = "0918765432"
    ensure_phone(active_user, phone)
    package, _product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer"
    )

    request_search = admin_api.get(
        "/api/v1/admin/membership-requests", {"search": phone}
    )
    assert request_search.status_code == 200
    assert request_search.json()["meta"] == {"page": 1, "page_size": 30, "total": 1}
    assert request_search.json()["data"][0]["id"] == str(request.pk)

    submit_payment(request_id=request.pk, user=active_user)
    membership, _created = approve_request(
        request_id=request.pk,
        actor=active_user,
        start_date=timezone.localdate(),
    )
    membership_search = admin_api.get(
        "/api/v1/admin/memberships", {"search": phone}
    )
    assert membership_search.status_code == 200
    assert membership_search.json()["meta"] == {
        "page": 1,
        "page_size": 30,
        "total": 1,
    }
    expected_user = {
        "id": str(active_user.pk),
        "name": active_user.name,
        "email": active_user.email,
        "phone": phone,
    }
    assert membership_search.json()["data"][0]["id"] == str(membership.pk)
    assert membership_search.json()["data"][0]["user"] == expected_user

    detail_response = admin_api.get(f"/api/v1/admin/memberships/{membership.pk}")
    assert detail_response.status_code == 200
    assert detail_response.json()["data"]["user"] == expected_user
    assert "user" not in admin_api.get("/api/v1/memberships/current").json()["data"]


@pytest.mark.django_db
def test_request_audit_events_preserve_multiple_payment_returns(active_user):
    ensure_phone(active_user)
    package, _product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer"
    )
    submit_payment(request_id=request.pk, user=active_user)
    return_to_payment_pending(
        request_id=request.pk,
        actor=active_user,
        reason="First bank check failed",
    )
    submit_payment(request_id=request.pk, user=active_user)
    return_to_payment_pending(
        request_id=request.pk,
        actor=active_user,
        reason="Second bank check failed",
    )

    returned_events = list(
        request.events.filter(action=MembershipRequestEventAction.PAYMENT_RETURNED).order_by(
            "created_at"
        )
    )
    assert [event.reason for event in returned_events] == [
        "First bank check failed",
        "Second bank check failed",
    ]
    assert all(event.actor_id == active_user.pk for event in returned_events)


@pytest.mark.django_db
def test_rollover_usage_allocates_oldest_cycles_and_undo_restores_each_cycle(active_user):
    membership, product, _request = activate_membership(
        active_user,
        policy=QuotaPolicy.ROLLOVER,
        quota=2,
        start_date=add_months(timezone.localdate(), -1),
    )
    order, _created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 3}],
    )
    allocations = list(
        MembershipUsageAllocation.objects.filter(line__order=order).order_by(
            "cycle_quota__cycle__sequence"
        )
    )
    assert [(item.cycle_quota.cycle.sequence, item.units) for item in allocations] == [
        (1, 2),
        (2, 1),
    ]
    assert [item.cycle_quota.remaining_units for item in allocations] == [0, 1]

    undo_usage_order(order_id=order.pk, actor=active_user, reason="Wrong delivery")
    assert list(
        MembershipCycleQuota.objects.filter(cycle__membership=membership)
        .order_by("cycle__sequence")
        .values_list("remaining_units", flat=True)
    ) == [2, 2]


@pytest.mark.django_db
def test_undo_rejects_corrupted_usage_relationships(active_user):
    membership, product, _request = activate_membership(active_user, quota=2)
    order, _created = create_usage_order(
        membership_id=membership.pk,
        actor=active_user,
        idempotency_key=uuid.uuid4(),
        lines=[{"product_id": product.pk, "units": 1}],
    )
    other_user = create_active_user("corrupt-usage-user@example.com")
    MembershipUsageOrder.objects.filter(pk=order.pk).update(user=other_user)

    with pytest.raises(UsageIntegrityConflict):
        undo_usage_order(order_id=order.pk, actor=active_user, reason="Should fail")
    assert not MembershipUsageReversal.objects.filter(order=order).exists()


@pytest.mark.django_db
def test_public_package_list_is_paginated(client):
    package, product = create_package()
    MembershipPackage.objects.bulk_create(
        [
            MembershipPackage(
                name=f"Package {index:02d}",
                upfront_price=Decimal("6000000"),
                duration_months=6,
                quota_policy=QuotaPolicy.EXPIRE,
            )
            for index in range(30)
        ]
    )
    additional = MembershipPackage.objects.exclude(pk=package.pk)
    MembershipPackageItem.objects.bulk_create(
        [MembershipPackageItem(package=item, product=product, quota_units=2) for item in additional]
    )

    response = client.get("/api/v1/public/membership-packages?page=2")
    assert response.status_code == 200
    assert response.json()["meta"] == {"page": 2, "page_size": 30, "total": 31}
    assert len(response.json()["data"]) == 1


@pytest.mark.django_db
def test_membership_pagination_rejects_page_above_safe_limit(client):
    response = client.get("/api/v1/public/membership-packages?page=10001")
    assert response.status_code == 400
    assert set(response.json()) == {"page"}
    assert "10000" in response.json()["page"][0]


@pytest.mark.django_db
def test_public_payment_availability_exposes_only_the_switch(client):
    disabled = client.get("/api/v1/public/membership-payment-availability")
    assert disabled.status_code == 200
    assert disabled.json() == {"data": {"direct_transfer_enabled": False}}

    configure_payment_settings(enabled=True)
    enabled = client.get("/api/v1/public/membership-payment-availability")
    assert enabled.status_code == 200
    assert enabled.json() == {"data": {"direct_transfer_enabled": True}}
    assert "account_number" not in enabled.json()["data"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bank_bin", "999999"),
        ("bank_name", "Wrong bank name"),
        ("account_number", "ABC123"),
        ("account_number", "12345"),
        ("account_number", "1" * 20),
        ("account_name", ""),
    ],
)
def test_public_payment_availability_fails_closed_for_invalid_settings(client, field, value):
    settings = configure_payment_settings(enabled=True)
    setattr(settings, field, value)
    settings.save(update_fields=[field, "updated_at"])

    response = client.get("/api/v1/public/membership-payment-availability")
    assert response.status_code == 200
    assert response.json() == {"data": {"direct_transfer_enabled": False}}
    assert "account_number" not in response.json()["data"]


@pytest.mark.django_db
def test_public_payment_availability_fails_closed_when_bank_directory_is_unavailable(
    client, monkeypatch
):
    configure_payment_settings(enabled=True)
    cache.clear()
    http_calls = 0

    def unavailable(*args, **kwargs):
        nonlocal http_calls
        http_calls += 1
        raise requests.Timeout("vietqr timeout")

    monkeypatch.setattr("memberships.banks.requests.get", unavailable)
    response = client.get("/api/v1/public/membership-payment-availability")
    assert response.status_code == 200
    assert response.json() == {"data": {"direct_transfer_enabled": False}}
    repeated = client.get("/api/v1/public/membership-payment-availability")
    assert repeated.status_code == 200
    assert repeated.json() == {"data": {"direct_transfer_enabled": False}}
    assert http_calls == 1
    assert cache.get(UNAVAILABLE_BANKS_CACHE_KEY) is True


def test_negative_bank_cache_does_not_override_fresh_cache(monkeypatch):
    fresh = [{"bin": "970436", "name": "Fresh bank"}]
    stale = [{"bin": "970415", "name": "Stale bank"}]
    cache.set(FRESH_BANKS_CACHE_KEY, fresh, timeout=60)
    cache.set(STALE_BANKS_CACHE_KEY, stale, timeout=None)
    cache.set(UNAVAILABLE_BANKS_CACHE_KEY, True, timeout=60)
    http_calls = 0

    def unexpected_http_call(*args, **kwargs):
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("VietQR must not be called when fresh cache exists")

    monkeypatch.setattr("memberships.banks.requests.get", unexpected_http_call)

    assert get_transfer_banks() == fresh
    assert http_calls == 0


def test_negative_bank_cache_uses_stale_cache_without_fetching(monkeypatch):
    stale = [{"bin": "970415", "name": "Stale bank"}]
    cache.set(STALE_BANKS_CACHE_KEY, stale, timeout=None)
    cache.set(UNAVAILABLE_BANKS_CACHE_KEY, True, timeout=60)
    http_calls = 0

    def unexpected_http_call(*args, **kwargs):
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("VietQR must not be called while negative cache is active")

    monkeypatch.setattr("memberships.banks.requests.get", unexpected_http_call)

    assert get_transfer_banks() == stale
    assert http_calls == 0


def test_negative_bank_cache_without_available_cache_fails_fast(monkeypatch):
    cache.set(UNAVAILABLE_BANKS_CACHE_KEY, True, timeout=60)
    http_calls = 0

    def unexpected_http_call(*args, **kwargs):
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("VietQR must not be called while negative cache is active")

    monkeypatch.setattr("memberships.banks.requests.get", unexpected_http_call)

    with pytest.raises(BankDirectoryUnavailable):
        get_transfer_banks()
    assert http_calls == 0


def test_successful_vietqr_fetch_clears_negative_cache_set_during_request(monkeypatch):
    cache.clear()

    class BankResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "bin": "970436",
                        "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
                        "shortName": "Vietcombank",
                        "code": "VCB",
                        "transferSupported": 1,
                    }
                ]
            }

    def successful_fetch(*args, **kwargs):
        cache.set(UNAVAILABLE_BANKS_CACHE_KEY, True, timeout=60)
        return BankResponse()

    monkeypatch.setattr("memberships.banks.requests.get", successful_fetch)
    assert get_transfer_banks()[0]["bin"] == "970436"
    assert cache.get(UNAVAILABLE_BANKS_CACHE_KEY) is None


@pytest.mark.django_db
def test_vietqr_bank_directory_filters_and_falls_back_to_stale_cache(monkeypatch):
    cache.clear()

    class BankResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": [
                    {
                        "bin": "970436",
                        "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
                        "shortName": "Vietcombank",
                        "code": "VCB",
                        "transferSupported": 1,
                    },
                    {
                        "bin": "970999",
                        "name": "Unsupported bank",
                        "shortName": "Unsupported",
                        "code": "NOPE",
                        "transferSupported": 0,
                    },
                ]
            }

    monkeypatch.setattr("memberships.banks.requests.get", lambda *args, **kwargs: BankResponse())
    expected = [
        {
            "bin": "970436",
            "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
            "short_name": "Vietcombank",
            "code": "VCB",
        }
    ]
    assert get_transfer_banks() == expected

    cache.delete(FRESH_BANKS_CACHE_KEY)

    def unavailable(*args, **kwargs):
        raise requests.ConnectionError("vietqr unavailable")

    monkeypatch.setattr("memberships.banks.requests.get", unavailable)
    assert get_transfer_banks() == expected

    cache.clear()
    with pytest.raises(BankDirectoryUnavailable):
        get_transfer_banks()


@pytest.mark.django_db
def test_admin_bank_options_error_is_retryable_without_any_cache(admin_api, monkeypatch):
    cache.clear()

    def unavailable(*args, **kwargs):
        raise requests.Timeout("vietqr timeout")

    monkeypatch.setattr("memberships.banks.requests.get", unavailable)
    response = admin_api.get("/api/v1/admin/membership-banks")
    assert response.status_code == 503
    assert response.json() == {"errors": [{"code": "bank_directory_unavailable"}]}


@pytest.mark.django_db
def test_admin_payment_settings_validate_only_when_enabling(admin_api):
    disabled = admin_api.put(
        "/api/v1/admin/membership-payment-settings",
        {
            "direct_transfer_enabled": False,
            "bank_bin": "",
            "account_number": "",
            "account_name": "",
            "version": 0,
        },
        format="json",
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["direct_transfer_enabled"] is False
    version = disabled.json()["data"]["version"]

    missing = admin_api.put(
        "/api/v1/admin/membership-payment-settings",
        {
            "direct_transfer_enabled": True,
            "bank_bin": "",
            "account_number": "",
            "account_name": "",
            "version": version,
        },
        format="json",
    )
    assert missing.status_code == 400
    assert set(missing.json()) == {"bank_bin", "account_number", "account_name"}

    configure_payment_settings(enabled=False)
    invalid_bank = admin_api.put(
        "/api/v1/admin/membership-payment-settings",
        {
            "direct_transfer_enabled": True,
            "bank_bin": "999999",
            "account_number": "0123456789",
            "account_name": "NATURAL FARMING VIETNAM",
            "version": version,
        },
        format="json",
    )
    assert invalid_bank.status_code == 400
    assert set(invalid_bank.json()) == {"bank_bin"}

    for invalid_account in ("ABC123", "12345", "1" * 20):
        invalid = admin_api.put(
            "/api/v1/admin/membership-payment-settings",
            {
                "direct_transfer_enabled": True,
                "bank_bin": "970436",
                "account_number": invalid_account,
                "account_name": "NATURAL FARMING VIETNAM",
                "version": version,
            },
            format="json",
        )
        assert invalid.status_code == 400
        assert set(invalid.json()) == {"account_number"}

    enabled = admin_api.put(
        "/api/v1/admin/membership-payment-settings",
        {
            "direct_transfer_enabled": True,
            "bank_bin": "970436",
            "account_number": "0123456789",
            "account_name": "NATURAL FARMING VIETNAM",
            "version": version,
        },
        format="json",
    )
    assert enabled.status_code == 200
    assert enabled.json()["data"]["direct_transfer_enabled"] is True


@pytest.mark.django_db
def test_payment_settings_reject_stale_version_without_overwrite(admin_api):
    first = admin_api.put(
        "/api/v1/admin/membership-payment-settings",
        {
            "direct_transfer_enabled": False,
            "bank_bin": "",
            "account_number": "0123456789",
            "account_name": "FIRST ADMIN",
            "version": 0,
        },
        format="json",
    )
    assert first.status_code == 200
    version = first.json()["data"]["version"]
    saved = admin_api.patch(
        "/api/v1/admin/membership-payment-settings",
        {"account_name": "SAVED CHANGE", "version": version},
        format="json",
    )
    assert saved.status_code == 200

    stale = admin_api.patch(
        "/api/v1/admin/membership-payment-settings",
        {"account_name": "STALE OVERWRITE", "version": version},
        format="json",
    )
    assert stale.status_code == 409
    assert stale.json() == {"errors": [{"code": "payment_settings_conflict"}]}
    current = MembershipPaymentSettings.objects.get(pk="default")
    assert current.account_name == "SAVED CHANGE"


@pytest.mark.django_db
def test_admin_product_lookup_is_paginated_searchable_and_can_filter_active(admin_api):
    for index in range(31):
        Product.objects.create(
            name=f"Lookup product {index:02d}",
            unit_size=Decimal("1.000"),
            unit_label_vi="kg",
            unit_label_en="kg",
            is_active=index != 30,
        )
    page_two = admin_api.get("/api/v1/admin/membership-products?page=2&active=true")
    assert page_two.status_code == 200
    assert page_two.json()["meta"] == {"page": 2, "page_size": 30, "total": 30}
    assert page_two.json()["data"] == []
    search = admin_api.get(
        "/api/v1/admin/membership-products?page=1&active=false&search=product%2030"
    )
    assert search.status_code == 200
    assert [item["name"] for item in search.json()["data"]] == ["Lookup product 30"]


@pytest.mark.django_db(transaction=True)
def test_payment_settings_migration_disables_legacy_non_bin_configuration():
    migrate_from = ("memberships", "0002_membershippaymentsettings_direct_transfer_enabled")
    migrate_to = (
        "memberships",
        "0003_remove_membershippaymentsettings_bank_code_and_more",
    )
    current_migration = (
        "memberships",
        "0005_membership_request_phone_and_payment_version",
    )
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([migrate_from])
        old_apps = executor.loader.project_state([migrate_from]).apps
        LegacySettings = old_apps.get_model("memberships", "MembershipPaymentSettings")
        LegacySettings.objects.create(
            key="default",
            direct_transfer_enabled=True,
            bank_code="VCB",
            account_number="0123456789",
            account_name="NATURAL FARMING VIETNAM",
        )

        executor = MigrationExecutor(connection)
        executor.migrate([migrate_to])
        new_apps = executor.loader.project_state([migrate_to]).apps
        MigratedSettings = new_apps.get_model("memberships", "MembershipPaymentSettings")
        migrated = MigratedSettings.objects.get(pk="default")
        assert migrated.direct_transfer_enabled is False
        assert migrated.bank_bin == ""
        assert migrated.bank_name == ""
    finally:
        MigrationExecutor(connection).migrate([current_migration])


@pytest.mark.django_db
def test_forged_direct_transfer_is_blocked_and_old_payment_request_still_works(
    access_token, active_user
):
    ensure_phone(active_user)
    package, _product = create_package()
    own_client = bearer_client(access_token)

    forged = own_client.post(
        "/api/v1/memberships/requests",
        {
            "package_id": str(package.pk),
            "flow": "direct_transfer",
            "phone": "0901234567",
        },
        format="json",
    )
    assert forged.status_code == 409
    assert forged.json() == {"errors": [{"code": "direct_transfer_disabled"}]}
    assert not MembershipRequest.objects.filter(user=active_user).exists()

    settings = configure_payment_settings(enabled=True)
    request, instruction = create_membership_request(
        user=active_user,
        package_id=package.pk,
        flow="direct_transfer",
    )
    instruction.bank_code = "VCB"
    instruction.bank_bin = ""
    instruction.bank_name = ""
    instruction.save(update_fields=["bank_code", "bank_bin", "bank_name"])
    settings.direct_transfer_enabled = False
    settings.save(update_fields=["direct_transfer_enabled", "updated_at"])

    pending = own_client.get("/api/v1/memberships/requests")
    assert pending.status_code == 200
    assert pending.json()["data"]["payment_instruction"]["bank_code"] == "VCB"
    assert pending.json()["data"]["payment_instruction"]["bank_bin"] == ""
    assert pending.json()["data"]["payment_instruction"]["account_number"] == (
        instruction.account_number
    )
    submitted = own_client.post(
        f"/api/v1/memberships/requests/{request.pk}/payment-submitted",
        {},
        format="json",
    )
    assert submitted.status_code == 200
    assert submitted.json()["data"]["status"] == MembershipRequestStatus.PAYMENT_SUBMITTED
    assert submitted.json()["data"]["payment_instruction"] is not None


@pytest.mark.django_db
def test_admin_api_full_membership_workflow(admin_api, active_user):
    ensure_phone(active_user)
    cache_transfer_banks()
    settings_response = admin_api.put(
        "/api/v1/admin/membership-payment-settings",
        {
            "direct_transfer_enabled": True,
            "bank_bin": "970436",
            "account_number": "0123456789",
            "account_name": "NATURAL FARMING VIETNAM",
            "version": 0,
        },
        format="json",
    )
    assert settings_response.status_code == 200
    product_response = admin_api.post(
        "/api/v1/admin/membership-products",
        {
            "name": "Vegetable basket",
            "unit_size": "0.500",
            "unit_label_vi": "kg",
            "unit_label_en": "kg",
        },
        format="json",
    )
    assert product_response.status_code == 201
    product = product_response.json()["data"]
    assert "category" not in product
    product_patch = admin_api.patch(
        f"/api/v1/admin/membership-products/{product['id']}",
        {"short_description": "Updated through the admin API"},
        format="json",
    )
    assert product_patch.status_code == 200
    assert product_patch.json()["data"]["short_description"] == "Updated through the admin API"
    product_list = admin_api.get("/api/v1/admin/membership-products")
    product_detail = admin_api.get(f"/api/v1/admin/membership-products/{product['id']}")
    assert "category" not in product_list.json()["data"][0]
    assert "category" not in product_detail.json()["data"]
    package_response = admin_api.post(
        "/api/v1/admin/membership-packages",
        {
            "name": "Six month CSA",
            "upfront_price": "6000000",
            "duration_months": 6,
            "quota_policy": "rollover",
            "items": [{"product_id": product["id"], "quota_units": 3}],
        },
        format="json",
    )
    assert package_response.status_code == 201
    package = package_response.json()["data"]
    package_patch = admin_api.patch(
        f"/api/v1/admin/membership-packages/{package['id']}",
        {"description": "Updated package"},
        format="json",
    )
    assert package_patch.status_code == 200
    assert package_patch.json()["data"]["description"] == "Updated package"

    invalid_request = admin_api.post(
        "/api/v1/memberships/requests",
        {
            "package_id": package["id"],
            "flow": "direct_transfer",
            "amount": "1",
        },
        format="json",
    )
    assert invalid_request.status_code == 400

    request_response = admin_api.post(
        "/api/v1/memberships/requests",
        {
            "package_id": package["id"],
            "flow": "direct_transfer",
            "phone": "0901234567",
        },
        format="json",
    )
    assert request_response.status_code == 201
    request_id = request_response.json()["data"]["id"]
    assert (
        admin_api.post(
            f"/api/v1/memberships/requests/{request_id}/payment-submitted",
            {},
            format="json",
        ).status_code
        == 200
    )
    approve_response = admin_api.post(
        f"/api/v1/admin/membership-requests/{request_id}/approve",
        {"start_date": timezone.localdate().isoformat()},
        format="json",
    )
    assert approve_response.status_code == 201
    membership_id = approve_response.json()["data"]["id"]
    request_detail = admin_api.get(f"/api/v1/admin/membership-requests/{request_id}")
    assert [event["action"] for event in request_detail.json()["data"]["events"]] == [
        MembershipRequestEventAction.REQUEST_CREATED,
        MembershipRequestEventAction.PAYMENT_INSTRUCTION_ISSUED,
        MembershipRequestEventAction.PAYMENT_SUBMITTED,
        MembershipRequestEventAction.APPROVED,
    ]

    usage_response = admin_api.post(
        "/api/v1/admin/membership-usage",
        {
            "membership_id": membership_id,
            "idempotency_key": str(uuid.uuid4()),
            "lines": [{"product_id": product["id"], "units": 1}],
        },
        format="json",
    )
    assert usage_response.status_code == 201
    usage_id = usage_response.json()["data"]["id"]
    undo_response = admin_api.post(
        f"/api/v1/admin/membership-usage/{usage_id}/undo",
        {"reason": "API integration test"},
        format="json",
    )
    assert undo_response.status_code == 200
    assert undo_response.json()["data"]["status"] == UsageOrderStatus.REVERSED


@pytest.mark.django_db
def test_self_api_authorization_ownership_and_repeated_submit(
    access_token, active_user, oauth_application
):
    ensure_phone(active_user)
    package, _product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=active_user, package_id=package.pk, flow="direct_transfer"
    )
    own_client = bearer_client(access_token)
    first = own_client.post(
        f"/api/v1/memberships/requests/{request.pk}/payment-submitted", {}, format="json"
    )
    second = own_client.post(
        f"/api/v1/memberships/requests/{request.pk}/payment-submitted", {}, format="json"
    )
    assert first.status_code == second.status_code == 200
    assert second.json()["meta"]["idempotent_replay"] is True
    assert first["Cache-Control"] == "private, no-store"

    other = create_active_user("membership-other@example.com")
    other_token = AccessToken.objects.create(
        user=other,
        application=oauth_application,
        token="membership-other-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )
    assert (
        bearer_client(other_token)
        .post(
            f"/api/v1/memberships/requests/{request.pk}/payment-submitted",
            {},
            format="json",
        )
        .status_code
        == 404
    )
    assert APIClient().get("/api/v1/memberships/quota").status_code == 401


@pytest.mark.django_db
def test_admin_permission_crud_actions_and_usage_pagination(admin_api, active_user, access_token):
    assert (
        admin_api.get("/api/v1/admin/membership-categories").status_code == 404
    )

    rejected_legacy_contract = admin_api.post(
        "/api/v1/admin/membership-products",
        {
            "category_id": str(uuid.uuid4()),
            "name": "Legacy product",
            "unit_size": "0.500",
            "unit_label_vi": "kg",
            "unit_label_en": "kg",
        },
        format="json",
    )
    assert rejected_legacy_contract.status_code == 400
    assert rejected_legacy_contract.json() == {
        "category_id": "This field is not allowed."
    }

    membership, product, _request = activate_membership(active_user, quota=100)
    for _index in range(31):
        create_usage_order(
            membership_id=membership.pk,
            actor=active_user,
            idempotency_key=uuid.uuid4(),
            lines=[{"product_id": product.pk, "units": 1}],
        )
    response = admin_api.get(f"/api/v1/admin/memberships/{membership.pk}/history?page=2")
    assert response.status_code == 200
    assert response.json()["meta"] == {"page": 2, "page_size": 30, "total": 31}
    assert len(response.json()["data"]) == 1


@pytest.mark.django_db
def test_non_admin_cannot_access_admin_membership_api(access_token):
    response = bearer_client(access_token).get("/api/v1/admin/membership-requests")
    assert response.status_code == 403


def test_mysql_safe_slot_constraints_do_not_use_partial_indexes():
    request_constraint = next(
        item
        for item in MembershipRequest._meta.constraints
        if item.name == "memberships_one_open_request_per_user"
    )
    membership_constraint = next(
        item
        for item in Membership._meta.constraints
        if item.name == "memberships_one_current_per_user"
    )
    assert request_constraint.condition is None
    assert request_constraint.fields == ("user", "open_slot")
    assert membership_constraint.condition is None
    assert membership_constraint.fields == ("user", "current_slot")


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_approval_creates_exactly_one_membership():
    user = create_active_user("concurrent-membership@example.com")
    package, _product = create_package()
    configure_payment_settings()
    request, _instruction = create_membership_request(
        user=user, package_id=package.pk, flow="direct_transfer"
    )
    submit_payment(request_id=request.pk, user=user)
    barrier = threading.Barrier(3)
    outcomes = []

    def approve():
        close_old_connections()
        try:
            barrier.wait()
            _membership, created = approve_request(
                request_id=request.pk,
                actor=User.objects.get(pk=user.pk),
                start_date=timezone.localdate(),
            )
            outcomes.append("created" if created else "replay")
        except RequestTransitionConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=approve) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "replay"]
    assert Membership.objects.filter(user=user).count() == 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_payment_settings_update_rejects_one_stale_writer(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    settings = MembershipPaymentSettings.objects.create(
        key="default",
        direct_transfer_enabled=False,
        account_number="0123456789",
        account_name="ORIGINAL",
    )
    barrier = threading.Barrier(3)
    outcomes = []

    def update_settings(name):
        close_old_connections()
        try:
            barrier.wait()
            response = bearer_client(access_token).patch(
                "/api/v1/admin/membership-payment-settings",
                {"account_name": name, "version": settings.version},
                format="json",
            )
            outcomes.append(response.status_code)
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=update_settings, args=(name,))
        for name in ("ADMIN ONE", "ADMIN TWO")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [200, 409]
    settings.refresh_from_db()
    assert settings.account_name in {"ADMIN ONE", "ADMIN TWO"}
    assert settings.version == 2


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_usage_cannot_overdraw_quota():
    user = create_active_user("concurrent-usage@example.com")
    membership, product, _request = activate_membership(user, quota=3)
    barrier = threading.Barrier(3)
    outcomes = []

    def use_quota():
        close_old_connections()
        try:
            barrier.wait()
            create_usage_order(
                membership_id=membership.pk,
                actor=User.objects.get(pk=user.pk),
                idempotency_key=uuid.uuid4(),
                lines=[{"product_id": product.pk, "units": 2}],
            )
            outcomes.append("created")
        except InsufficientQuota:
            outcomes.append("insufficient")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=use_quota) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["created", "insufficient"]
    quota = MembershipCycleQuota.objects.get(cycle__membership=membership)
    assert quota.remaining_units == 1


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_current_and_quota_views_initialize_cycles_once(active_user, access_token):
    membership, _product, _request = activate_membership(active_user)
    MembershipCycleQuota.objects.filter(cycle__membership=membership).delete()
    membership.cycles.all().delete()
    barrier = threading.Barrier(3)
    outcomes = []

    def load(path):
        close_old_connections()
        try:
            barrier.wait()
            response = bearer_client(access_token).get(path)
            outcomes.append((path, response.status_code))
        finally:
            close_old_connections()

    threads = [
        threading.Thread(target=load, args=(path,))
        for path in ("/api/v1/memberships/current", "/api/v1/memberships/quota")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == [
        ("/api/v1/memberships/current", 200),
        ("/api/v1/memberships/quota", 200),
    ]
    assert list(membership.cycles.values_list("sequence", flat=True)) == [1]
    assert MembershipCycleQuota.objects.filter(cycle__membership=membership).count() == 1


def test_add_months_clamps_anniversary_day():
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
