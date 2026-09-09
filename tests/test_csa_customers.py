import threading
import uuid
from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AccountStatus, AuditLog, User
from members.models import MemberProfile
from memberships.models import CSACustomer, Membership, MembershipStatus
from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone
from memberships.services import (
    CSACustomerRequiredForMembership,
    DuplicateCSACustomerPhone,
    DuplicateCSACustomerUser,
    MembershipOverlap,
    assign_membership,
    create_csa_customer_for_user,
    create_managed_csa_customer,
    link_csa_customer_to_user,
    revoke_membership,
)
from tests.test_memberships import create_active_user, create_package

VALID_CUSTOMER_FIELDS = {
    "province_code": "66",
    "ward_code": "22015",
    "address": "Số 12, ngõ 5",
}


def create_complete_managed_customer(*, name="Managed", phone="0901234567"):
    return create_managed_csa_customer(
        name=name,
        phone=phone,
        **VALID_CUSTOMER_FIELDS,
    )


def create_complete_customer_for_user(user, *, phone="0901234567"):
    return create_csa_customer_for_user(
        user=user,
        name=user.name,
        phone=phone,
        **VALID_CUSTOMER_FIELDS,
    )


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture
def admin_api(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0901234567", "+84901234567"),
        ("84901234567", "+84901234567"),
        ("+84901234567", "+84901234567"),
    ],
)
def test_phone_normalization(raw, expected):
    assert normalize_vietnam_phone(raw) == expected


@pytest.mark.parametrize("raw", ["901234567", "+12025550123", "call-me", "0123"])
def test_phone_normalization_rejects_ambiguous_or_invalid_values(raw):
    with pytest.raises(InvalidPhoneNumber):
        normalize_vietnam_phone(raw)


@pytest.mark.django_db
def test_customer_model_constraints_and_nullable_phone():
    user = create_active_user("customer-model@example.com", phone=None)
    linked = CSACustomer.objects.create(user=user, name="Linked", phone_e164=None)
    managed = CSACustomer.objects.create(name="Managed", phone_e164=None)
    assert linked.user == user
    assert managed.user is None
    with pytest.raises(IntegrityError):
        CSACustomer.objects.create(user=user, name="Duplicate user")


@pytest.mark.django_db
def test_customer_services_do_not_create_users_or_match_by_phone():
    before = User.objects.count()
    managed = create_complete_managed_customer(name="Walk-in")
    assert managed.user is None
    assert managed.phone_e164 == "+84901234567"
    assert User.objects.count() == before
    with pytest.raises(DuplicateCSACustomerPhone):
        create_complete_managed_customer(name="Duplicate", phone="+84901234567")

    user = create_active_user("same-phone@example.com", phone=None)
    with pytest.raises(DuplicateCSACustomerPhone):
        create_complete_customer_for_user(user, phone="84901234567")
    assert not CSACustomer.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_customer_for_user_is_unique_and_requires_complete_request_data():
    user = create_active_user("linked-customer@example.com", phone=None)
    customer = create_complete_customer_for_user(user)
    assert customer.name == user.name
    assert customer.phone_e164 == "+84901234567"
    with pytest.raises(DuplicateCSACustomerUser):
        create_complete_customer_for_user(user)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("name", None, "csa_customer_name_required"),
        ("phone", None, "csa_customer_phone_required"),
        ("province_code", None, "csa_customer_province_required"),
        ("ward_code", None, "csa_customer_ward_required"),
        ("address", None, "csa_customer_address_required"),
        ("address", " ", "csa_customer_address_required"),
        ("phone", "0123", "invalid_csa_customer_phone"),
        ("ward_code", "00004", "invalid_csa_customer_address"),
    ],
)
def test_admin_managed_customer_requires_complete_valid_data(admin_api, field, value, code):
    payload = {"name": "Managed", "phone": "0901234567", **VALID_CUSTOMER_FIELDS}
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    response = admin_api.post("/api/v1/admin/csa-customers", payload, format="json")

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": code}]
    assert not CSACustomer.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("name", "csa_customer_name_required"),
        ("phone", "csa_customer_phone_required"),
        ("province_code", "csa_customer_province_required"),
        ("ward_code", "csa_customer_ward_required"),
        ("address", "csa_customer_address_required"),
    ],
)
def test_admin_linked_customer_does_not_backfill_missing_request_data(admin_api, field, code):
    user = create_active_user("linked-required@example.com")
    payload = {
        "user_id": str(user.pk),
        "name": user.name,
        "phone": "0901234567",
        **VALID_CUSTOMER_FIELDS,
    }
    payload.pop(field)

    response = admin_api.post("/api/v1/admin/csa-customers", payload, format="json")

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": code}]
    assert not CSACustomer.objects.filter(user=user).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("name", "csa_customer_name_required"),
        ("phone", "csa_customer_phone_required"),
        ("province_code", "csa_customer_province_required"),
        ("ward_code", "csa_customer_ward_required"),
        ("address", "csa_customer_address_required"),
    ],
)
def test_admin_customer_patch_cannot_remove_required_data(admin_api, field, code):
    customer = create_complete_managed_customer()

    response = admin_api.patch(
        f"/api/v1/admin/csa-customers/{customer.pk}",
        {field: " "},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": code}]
    customer.refresh_from_db()
    assert customer.name == "Managed"
    assert customer.phone_e164 == "+84901234567"
    assert customer.province_code == "66"
    assert customer.ward_code == "22015"
    assert customer.address == "Số 12, ngõ 5"


@pytest.mark.django_db
def test_admin_customer_patch_requires_new_ward_when_province_changes(admin_api):
    customer = create_complete_managed_customer()

    response = admin_api.patch(
        f"/api/v1/admin/csa-customers/{customer.pk}",
        {"province_code": "01"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_customer_ward_required"}]


@pytest.mark.django_db
def test_admin_customer_crud_filters_and_strict_fields(admin_api):
    managed_response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {
            "name": "Khách cửa hàng",
            "phone": "0901234567",
            "province_code": "66",
            "ward_code": "22015",
            "address": "Số 12, ngõ 5",
        },
        format="json",
    )
    assert managed_response.status_code == 201
    managed = managed_response.json()["data"]
    assert managed["phone"] == "+84901234567"
    assert managed["user"] is None
    assert managed["province_code"] == "66"
    assert managed["ward_code"] == "22015"
    assert managed["address"] == "Số 12, ngõ 5"

    user = create_active_user("api-linked@example.com", phone=None)
    MemberProfile.objects.create(
        user=user,
        phone_number="0908888888",
        province_code="66",
        ward_code="22015",
        address="Địa chỉ từ hồ sơ",
    )
    linked_response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {
            "user_id": str(user.pk),
            "name": user.name,
            "phone": "0908888888",
            "province_code": "66",
            "ward_code": "22015",
            "address": "Địa chỉ gửi từ admin",
        },
        format="json",
    )
    assert linked_response.status_code == 201
    linked_data = linked_response.json()["data"]
    assert linked_data["user"]["email"] == user.email
    assert linked_data["phone"] == "+84908888888"
    assert linked_data["province_code"] == "66"
    assert linked_data["ward_code"] == "22015"
    assert linked_data["address"] == "Địa chỉ gửi từ admin"
    user.member_profile.refresh_from_db()
    assert user.member_profile.address == "Địa chỉ từ hồ sơ"

    listed = admin_api.get(
        "/api/v1/admin/csa-customers",
        {"linked": "managed", "search": "Khách", "page": 1, "page_size": 10},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [managed["id"]]
    assert listed.json()["meta"]["page_size"] == 10

    patched = admin_api.patch(
        f"/api/v1/admin/csa-customers/{managed['id']}",
        {"name": "Khách CSA", "phone": "84909999999", "address": "Số 20"},
        format="json",
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["phone"] == "+84909999999"
    assert patched.json()["data"]["address"] == "Số 20"

    invalid_address = admin_api.patch(
        f"/api/v1/admin/csa-customers/{managed['id']}",
        {"province_code": "01", "ward_code": "22015"},
        format="json",
    )
    assert invalid_address.status_code == 400
    assert invalid_address.json()["errors"] == [{"code": "invalid_csa_customer_address"}]

    unknown = admin_api.patch(
        f"/api/v1/admin/csa-customers/{managed['id']}",
        {"user_id": str(user.pk)},
        format="json",
    )
    assert unknown.status_code == 400
    assert unknown.json()["user_id"] == "This field is not allowed."
    assert admin_api.delete(f"/api/v1/admin/csa-customers/{managed['id']}").status_code == 405


@pytest.mark.django_db
def test_admin_customer_create_maps_duplicate_user_and_phone_to_stable_conflicts(admin_api):
    user = create_active_user("duplicate-linked@example.com")
    create_complete_customer_for_user(user)
    linked_response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {
            "user_id": str(user.pk),
            "name": user.name,
            "phone": "0907777777",
            **VALID_CUSTOMER_FIELDS,
        },
        format="json",
    )
    assert linked_response.status_code == 409
    assert linked_response.json()["errors"] == [{"code": "csa_customer_user_exists"}]

    phone_response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {"name": "Duplicate phone", "phone": "0901234567", **VALID_CUSTOMER_FIELDS},
        format="json",
    )
    assert phone_response.status_code == 409
    assert phone_response.json()["errors"] == [{"code": "csa_customer_phone_exists"}]


@pytest.mark.django_db
def test_customer_api_requires_scope_and_group(active_user, access_token):
    assert APIClient().get("/api/v1/admin/csa-customers").status_code == 401
    client = bearer_client(access_token)
    assert client.get("/api/v1/admin/csa-customers").status_code == 403
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    assert client.get("/api/v1/admin/csa-customers").status_code == 403
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    active_user.groups.clear()
    assert client.get("/api/v1/admin/csa-customers").status_code == 403


@pytest.mark.django_db
def test_admin_can_link_user_once_with_audit_and_cannot_unlink(admin_api, active_user):
    customer = create_complete_managed_customer()
    response = admin_api.post(
        f"/api/v1/admin/csa-customers/{customer.pk}/link-user",
        {"user_id": str(active_user.pk)},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["data"]["user"]["id"] == str(active_user.pk)
    audit = AuditLog.objects.get(action="csa_customer.link_user")
    assert audit.user_id == active_user.pk
    assert audit.context == {
        "customer_id": str(customer.pk),
        "linked_user_id": str(active_user.pk),
    }
    assert (
        admin_api.post(
            f"/api/v1/admin/csa-customers/{customer.pk}/link-user",
            {"user_id": str(active_user.pk)},
            format="json",
        ).status_code
        == 409
    )
    assert (
        admin_api.patch(
            f"/api/v1/admin/csa-customers/{customer.pk}",
            {"user": None},
            format="json",
        ).status_code
        == 400
    )


@pytest.mark.django_db
def test_link_rejects_user_owned_by_another_customer(active_user):
    create_complete_customer_for_user(active_user)
    managed = create_complete_managed_customer(phone="0907777777")
    actor = create_active_user("link-actor@example.com", phone=None)
    with pytest.raises(DuplicateCSACustomerUser):
        link_csa_customer_to_user(customer=managed, user=active_user, actor=actor)


@pytest.mark.django_db
def test_managed_customer_assignment_and_self_ownership(admin_api, active_user, access_token):
    customer = create_complete_managed_customer()
    package, _product, option = create_package()
    response = admin_api.post(
        "/api/v1/admin/memberships",
        {
            "customer_id": str(customer.pk),
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "start_date": timezone.localdate().isoformat(),
            "farmbrite_order_reference": "FB-MANAGED",
        },
        format="json",
    )
    assert response.status_code == 201
    assert response.json()["data"]["customer"]["user"] is None
    assert not User.objects.filter(email__icontains="managed").exists()

    self_client = bearer_client(access_token)
    assert self_client.get("/api/v1/memberships/current").json()["data"] is None
    assert self_client.get("/api/v1/memberships/quota").json()["data"] is None
    assert self_client.get("/api/v1/memberships/usage").json()["data"] == []
    assert (
        self_client.get(
            "/api/v1/memberships/current", {"customer_id": str(customer.pk)}
        ).status_code
        == 400
    )


@pytest.mark.django_db
def test_membership_assignment_requires_one_complete_canonical_customer(admin_api, active_user):
    package, _product, option = create_package()
    base = {
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "farmbrite_order_reference": "FB-CUSTOMER-REQUIRED",
    }

    missing = admin_api.post("/api/v1/admin/memberships", base, format="json")
    assert missing.status_code == 400
    assert missing.json()["errors"] == [{"code": "csa_customer_required_for_membership"}]

    user_only = admin_api.post(
        "/api/v1/admin/memberships",
        {**base, "user_id": str(active_user.pk)},
        format="json",
    )
    assert user_only.status_code == 400
    assert user_only.json()["user_id"] == "This field is not allowed."
    assert not CSACustomer.objects.filter(user=active_user).exists()

    complete_customer = create_complete_customer_for_user(active_user)
    both = admin_api.post(
        "/api/v1/admin/memberships",
        {
            **base,
            "customer_id": str(complete_customer.pk),
            "user_id": str(active_user.pk),
        },
        format="json",
    )
    assert both.status_code == 400
    assert both.json()["user_id"] == "This field is not allowed."

    legacy = CSACustomer.objects.create(name="Legacy incomplete")
    incomplete = admin_api.post(
        "/api/v1/admin/memberships",
        {**base, "customer_id": str(legacy.pk)},
        format="json",
    )
    assert incomplete.status_code == 400
    assert incomplete.json()["errors"] == [{"code": "csa_customer_incomplete"}]
    assert not Membership.objects.exists()


@pytest.mark.django_db
def test_memberships_schedule_back_to_back_and_self_returns_only_active(
    active_user, access_token, admin_api
):
    customer = create_complete_customer_for_user(active_user)
    package, _product, option = create_package(duration=1)
    first = assign_membership(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate(),
        farmbrite_order_reference="FB-SEQUENCE-1",
        actor=active_user,
    )
    second_response = admin_api.post(
        "/api/v1/admin/memberships",
        {
            "customer_id": str(customer.pk),
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "farmbrite_order_reference": "FB-SEQUENCE-2",
        },
        format="json",
    )
    assert second_response.status_code == 201
    second = customer.memberships.get(pk=second_response.json()["data"]["id"])
    third = assign_membership(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=None,
        farmbrite_order_reference="FB-SEQUENCE-3",
        actor=active_user,
    )
    assert second.start_date == first.end_date
    assert third.start_date == second.end_date
    assert first.status == MembershipStatus.ACTIVE
    assert second.status == third.status == MembershipStatus.SCHEDULED
    assert bearer_client(access_token).get("/api/v1/memberships/current").json()["data"][
        "id"
    ] == str(first.pk)
    customer_data = admin_api.get(f"/api/v1/admin/csa-customers/{customer.pk}").json()["data"]
    assert customer_data["status"] == "active"
    assert customer_data["current_membership"]["id"] == str(first.pk)
    assert customer_data["next_scheduled_membership"]["id"] == str(second.pk)

    with pytest.raises(MembershipOverlap):
        assign_membership(
            customer_id=customer.pk,
            package_id=package.pk,
            price_option_id=option.pk,
            start_date=first.start_date,
            farmbrite_order_reference="FB-OVERLAP",
            actor=active_user,
        )


@pytest.mark.django_db
def test_self_current_does_not_return_scheduled_membership(active_user, access_token):
    customer = create_complete_customer_for_user(active_user)
    package, _product, option = create_package(duration=1)
    assign_membership(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        start_date=timezone.localdate() + timezone.timedelta(days=1),
        farmbrite_order_reference="FB-FUTURE-ONLY",
        actor=active_user,
    )
    client = bearer_client(access_token)
    assert client.get("/api/v1/memberships/current").json()["data"] is None
    assert client.get("/api/v1/memberships/quota").json()["data"] is None


@pytest.mark.django_db
def test_customer_survives_when_all_memberships_are_revoked(active_user):
    customer = create_complete_managed_customer(name="Historical")
    package, _product, option = create_package(duration=1)
    membership = assign_membership(
        customer_id=customer.pk,
        package_id=package.pk,
        price_option_id=option.pk,
        farmbrite_order_reference="FB-HISTORICAL",
        actor=active_user,
    )
    revoke_membership(membership_id=membership.pk, reason="Historical", actor=active_user)
    assert CSACustomer.objects.filter(pk=customer.pk).exists()
    assert customer.memberships.get().status == MembershipStatus.REVOKED


@pytest.mark.django_db
def test_assignment_without_customer_never_creates_one(active_user):
    package, _product, option = create_package()
    with pytest.raises(CSACustomerRequiredForMembership):
        assign_membership(
            package_id=package.pk,
            price_option_id=option.pk,
            start_date=timezone.localdate(),
            farmbrite_order_reference="FB-ROLLBACK",
            actor=active_user,
        )
    assert not CSACustomer.objects.filter(user=active_user).exists()


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_managed_customer_creation_is_unique():
    barrier = threading.Barrier(3)
    outcomes = []

    def create_once():
        close_old_connections()
        try:
            barrier.wait()
            create_complete_managed_customer(name="Concurrent")
            outcomes.append("created")
        except DuplicateCSACustomerPhone:
            outcomes.append("duplicate")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=create_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["created", "duplicate"]
    assert CSACustomer.objects.filter(phone_e164="+84901234567").count() == 1


@pytest.mark.django_db(transaction=True)
def test_customer_migration_backfills_only_membership_users_without_losing_history():
    migrate_from = ("memberships", "0009_revoke_membership_fields")
    migrate_to = ("memberships", "0011_csa_customer_finalize")
    members_target = ("members", "0002_member_profile_administrative_codes")
    executor = MigrationExecutor(connection)
    try:
        executor.migrate([migrate_from, members_target])
        old = executor.loader.project_state([migrate_from, members_target]).apps
        OldUser = old.get_model("identity", "User")
        OldProfile = old.get_model("members", "MemberProfile")
        OldProduct = old.get_model("memberships", "Product")
        OldPackage = old.get_model("memberships", "MembershipPackage")
        OldOption = old.get_model("memberships", "MembershipPackagePriceOption")
        OldMembership = old.get_model("memberships", "Membership")
        OldSnapshot = old.get_model("memberships", "MembershipProductSnapshot")
        OldCycle = old.get_model("memberships", "MembershipCycle")
        OldQuota = old.get_model("memberships", "MembershipCycleQuota")
        OldUsage = old.get_model("memberships", "MembershipUsageOrder")

        buyer = OldUser.objects.create(
            email="migration-buyer@example.com",
            name="Migration Buyer",
            password="!",
            status=AccountStatus.ACTIVE,
            is_active=True,
        )
        no_phone = OldUser.objects.create(
            email="migration-no-phone@example.com",
            name="No Phone",
            password="!",
            status=AccountStatus.ACTIVE,
            is_active=True,
        )
        non_buyer = OldUser.objects.create(
            email="migration-non-buyer@example.com",
            name="Non Buyer",
            password="!",
            status=AccountStatus.ACTIVE,
            is_active=True,
        )
        OldProfile.objects.create(
            user=buyer,
            phone_number="0901234567",
            province_code="66",
            ward_code="22015",
            address="Địa chỉ lịch sử",
        )
        product = OldProduct.objects.create(
            name="Basket",
            unit_size="0.500",
            unit_label_vi="kg",
            unit_label_en="kg",
        )
        package = OldPackage.objects.create(name="CSA", quota_policy="rollover")
        option = OldOption.objects.create(
            package=package,
            duration_months=1,
            monthly_price_vnd=100000,
            active_duration_key=1,
        )

        def old_membership(user, reference, current_slot):
            return OldMembership.objects.create(
                user=user,
                package=package,
                price_option=option,
                package_name_snapshot="CSA",
                monthly_price_vnd_snapshot=100000,
                total_price_vnd_snapshot=100000,
                duration_months_snapshot=1,
                quota_policy_snapshot="rollover",
                farmbrite_order_reference=reference,
                farmbrite_order_reference_key=reference.lower(),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 2, 1),
                status="active" if current_slot else "ended",
                current_slot=current_slot,
                activated_by=buyer,
            )

        first = old_membership(buyer, "MIG-1", True)
        second = old_membership(buyer, "MIG-2", None)
        old_membership(no_phone, "MIG-3", True)
        snapshot = OldSnapshot.objects.create(
            membership=first,
            product=product,
            product_name="Basket",
            unit_size="0.500",
            unit_label_vi="kg",
            unit_label_en="kg",
            quota_units_per_cycle=2,
        )
        cycle = OldCycle.objects.create(
            membership=first,
            sequence=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 2, 1),
        )
        OldQuota.objects.create(
            cycle=cycle,
            product_snapshot=snapshot,
            granted_units=2,
            remaining_units=1,
        )
        OldUsage.objects.create(
            membership=first,
            user=buyer,
            idempotency_key=uuid.uuid4(),
            created_by=buyer,
        )

        executor = MigrationExecutor(connection)
        executor.migrate([migrate_to])
        new = executor.loader.project_state([migrate_to]).apps
        Customer = new.get_model("memberships", "CSACustomer")
        NewMembership = new.get_model("memberships", "Membership")
        NewUsage = new.get_model("memberships", "MembershipUsageOrder")
        assert Customer.objects.count() == 2
        buyer_customer = Customer.objects.get(user_id=buyer.pk)
        assert buyer_customer.phone_e164 == "+84901234567"
        assert buyer_customer.province_code == "66"
        assert buyer_customer.ward_code == "22015"
        assert buyer_customer.address == "Địa chỉ lịch sử"
        assert Customer.objects.get(user_id=no_phone.pk).phone_e164 is None
        assert not Customer.objects.filter(user_id=non_buyer.pk).exists()
        assert set(
            NewMembership.objects.filter(pk__in=[first.pk, second.pk]).values_list(
                "customer_id", flat=True
            )
        ) == {buyer_customer.pk}
        assert NewUsage.objects.get(membership_id=first.pk).customer_id == buyer_customer.pk
        assert new.get_model("memberships", "MembershipCycleQuota").objects.count() == 1
    finally:
        MigrationExecutor(connection).migrate([migrate_to])
