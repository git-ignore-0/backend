import pytest
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

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
def test_user_has_one_shipping_address_and_backend_validates_supported_area(admin_api, member_api):
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
    assert first["recipient_name"] == "Nguyễn Văn An"
    assert first["phone_number"] == "+84901234567"
    assert first["is_supported"] is True
    assert first["shipping_fee_vnd"] == "30000"

    updated = member_api.put(
        "/api/v1/shipping-address",
        address_payload(recipient_name="Nguyễn Văn Bình"),
        format="json",
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["id"] == first["id"]
    assert updated.json()["data"]["recipient_name"] == "Nguyễn Văn Bình"


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
