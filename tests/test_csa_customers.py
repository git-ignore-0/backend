import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from django.contrib.auth.models import Group
from django.db import IntegrityError, close_old_connections, connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone
from rest_framework.test import APIClient

from commerce.models import ShippingAddress, ShippingArea
from config.phones import InvalidPhoneNumber, normalize_vietnam_phone
from identity.models import AccountStatus, AuditLog, User
from identity.services import transition_account
from members.models import MemberProfile
from memberships.models import (
    CSACustomer,
    Membership,
    MembershipPaymentSettings,
    MembershipStatus,
)
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
CUSTOMERS = "/api/v1/admin/csa-customers"


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


@pytest.fixture
def supported_customer_address():
    ShippingArea.objects.create(
        province_code=VALID_CUSTOMER_FIELDS["province_code"],
        ward_code=VALID_CUSTOMER_FIELDS["ward_code"],
        shipping_fee_vnd=30_000,
        is_active=True,
    )
    return VALID_CUSTOMER_FIELDS.copy()


@pytest.fixture
def manual_payment_settings(monkeypatch, settings, tmp_path):
    bank = {"bin": "970436", "name": "Vietcombank"}
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [bank])
    settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT = tmp_path / "contracts"
    return MembershipPaymentSettings.objects.create(
        bank_bin=bank["bin"],
        bank_name=bank["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
    )


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
def test_admin_managed_customer_requires_supported_shipping_area(admin_api):
    response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {"name": "Managed", "phone": "0901234567", **VALID_CUSTOMER_FIELDS},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_customer_shipping_area_unsupported"}]
    assert not CSACustomer.objects.exists()


@pytest.mark.django_db
def test_admin_customer_create_rejects_user_id(admin_api, active_user):
    response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {
            "user_id": str(active_user.pk),
            "name": "Managed",
            "phone": "0901234567",
            **VALID_CUSTOMER_FIELDS,
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["user_id"] == "This field is not allowed."
    assert not CSACustomer.objects.exists()


@pytest.mark.django_db
def test_admin_customer_patch_is_not_available(admin_api):
    customer = create_complete_managed_customer()

    response = admin_api.patch(
        f"/api/v1/admin/csa-customers/{customer.pk}",
        {"name": "Changed"},
        format="json",
    )

    assert response.status_code == 405
    customer.refresh_from_db()
    assert customer.name == "Managed"


@pytest.mark.django_db
def test_admin_customer_create_list_and_delete_rejection(admin_api):
    ShippingArea.objects.create(
        province_code="66",
        ward_code="22015",
        shipping_fee_vnd=30_000,
        is_active=True,
    )
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
    assert managed["note"] == ""

    listed = admin_api.get(
        "/api/v1/admin/csa-customers",
        {"linked": "managed", "search": "Khách", "page": 1, "page_size": 10},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]] == [managed["id"]]
    assert listed.json()["meta"]["page_size"] == 10

    assert admin_api.delete(f"/api/v1/admin/csa-customers/{managed['id']}").status_code == 405


@pytest.mark.django_db
def test_admin_customer_search_matches_domestic_phone_before_pagination(admin_api):
    target = CSACustomer.objects.create(name="Target", phone_e164="+84981234567")
    longer = CSACustomer.objects.create(name="Longer", phone_e164="+849812345678")
    landline = CSACustomer.objects.create(name="Landline", phone_e164="+842412345678")
    CSACustomer.objects.create(name="No Phone", phone_e164=None)

    def search(value):
        response = admin_api.get(CUSTOMERS, {"search": value, "page_size": 1})
        assert response.status_code == 200
        return response.json()

    exact = search("0981234567")
    assert exact["meta"]["total"] == 1
    assert [row["id"] for row in exact["data"]] == [str(target.pk)]
    assert exact["data"][0]["phone"] == "+84981234567"
    for fragment in ("098", "098123", "981234"):
        result = search(fragment)
        assert result["meta"]["total"] == 2
        assert result["data"][0]["id"] in {str(target.pk), str(longer.pk)}
    for value, customer in (
        ("0241234567", landline),
        ("02412345678", landline),
        ("09812345678", longer),
    ):
        result = search(value)
        assert result["meta"]["total"] == 1
        assert [row["id"] for row in result["data"]] == [str(customer.pk)]
    assert search("4567")["meta"]["total"] == 3
    assert search("No Phone")["meta"]["total"] == 1
    for value in ("+84981234567", "84981234567", "8498"):
        assert search(value)["data"] == []


@pytest.mark.django_db
def test_admin_customer_note_is_saved_and_returned_only_in_create_and_detail(admin_api):
    ShippingArea.objects.create(
        province_code="66", ward_code="22015", shipping_fee_vnd=30_000, is_active=True
    )
    path = "/api/v1/admin/csa-customers"
    body = {"name": "Khách cửa hàng", "phone": "0901234567", **VALID_CUSTOMER_FIELDS}
    rejected = admin_api.post(path, {**body, "note": "a" * 1001}, format="json")
    assert rejected.status_code == 400
    assert "note" in rejected.json()
    assert not CSACustomer.objects.exists()

    note = "Gọi trước khi giao.\nKhách nhận vào buổi sáng."
    response = admin_api.post(path, {**body, "note": f"  {note}  "}, format="json")
    assert response.status_code == 201, response.json()
    item = response.json()["data"]
    assert item["note"] == note
    assert CSACustomer.objects.get(pk=item["id"]).note == note
    assert admin_api.get(f"{path}/{item['id']}").json()["data"]["note"] == note
    assert "note" not in admin_api.get(path).json()["data"][0]


@pytest.mark.django_db
def test_admin_customer_create_maps_duplicate_phone_to_stable_conflict(admin_api):
    ShippingArea.objects.create(
        province_code="66",
        ward_code="22015",
        shipping_fee_vnd=30_000,
        is_active=True,
    )
    create_complete_managed_customer()

    phone_response = admin_api.post(
        "/api/v1/admin/csa-customers",
        {"name": "Duplicate phone", "phone": "0901234567", **VALID_CUSTOMER_FIELDS},
        format="json",
    )
    assert phone_response.status_code == 409
    assert phone_response.json()["errors"] == [{"code": "csa_customer_phone_exists"}]


@pytest.mark.django_db
def test_admin_customer_lookup_classifies_normalized_phone(admin_api):
    user = User.objects.create_user("lookup@example.com", name="Core name")
    MemberProfile.objects.create(user=user, phone_number="0912345678")

    def lookup(phone):
        response = admin_api.post(f"{CUSTOMERS}/lookup", {"phone": phone}, format="json")
        assert response.status_code == 200
        assert {"private", "no-store"} <= set(response["Cache-Control"].split(", "))
        return response.json()["data"]

    expected = {"phone": "+84912345678", "status": "available"}
    for phone in ("0912345678", "84912345678", "+84912345678"):
        assert lookup(phone) == expected

    user.login_phone_number = "0912345678"
    user.save(update_fields=["login_phone_number"])
    assert lookup("0912345678") == {
        "phone": "+84912345678",
        "status": "account_found",
        "user": {"id": str(user.pk), "name": "Core name"},
    }
    assert not CSACustomer.objects.exists()

    linked = create_complete_customer_for_user(user, phone="0971234567")
    assert lookup("0912345678") == {
        "phone": "+84912345678",
        "status": "customer_exists",
        "customer": {"id": str(linked.pk), "is_linked": True},
    }

    managed = create_complete_managed_customer(phone="0912345678")
    assert lookup("0912345678") == {
        "phone": "+84912345678",
        "status": "customer_exists",
        "customer": {"id": str(managed.pk), "is_linked": False},
    }
    assert CSACustomer.objects.count() == 2
    invalid = admin_api.post(f"{CUSTOMERS}/lookup", {"phone": "0123"}, format="json")
    assert invalid.status_code == 400
    assert invalid.json()["errors"] == [{"code": "invalid_csa_customer_phone"}]


@pytest.mark.django_db
def test_admin_customer_create_rechecks_account_after_lookup(admin_api, supported_customer_address):
    payload = {"name": "Managed", "phone": "0912345678", **supported_customer_address}
    lookup = admin_api.post(f"{CUSTOMERS}/lookup", {"phone": payload["phone"]}, format="json")
    assert lookup.json()["data"]["status"] == "available"

    user = User.objects.create_user(
        "new-account@example.com", name="Core name", login_phone_number="0912345678"
    )
    response = admin_api.post(CUSTOMERS, payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_customer_phone_has_account"}]
    assert not CSACustomer.objects.exists()

    linked = create_complete_customer_for_user(user, phone="0971234567")
    response = admin_api.post(CUSTOMERS, payload, format="json")
    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_customer_user_exists"}]
    assert list(CSACustomer.objects.values_list("id", flat=True)) == [linked.pk]


@pytest.mark.django_db
def test_admin_customer_create_from_user_uses_core_data(admin_api, supported_customer_address):
    user = User.objects.create_user(
        "customer-from-user@example.com", name="Core name", login_phone_number="0912345678"
    )
    MemberProfile.objects.create(user=user, phone_number="0931234567", address="Legacy address")
    ShippingAddress.objects.create(
        user=user,
        recipient_name="Old recipient",
        phone_number="0941234567",
        **supported_customer_address,
    )
    response = admin_api.post(f"{CUSTOMERS}/from-user", {"user_id": user.pk}, format="json")

    assert response.status_code == 201
    assert {"private", "no-store"} <= set(response["Cache-Control"].split(", "))
    data = response.json()["data"]
    assert data["user"]["id"] == str(user.pk)
    assert data["name"] == "Core name" and data["phone"] == "+84912345678"
    assert {key: data[key] for key in supported_customer_address} == supported_customer_address
    customer = CSACustomer.objects.get(pk=data["id"])
    assert customer.user_id == user.pk
    assert customer.phone_e164 == user.login_phone_number
    assert customer.address == supported_customer_address["address"]

    duplicate = admin_api.post(f"{CUSTOMERS}/from-user", {"user_id": user.pk}, format="json")
    assert duplicate.status_code == 409
    assert duplicate.json()["errors"] == [{"code": "csa_customer_user_exists"}]
    assert CSACustomer.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing_address", "csa_customer_shipping_address_unavailable"),
        ("unsupported_address", "csa_customer_shipping_address_unavailable"),
        ("hidden_address", "csa_customer_shipping_address_unavailable"),
        ("missing_phone", "csa_customer_phone_required"),
        ("missing_name", "csa_customer_name_required"),
        ("existing_phone", "csa_customer_phone_exists"),
    ],
)
def test_admin_customer_create_from_user_rejects_invalid_core_data(
    admin_api, supported_customer_address, case, code
):
    user = User.objects.create_user(
        "invalid-customer-source@example.com",
        name=" " if case == "missing_name" else "Core name",
        login_phone_number=None if case == "missing_phone" else "0912345678",
        **(
            {"status": AccountStatus.DELETION_REQUESTED, "is_active": False}
            if case == "hidden_address"
            else {}
        ),
    )
    if case != "missing_address":
        address = supported_customer_address.copy()
        if case == "unsupported_address":
            address["ward_code"] = "22045"
        ShippingAddress.objects.create(user=user, **address)
    if case == "existing_phone":
        create_complete_managed_customer(phone="0912345678")

    response = admin_api.post(f"{CUSTOMERS}/from-user", {"user_id": user.pk}, format="json")

    assert response.status_code == (409 if case == "existing_phone" else 400)
    assert response.json()["errors"] == [{"code": code}]
    assert not CSACustomer.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_admin_customer_create_from_user_validates_payload_and_target(admin_api):
    path = f"{CUSTOMERS}/from-user"
    missing = admin_api.post(path, {"user_id": uuid.uuid4()}, format="json")
    assert missing.status_code == 404
    assert missing.json()["errors"] == [{"code": "csa_customer_user_not_found"}]
    response = admin_api.post(
        path, {"user_id": uuid.uuid4(), "name": "Override", "address": "Override"}, format="json"
    )
    assert response.status_code == 400
    assert response["Cache-Control"] == "private, no-store"
    assert set(response.json()) == {"name", "address"}
    assert not CSACustomer.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["lookup", "from-user"])
def test_new_customer_actions_require_scope_and_group(action, active_user, access_token):
    path = f"{CUSTOMERS}/{action}"
    response = APIClient().post(path, {}, format="json")
    assert response.status_code == 401
    assert response["Cache-Control"] == "private, no-store"
    client = bearer_client(access_token)
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    assert client.post(path, {}, format="json").status_code == 403
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    active_user.groups.clear()
    assert client.post(path, {}, format="json").status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("from_user", [False, True])
def test_admin_customer_create_does_not_mask_unclassified_integrity_error(
    admin_api, supported_customer_address, monkeypatch, from_user
):
    if from_user:
        user = User.objects.create_user(
            "integrity-customer@example.com", name="Core name", login_phone_number="0912345678"
        )
        ShippingAddress.objects.create(user=user, **supported_customer_address)
        path, payload = f"{CUSTOMERS}/from-user", {"user_id": user.pk}
    else:
        path = CUSTOMERS
        payload = {"name": "Managed", "phone": "0912345678", **supported_customer_address}

    def reject_insert(**kwargs):
        raise IntegrityError("some other database constraint")

    monkeypatch.setattr(CSACustomer.objects, "create", reject_insert)
    with pytest.raises(IntegrityError, match="some other database constraint"):
        admin_api.post(path, payload, format="json")
    assert not CSACustomer.objects.exists()


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
    customer.note = "Gọi trước khi giao."
    customer.save(update_fields=["note"])
    ShippingArea.objects.create(
        province_code="66",
        ward_code="22045",
        shipping_fee_vnd=30_000,
        is_active=True,
    )
    ShippingAddress.objects.create(
        user=active_user,
        recipient_name="Người nhận mới",
        phone_number="0987654321",
        province_code="66",
        ward_code="22045",
        address="Địa chỉ giao hàng mới",
    )
    response = admin_api.post(
        f"/api/v1/admin/csa-customers/{customer.pk}/link-user",
        {"user_id": str(active_user.pk)},
        format="json",
    )
    assert response.status_code == 200
    assert response.json()["data"]["user"]["id"] == str(active_user.pk)
    customer.refresh_from_db()
    assert customer.name == "Managed"
    assert customer.phone_e164 == "+84901234567"
    assert customer.province_code == "66"
    assert customer.ward_code == "22045"
    assert customer.address == "Địa chỉ giao hàng mới"
    profile = admin_api.get(f"/api/v1/admin/csa-customers/{customer.pk}").json()["data"]
    assert profile["note"] == "Gọi trước khi giao."
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
        == 405
    )


@pytest.mark.django_db
def test_admin_link_rejects_changed_shipping_address(admin_api, supported_customer_address):
    customer = create_complete_managed_customer(phone="0912345678")
    customer.note = "Gọi trước khi giao."
    customer.save(update_fields=["note"])
    user = User.objects.create_user(
        "later-account@example.com", name="Core name", login_phone_number="0912345678"
    )
    shipping = ShippingAddress.objects.create(user=user, **supported_customer_address)
    preview = admin_api.get(f"/api/v1/admin/users/{user.pk}").json()["data"]["shipping_address"]
    shipping.address = "Địa chỉ vừa cập nhật"
    shipping.save(update_fields=["address", "updated_at"])
    path = f"{CUSTOMERS}/{customer.pk}/link-user"
    payload = {"user_id": user.pk, "expected_shipping_address_updated_at": preview["updated_at"]}

    stale = admin_api.post(path, payload, format="json")
    assert stale.status_code == 409
    assert stale.json()["errors"] == [{"code": "csa_customer_shipping_address_changed"}]
    customer.refresh_from_db()
    assert customer.user_id is None and customer.address == supported_customer_address["address"]
    assert not AuditLog.objects.filter(action="csa_customer.link_user").exists()

    preview = admin_api.get(f"/api/v1/admin/users/{user.pk}").json()["data"]["shipping_address"]
    payload["expected_shipping_address_updated_at"] = preview["updated_at"]
    response = admin_api.post(path, payload, format="json")
    assert response.status_code == 200
    assert response.json()["data"]["id"] == str(customer.pk)
    customer.refresh_from_db()
    assert customer.user_id == user.pk and customer.address == shipping.address
    assert customer.name == "Managed" and customer.phone_e164 == "+84912345678"
    assert customer.note == "Gọi trước khi giao."
    assert AuditLog.objects.filter(action="csa_customer.link_user").count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize("include_version", [False, True])
def test_admin_link_rejects_address_hidden_after_preview(
    admin_api, supported_customer_address, include_version
):
    customer = create_complete_managed_customer(phone="0912345678")
    user = User.objects.create_user(
        "deleting-customer@example.com",
        name="Core name",
        login_phone_number="0912345678",
        status=AccountStatus.ACTIVE,
    )
    shipping = ShippingAddress.objects.create(
        user=user, **{**supported_customer_address, "address": "Địa chỉ tài khoản"}
    )
    detail_url = f"/api/v1/admin/users/{user.pk}"
    preview = admin_api.get(detail_url).json()["data"]["shipping_address"]
    transition_account(user, AccountStatus.DELETION_REQUESTED)
    shipping.refresh_from_db()
    assert shipping.updated_at.isoformat() == preview["updated_at"]
    assert admin_api.get(detail_url).json()["data"]["shipping_address"] is None

    payload = {"user_id": user.pk}
    if include_version:
        payload["expected_shipping_address_updated_at"] = preview["updated_at"]
    response = admin_api.post(f"{CUSTOMERS}/{customer.pk}/link-user", payload, format="json")

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_customer_shipping_address_unavailable"}]
    customer.refresh_from_db()
    assert customer.user_id is None and customer.address == supported_customer_address["address"]
    assert not AuditLog.objects.filter(action="csa_customer.link_user").exists()


@pytest.mark.parametrize("has_shipping_address", [False, True])
@pytest.mark.django_db
def test_admin_cannot_link_customer_without_supported_shipping_address(
    admin_api, active_user, has_shipping_address
):
    customer = create_complete_managed_customer()
    if has_shipping_address:
        ShippingAddress.objects.create(
            user=active_user,
            recipient_name="Người nhận mới",
            phone_number="0987654321",
            province_code="66",
            ward_code="22045",
            address="Địa chỉ chưa được hỗ trợ",
        )

    response = admin_api.post(
        f"/api/v1/admin/csa-customers/{customer.pk}/link-user",
        {"user_id": str(active_user.pk)},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"] == [{"code": "csa_customer_shipping_address_unavailable"}]
    customer.refresh_from_db()
    assert customer.user_id is None
    assert customer.province_code == "66"
    assert customer.ward_code == "22015"
    assert customer.address == "Số 12, ngõ 5"


@pytest.mark.django_db
def test_link_rejects_user_owned_by_another_customer(active_user):
    create_complete_customer_for_user(active_user)
    managed = create_complete_managed_customer(phone="0907777777")
    actor = create_active_user("link-actor@example.com", phone=None)
    with pytest.raises(DuplicateCSACustomerUser):
        link_csa_customer_to_user(customer=managed, user=active_user, actor=actor)


@pytest.mark.django_db
def test_managed_customer_assignment_and_self_ownership(
    admin_api, active_user, access_token, manual_payment_settings
):
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
    membership_id = response.json()["data"]["id"]
    for value in ("0901234567", "090", "234567", "Managed", "FB-MANAGED"):
        result = admin_api.get("/api/v1/admin/memberships", {"search": value})
        assert result.status_code == 200
        assert [row["id"] for row in result.json()["data"]] == [membership_id]

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
    active_user, access_token, admin_api, manual_payment_settings
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
@pytest.mark.parametrize("from_user", [False, True])
def test_concurrent_customer_creation_is_unique(
    admin_api, access_token, supported_customer_address, from_user
):
    phone = "0912345678"
    if from_user:
        user = User.objects.create_user(
            "concurrent-customer@example.com", name="Core name", login_phone_number=phone
        )
        ShippingAddress.objects.create(user=user, **supported_customer_address)
        path, payload = f"{CUSTOMERS}/from-user", {"user_id": user.pk}
        error_code = "csa_customer_user_exists"
    else:
        path = CUSTOMERS
        payload = {"name": "Managed", "phone": phone, **supported_customer_address}
        error_code = "csa_customer_phone_exists"
    barrier = threading.Barrier(2, timeout=5)

    def create_once():
        close_old_connections()
        try:
            barrier.wait()
            return bearer_client(access_token).post(path, payload, format="json")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        requests = [executor.submit(create_once) for _ in range(2)]
        responses = sorted((request.result() for request in requests), key=lambda r: r.status_code)
    created, conflict = responses
    assert created.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["errors"] == [{"code": error_code}]
    assert CSACustomer.objects.filter(phone_e164="+84912345678").count() == 1
    if from_user:
        assert CSACustomer.objects.filter(user=user).count() == 1


@pytest.mark.django_db(transaction=True)
def test_customer_migration_backfills_only_membership_users_without_losing_history():
    migrate_from = ("memberships", "0009_revoke_membership_fields")
    migrate_to = ("memberships", "0011_csa_customer_finalize")
    members_target = ("members", "0002_member_profile_administrative_codes")
    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
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
        MigrationExecutor(connection).migrate(latest)
