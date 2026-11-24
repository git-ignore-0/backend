from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, local
from uuid import UUID

import pytest
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from oauth2_provider.models import AccessToken
from rest_framework.test import APIClient

from commerce.models import ShippingAddress
from commerce.store_orders import StoreOrderError, _address_for_checkout
from identity.models import User
from members.models import MemberProfile

PROVINCE_CODE = "66"
WARD_CODE = "22015"
SECOND_WARD_CODE = "22045"
THIRD_WARD_CODE = "22051"


@pytest.fixture
def admin_api(active_user, access_token):
    group, _ = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


@pytest.fixture
def member_api(access_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def settings_payload(**overrides):
    payload = {"is_active": False, "default_shipping_fee_vnd": 30_000, "overrides": []}
    payload.update(overrides)
    return payload


def address_payload(**overrides):
    payload = {
        "recipient_name": "Nguyễn Văn An",
        "phone_number": "0901234567",
        "province_code": PROVINCE_CODE,
        "ward_code": WARD_CODE,
        "address": "Số 12, đường Nguyễn Huệ",
    }
    payload.update(overrides)
    return payload


def admin_address_payload(expected_updated_at=None, **overrides):
    return {
        "province_code": PROVINCE_CODE,
        "ward_code": WARD_CODE,
        "address": "Số 12, đường Nguyễn Huệ",
        "expected_updated_at": expected_updated_at,
        **overrides,
    }


@pytest.fixture
def customer(db):
    user = User.objects.create_user(
        email="customer@example.com",
        name="Customer",
        login_phone_number="0912345678",
        status="active",
    )
    MemberProfile.objects.create(user=user, phone_number="0901234567")
    return user


@pytest.mark.django_db
@pytest.mark.parametrize("first_writer", ["admin", "customer"])
def test_admin_and_customer_update_the_same_shipping_address(
    admin_api, customer, oauth_application, default_supported_shipping_area, first_writer
):
    detail_url = f"/api/v1/admin/users/{customer.pk}"
    admin_url = f"{detail_url}/shipping-address"
    self_url = "/api/v1/shipping-address"
    token = AccessToken.objects.create(
        user=customer,
        application=oauth_application,
        token="customer-test-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )
    customer_api = APIClient()
    customer_api.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")

    empty = admin_api.get(detail_url).json()["data"]
    assert empty["can_edit_shipping_address"] is True and empty["shipping_address"] is None
    assert "shipping_address" not in admin_api.get("/api/v1/admin/users").json()["data"][0]

    if first_writer == "admin":
        added = admin_api.put(admin_url, admin_address_payload(), format="json")
        expected_name, expected_phone = "Customer", "+84912345678"
    else:
        body = admin_address_payload()
        del body["expected_updated_at"]
        added = customer_api.put(self_url, body, format="json")
        expected_name, expected_phone = "Customer", "+84912345678"
    assert added.status_code == 200
    assert added["Cache-Control"] == "private, no-store"
    first = added.json()["data"]
    assert first["recipient_name"] == expected_name
    assert first["phone_number"] == expected_phone
    assert first["shipping_fee_vnd"] == "30000"
    self_read = customer_api.get(self_url).json()["data"]
    version = self_read["shipping_address_version"]
    assert len(version) == 64
    assert self_read == {**first, "shipping_address_version": version}
    admin_address = admin_api.get(detail_url).json()["data"]["shipping_address"]
    assert "shipping_address_version" not in admin_address
    assert self_read == {**admin_address, "shipping_address_version": version}

    edited = admin_api.put(
        admin_url,
        admin_address_payload(first["updated_at"], address="Địa chỉ từ CMS"),
        format="json",
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["id"] == first["id"]
    assert customer_api.get(self_url).json()["data"]["address"] == "Địa chỉ từ CMS"

    self_edit = customer_api.put(
        self_url,
        address_payload(address="Địa chỉ từ tài khoản", phone_number="0987654321"),
        format="json",
    )
    assert self_edit.status_code == 200
    assert self_edit.json()["data"]["phone_number"] == customer.login_phone_number
    assert self_edit.json()["data"]["recipient_name"] == customer.name
    customer.refresh_from_db()
    assert customer.login_phone_number == "+84912345678"
    detail = admin_api.get(detail_url)
    assert detail["Cache-Control"] == "private, no-store"
    self_address = self_edit.json()["data"]
    assert self_address["shipping_address_version"] != version
    assert self_address == {
        **detail.json()["data"]["shipping_address"],
        "shipping_address_version": self_address["shipping_address_version"],
    }
    assert detail.json()["data"]["shipping_address"]["address"] == "Địa chỉ từ tài khoản"
    assert detail.json()["data"]["address"] == ""
    assert ShippingAddress.objects.filter(user=customer).count() == 1

    stale = admin_api.put(
        admin_url,
        admin_address_payload(edited.json()["data"]["updated_at"], address="Địa chỉ cũ từ CMS"),
        format="json",
    )
    assert stale.status_code == 409
    assert stale.json() == {"errors": [{"code": "shipping_address_changed"}]}
    assert customer_api.get(self_url).json()["data"] == self_edit.json()["data"]

    saved = admin_api.put(
        admin_url,
        admin_address_payload(self_edit.json()["data"]["updated_at"], address="Đã đổi địa chỉ"),
        format="json",
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["recipient_name"] == "Customer"
    assert saved.json()["data"]["phone_number"] == "+84912345678"


@pytest.mark.django_db
def test_self_address_ignores_legacy_contacts_when_login_phone_is_missing(
    member_api, active_user, default_supported_shipping_area
):
    MemberProfile.objects.create(user=active_user, phone_number="0987654321")
    address = ShippingAddress.objects.create(user=active_user, **address_payload())
    url = "/api/v1/shipping-address"

    for response in [
        member_api.get(url),
        member_api.put(url, address_payload(address="Địa chỉ mới"), format="json"),
    ]:
        assert response.status_code == 200, response.json()
        payload = response.json()["data"]
        assert payload["recipient_name"] == active_user.name
        assert payload["phone_number"] == ""

    address.refresh_from_db()
    assert address.address == "Địa chỉ mới"
    assert address.recipient_name == "Nguyễn Văn An"
    assert address.phone_number == "0901234567"


@pytest.mark.django_db
def test_account_address_uses_login_phone_over_old_recipient_phone(member_api, active_user):
    active_user.login_phone_number = "0912345678"
    active_user.save(update_fields=["login_phone_number"])
    ShippingAddress.objects.create(user=active_user, **address_payload(phone_number="0987654321"))

    response = member_api.get("/api/v1/shipping-address")
    assert response.status_code == 200
    assert response.json()["data"]["phone_number"] == "+84912345678"
    assert response.json()["data"]["recipient_name"] == active_user.name

    rejected = member_api.put(
        "/api/v1/shipping-address", address_payload(login_phone_number="0987654321"), format="json"
    )
    assert rejected.status_code == 400
    active_user.refresh_from_db()
    assert active_user.login_phone_number == "+84912345678"


@pytest.mark.django_db
@pytest.mark.parametrize("contact_phone", ["", "123", "0987654321"])
def test_admin_saves_location_for_accounts_without_a_valid_phone(
    admin_api, customer, oauth_application, default_supported_shipping_area, contact_phone
):
    User.objects.filter(pk=customer.pk).update(login_phone_number=None)
    customer.refresh_from_db()
    MemberProfile.objects.filter(user=customer).update(phone_number=contact_phone)
    response = admin_api.put(
        f"/api/v1/admin/users/{customer.pk}/shipping-address",
        admin_address_payload(),
        format="json",
    )

    assert response.status_code == 200
    saved = response.json()["data"]
    assert saved["phone_number"] == "—"
    assert ShippingAddress.objects.get(user=customer).phone_number == ""
    assert saved["address"] == admin_address_payload()["address"]
    assert (
        admin_api.get(f"/api/v1/admin/users/{customer.pk}").json()["data"]["shipping_address"]
        == saved
    )

    token = AccessToken.objects.create(
        user=customer,
        application=oauth_application,
        token="customer-location-only-test-token",
        expires=timezone.now() + timezone.timedelta(hours=1),
        scope="profile email",
    )
    customer_api = APIClient()
    customer_api.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    self_url = "/api/v1/shipping-address"
    self_read = customer_api.get(self_url).json()["data"]
    version = self_read["shipping_address_version"]
    assert len(version) == 64
    assert self_read == {**saved, "phone_number": "", "shipping_address_version": version}
    with pytest.raises(StoreOrderError) as error:
        _address_for_checkout(customer, None, lock=False)
    assert error.value.code == "store_shipping_address_required"

    customer.login_phone_number = "0912345678"
    customer.save(update_fields=["login_phone_number"])
    saved_contact = customer_api.get(self_url).json()["data"]
    assert saved_contact["address"] == saved["address"]
    assert saved_contact["phone_number"] == "+84912345678"
    assert saved_contact["shipping_address_version"] != version
    assert (
        _address_for_checkout(customer, None, lock=False)[0]["phone_e164_snapshot"]
        == "+84912345678"
    )

    changed = customer_api.put(
        self_url,
        address_payload(address="Địa chỉ người dùng sửa"),
        format="json",
    )
    assert changed.status_code == 200
    changed_address = changed.json()["data"]
    assert changed_address["id"] == saved["id"]
    assert changed_address["shipping_address_version"] != saved_contact["shipping_address_version"]
    admin_address = admin_api.get(f"/api/v1/admin/users/{customer.pk}").json()["data"][
        "shipping_address"
    ]
    assert changed_address == {
        **admin_address,
        "shipping_address_version": changed_address["shipping_address_version"],
    }


@pytest.mark.django_db
def test_admin_shipping_address_requires_version_and_preserves_identical_retries(
    admin_api, customer, default_supported_shipping_area
):
    url = f"/api/v1/admin/users/{customer.pk}/shipping-address"
    missing_version = admin_address_payload()
    del missing_version["expected_updated_at"]
    missing_version = admin_api.put(url, missing_version, format="json")
    assert missing_version.status_code == 400
    assert "expected_updated_at" in missing_version.json()

    added = admin_api.put(url, admin_address_payload(), format="json")
    assert added.status_code == 200
    saved = added.json()["data"]
    for expected in [None, saved["updated_at"]]:
        repeated = admin_api.put(url, admin_address_payload(expected), format="json")
        assert repeated.status_code == 200
        assert repeated.json()["data"] == saved

    stale_add = admin_api.put(url, admin_address_payload(address="Địa chỉ khác"), format="json")
    assert stale_add.status_code == 409
    assert stale_add.json() == {"errors": [{"code": "shipping_address_changed"}]}


@pytest.mark.django_db(transaction=True)
def test_concurrent_sqlite_admin_shipping_writes_retry_transaction(
    admin_api, access_token, customer, default_supported_shipping_area, monkeypatch
):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite read-to-write upgrade contention")
    barrier = Barrier(2, timeout=10)
    attempt = local()
    save_address = ShippingAddress.save

    def synchronize_first_write(instance, *args, **kwargs):
        if not getattr(attempt, "synchronized", False):
            attempt.synchronized = True
            barrier.wait()
        return save_address(instance, *args, **kwargs)

    monkeypatch.setattr(ShippingAddress, "save", synchronize_first_write)

    def save_from_cms(index):
        close_old_connections()
        try:
            client = APIClient(raise_request_exception=False)
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
            return client.put(
                f"/api/v1/admin/users/{customer.pk}/shipping-address",
                admin_address_payload(address=f"Địa chỉ {index}"),
                format="json",
            ).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(save_from_cms, range(2))) == [200, 409]
    assert ShippingAddress.objects.filter(user=customer).count() == 1


@pytest.mark.django_db
def test_mysql_shipping_write_locks_actor_shared_before_later_target(
    admin_api, active_user, default_supported_shipping_area
):
    if connection.vendor != "mysql":
        pytest.skip("MySQL shared lock syntax")
    customer = User.objects.create_user(
        id=UUID(int=active_user.pk.int + 1), email="lock-review@example.com", status="active"
    )
    MemberProfile.objects.create(user=customer, phone_number="0901234567")
    with CaptureQueriesContext(connection) as queries:
        response = admin_api.put(
            f"/api/v1/admin/users/{customer.pk}/shipping-address",
            admin_address_payload(),
            format="json",
        )
    assert response.status_code == 200
    locks = [
        row["sql"]
        for row in queries
        if "identity_user" in row["sql"]
        and ("LOCK IN SHARE MODE" in row["sql"] or "FOR UPDATE" in row["sql"])
    ]
    assert len(locks) == 2
    assert active_user.pk.hex in locks[0].lower().replace("-", "")
    assert "LOCK IN SHARE MODE" in locks[0]
    assert customer.pk.hex in locks[1].lower().replace("-", "")
    assert "FOR UPDATE" in locks[1]


@pytest.mark.django_db
@pytest.mark.parametrize("missing", ["scope", "group"])
def test_admin_shipping_address_requires_group_and_scope(
    admin_api, active_user, access_token, customer, missing
):
    if missing == "scope":
        access_token.scope = "profile email"
        access_token.save(update_fields=["scope"])
    else:
        active_user.groups.clear()
    response = admin_api.put(
        f"/api/v1/admin/users/{customer.pk}/shipping-address",
        admin_address_payload(),
        format="json",
    )
    assert response.status_code == 403
    assert not ShippingAddress.objects.filter(user=customer).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "protected", ["website_admin", "staff", "superuser", "deletion_requested", "anonymized"]
)
def test_admin_shipping_address_rejects_protected_targets(admin_api, customer, protected):
    saved = ShippingAddress.objects.create(
        user=customer, **address_payload(phone_number="+84901234567")
    )
    if protected == "website_admin":
        customer.groups.add(Group.objects.get(name="website_admin"))
    elif protected in {"staff", "superuser"}:
        User.objects.filter(pk=customer.pk).update(**{f"is_{protected}": True})
    else:
        User.objects.filter(pk=customer.pk).update(status=protected, is_active=False)

    url = f"/api/v1/admin/users/{customer.pk}"
    response = admin_api.put(
        f"{url}/shipping-address", admin_address_payload(address="Địa chỉ khác"), format="json"
    )
    assert response.status_code == 409
    assert response.json() == {"errors": [{"code": "account_target_protected"}]}
    saved.refresh_from_db()
    assert saved.address == "Số 12, đường Nguyễn Huệ"
    detail = admin_api.get(url).json()["data"]
    assert detail["can_edit_shipping_address"] is False
    if protected in {"deletion_requested", "anonymized"}:
        assert detail["shipping_address"] is None
    else:
        assert detail["shipping_address"]["id"] == str(saved.pk)
        assert detail["shipping_address"]["address"] == saved.address


@pytest.mark.django_db
def test_admin_shipping_address_returns_not_found_for_unknown_uuid(admin_api):
    response = admin_api.put(
        "/api/v1/admin/users/00000000-0000-0000-0000-000000000001/shipping-address",
        admin_address_payload(),
        format="json",
    )
    assert response.status_code == 404
    assert "detail" in response.json()


@pytest.mark.django_db
def test_legacy_cms_can_save_location_without_replacing_account_contact(
    admin_api, customer, default_supported_shipping_area
):
    url = f"/api/v1/admin/users/{customer.pk}/shipping-address"
    legacy_contact = {"recipient_name": "Tên từ CMS cũ", "phone_number": "0987654321"}

    created = admin_api.put(url, admin_address_payload(**legacy_contact), format="json")
    assert created.status_code == 200, created.json()
    first = created.json()["data"]
    assert first["recipient_name"] == customer.name
    assert first["phone_number"] == customer.login_phone_number

    updated = admin_api.put(
        url,
        admin_address_payload(first["updated_at"], address="Địa chỉ mới", **legacy_contact),
        format="json",
    )
    assert updated.status_code == 200, updated.json()
    assert updated.json()["data"]["id"] == first["id"]
    saved = ShippingAddress.objects.get(user=customer)
    assert saved.address == "Địa chỉ mới"
    assert saved.recipient_name == customer.name
    assert saved.phone_number == customer.login_phone_number
    customer.refresh_from_db()
    assert customer.login_phone_number == "+84912345678"


@pytest.mark.django_db
def test_legacy_cms_can_read_its_saved_location_when_the_account_has_no_contact(
    admin_api, customer, default_supported_shipping_area
):
    User.objects.filter(pk=customer.pk).update(login_phone_number=None, name="")
    MemberProfile.objects.filter(user=customer).update(phone_number="")
    user_url = f"/api/v1/admin/users/{customer.pk}"
    payload = admin_address_payload(recipient_name="Old CMS name", phone_number="0987654321")
    saved = admin_api.put(f"{user_url}/shipping-address", payload, format="json")

    assert saved.status_code == 200, saved.json()
    address = saved.json()["data"]
    assert address["recipient_name"] == address["phone_number"] == "—"
    assert admin_api.get(user_url).json()["data"]["shipping_address"] == address
    row = ShippingAddress.objects.get(user=customer)
    assert row.address == payload["address"]
    assert row.recipient_name == row.phone_number == ""


@pytest.mark.django_db
def test_admin_shipping_address_validates_updates_and_preserves_the_saved_address(
    admin_api, customer, default_supported_shipping_area
):
    url = f"/api/v1/admin/users/{customer.pk}/shipping-address"
    added = admin_api.put(url, admin_address_payload(), format="json")
    assert added.status_code == 200
    first = added.json()["data"]

    for field, invalid in [
        ("login_phone_number", "+84987654321"),
        ("ward_code", "00004"),
        ("user_id", "other"),
    ]:
        response = admin_api.put(
            url,
            admin_address_payload(
                first["updated_at"], **{field: invalid, "address": "Địa chỉ khác"}
            ),
            format="json",
        )
        assert response.status_code == 400
        assert field in response.json()

    default_supported_shipping_area.is_active = False
    default_supported_shipping_area.save(update_fields=["is_active"])
    unsupported = admin_api.put(
        url, admin_address_payload(first["updated_at"], address="Địa chỉ khác"), format="json"
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["errors"][0]["code"] == "shipping_area_unsupported"
    detail = admin_api.get(f"/api/v1/admin/users/{customer.pk}").json()["data"]["shipping_address"]
    assert detail == {**first, "is_supported": False, "shipping_fee_vnd": None}


@pytest.mark.django_db
def test_shipping_tests_start_without_an_implicit_supported_area():
    from commerce.models import ShippingArea

    assert not ShippingArea.objects.exists()


@pytest.mark.django_db
def test_shipping_admin_requires_website_admin(client, access_token):
    endpoint = f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}"
    anonymous = client.get(endpoint)
    assert anonymous.status_code in {401, 403}

    ordinary = APIClient()
    ordinary.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    denied = ordinary.put(endpoint, settings_payload(), format="json")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_admin_shipping_settings_rejects_foreign_and_duplicate_wards(admin_api):
    endpoint = f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}"
    foreign = admin_api.put(
        endpoint,
        settings_payload(
            overrides=[{"ward_code": "26734", "shipping_fee_vnd": 30_000, "is_active": True}]
        ),
        format="json",
    )
    assert foreign.status_code == 400
    assert "overrides" in foreign.json()

    duplicate = admin_api.put(
        endpoint,
        settings_payload(
            overrides=[
                {"ward_code": WARD_CODE, "shipping_fee_vnd": 30_000, "is_active": True},
                {"ward_code": WARD_CODE, "shipping_fee_vnd": None, "is_active": False},
            ]
        ),
        format="json",
    )
    assert duplicate.status_code == 400
    assert "overrides" in duplicate.json()


@pytest.mark.django_db
def test_public_shipping_areas_only_returns_active_wards_and_quotes_server_fee(admin_api, client):
    configured = admin_api.put(
        f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}",
        settings_payload(
            overrides=[
                {"ward_code": WARD_CODE, "shipping_fee_vnd": 30_000, "is_active": True},
                {
                    "ward_code": SECOND_WARD_CODE,
                    "shipping_fee_vnd": None,
                    "is_active": False,
                },
            ]
        ),
        format="json",
    )
    assert configured.status_code == 200, configured.json()

    listed = client.get("/api/v1/public/shipping-areas?locale=vi")
    assert listed.status_code == 200
    assert listed.json()["data"] == [
        {
            "province_code": PROVINCE_CODE,
            "province_name": "Tỉnh Đắk Lắk",
            "wards": [
                {
                    "ward_code": WARD_CODE,
                    "ward_name": "Phường Tuy Hoà",
                    "shipping_fee_vnd": "30000",
                }
            ],
        }
    ]

    supported = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": WARD_CODE},
        format="json",
    )
    assert supported.status_code == 200
    assert supported.json()["data"] == {
        "supported": True,
        "shipping_fee_vnd": "30000",
    }

    unsupported = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": SECOND_WARD_CODE},
        format="json",
    )
    assert unsupported.status_code == 200
    assert unsupported.json()["data"] == {
        "supported": False,
        "shipping_fee_vnd": None,
    }


@pytest.mark.django_db
def test_user_has_one_shipping_address_and_backend_validates_supported_area(
    admin_api, member_api, active_user
):
    active_user.login_phone_number = "0901234567"
    active_user.save(update_fields=["login_phone_number"])
    empty = member_api.get("/api/v1/shipping-address")
    assert empty.status_code == 200
    assert empty.json()["data"] is None

    unsupported = member_api.put("/api/v1/shipping-address", address_payload(), format="json")
    assert unsupported.status_code == 400
    assert unsupported.json()["errors"][0]["code"] == "shipping_area_unsupported"

    configured = admin_api.put(
        f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}",
        settings_payload(
            overrides=[{"ward_code": WARD_CODE, "shipping_fee_vnd": 30_000, "is_active": True}]
        ),
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    created = member_api.put("/api/v1/shipping-address", address_payload(), format="json")
    assert created.status_code == 200, created.json()
    first = created.json()["data"]
    assert first["recipient_name"] == active_user.name
    assert first["phone_number"] == "+84901234567"
    assert first["is_supported"] is True
    assert first["shipping_fee_vnd"] == "30000"

    updated = member_api.put(
        "/api/v1/shipping-address",
        address_payload(address="Địa chỉ mới"),
        format="json",
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["id"] == first["id"]
    assert updated.json()["data"]["address"] == "Địa chỉ mới"
    repeated = member_api.put(
        "/api/v1/shipping-address",
        address_payload(address="Địa chỉ mới"),
        format="json",
    )
    assert repeated.status_code == 200
    assert repeated.json()["data"] == updated.json()["data"]


@pytest.mark.django_db
def test_saved_address_remains_readable_after_area_is_disabled(admin_api, member_api):
    configured = admin_api.put(
        f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}",
        settings_payload(
            overrides=[{"ward_code": WARD_CODE, "shipping_fee_vnd": 30_000, "is_active": True}]
        ),
        format="json",
    )
    assert configured.status_code == 200, configured.json()
    member_api.put("/api/v1/shipping-address", address_payload(), format="json")

    disabled = admin_api.put(
        f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}",
        settings_payload(),
        format="json",
    )
    assert disabled.status_code == 200, disabled.json()
    fetched = member_api.get("/api/v1/shipping-address")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["ward_code"] == WARD_CODE
    assert fetched.json()["data"]["is_supported"] is False
    assert fetched.json()["data"]["shipping_fee_vnd"] is None


@pytest.mark.django_db
def test_admin_shipping_tree_persists_province_default_and_ward_overrides(admin_api, client):
    saved = admin_api.put(
        f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}",
        {
            "is_active": True,
            "default_shipping_fee_vnd": 30_000,
            "overrides": [
                {
                    "ward_code": WARD_CODE,
                    "is_active": True,
                    "shipping_fee_vnd": 20_000,
                },
                {
                    "ward_code": SECOND_WARD_CODE,
                    "is_active": False,
                    "shipping_fee_vnd": None,
                },
            ],
        },
        format="json",
    )

    assert saved.status_code == 200, saved.json()
    assert saved.json()["data"]["default_shipping_fee_vnd"] == "30000"
    assert [item["ward_code"] for item in saved.json()["data"]["overrides"]] == [
        WARD_CODE,
        SECOND_WARD_CODE,
    ]

    inherited = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": THIRD_WARD_CODE},
        format="json",
    )
    overridden = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": WARD_CODE},
        format="json",
    )
    excluded = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": SECOND_WARD_CODE},
        format="json",
    )

    assert inherited.json()["data"] == {
        "supported": True,
        "shipping_fee_vnd": "30000",
    }
    assert overridden.json()["data"] == {
        "supported": True,
        "shipping_fee_vnd": "20000",
    }
    assert excluded.json()["data"] == {
        "supported": False,
        "shipping_fee_vnd": None,
    }


@pytest.mark.django_db
def test_inactive_province_keeps_explicit_inherited_ward_supported(admin_api, client):
    saved = admin_api.put(
        f"/api/v1/admin/shipping-settings/{PROVINCE_CODE}",
        {
            "is_active": False,
            "default_shipping_fee_vnd": 25_000,
            "overrides": [
                {
                    "ward_code": WARD_CODE,
                    "is_active": True,
                    "shipping_fee_vnd": None,
                }
            ],
        },
        format="json",
    )
    assert saved.status_code == 200, saved.json()

    supported = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": WARD_CODE},
        format="json",
    )
    unsupported = client.post(
        "/api/v1/public/shipping-quotes",
        {"province_code": PROVINCE_CODE, "ward_code": SECOND_WARD_CODE},
        format="json",
    )

    assert supported.json()["data"] == {
        "supported": True,
        "shipping_fee_vnd": "25000",
    }
    assert unsupported.json()["data"] == {
        "supported": False,
        "shipping_fee_vnd": None,
    }
