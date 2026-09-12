import pytest
from django.contrib.auth.models import Group
from django.core.cache import cache
from rest_framework.test import APIClient

from memberships.models import MembershipPaymentSettings

SETTINGS_URL = "/api/v1/admin/membership-payment-settings"
BANKS_URL = "/api/v1/admin/membership-banks"
TEST_BANK = {
    "bin": "970436",
    "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
    "short_name": "Vietcombank",
    "code": "VCB",
}


def bearer_client(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.token}")
    return client


@pytest.fixture(autouse=True)
def bank_directory(monkeypatch):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])
    monkeypatch.setattr("memberships.views.get_transfer_banks", lambda: [TEST_BANK])
    yield
    cache.clear()


@pytest.fixture
def admin_api(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    return bearer_client(access_token)


def valid_payload(**overrides):
    payload = {
        "bank_bin": TEST_BANK["bin"],
        "bank_name": TEST_BANK["name"],
        "account_number": "123456789",
        "account_name": "NATURAL FARMING VIETNAM",
        "direct_transfer_enabled": True,
    }
    payload.update(overrides)
    return payload


@pytest.mark.django_db
def test_bank_settings_and_directory_require_website_admin(active_user, access_token):
    anonymous = APIClient()
    non_admin = bearer_client(access_token)

    assert anonymous.get(SETTINGS_URL).status_code == 401
    assert anonymous.get(BANKS_URL).status_code == 401
    assert non_admin.get(SETTINGS_URL).status_code == 403
    assert non_admin.patch(SETTINGS_URL, valid_payload(), format="json").status_code == 403


@pytest.mark.django_db
def test_get_empty_settings_and_supported_banks(admin_api):
    settings_response = admin_api.get(SETTINGS_URL)
    banks_response = admin_api.get(BANKS_URL)

    assert settings_response.status_code == 200
    assert settings_response.json()["data"] == {
        "key": "default",
        "direct_transfer_enabled": False,
        "bank_bin": "",
        "bank_name": "",
        "account_number": "",
        "account_name": "",
        "version": 0,
        "updated_at": None,
    }
    assert banks_response.status_code == 200
    assert banks_response.json()["data"] == [TEST_BANK]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"bank_bin": "123"}, "bank_bin"),
        ({"bank_name": "Wrong bank"}, "bank_name"),
        ({"account_number": "ABC"}, "account_number"),
        ({"account_name": ""}, "account_name"),
    ],
)
def test_patch_rejects_invalid_settings_with_stable_error(admin_api, patch, field):
    response = admin_api.patch(SETTINGS_URL, valid_payload(**patch), format="json")

    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["code"] == "invalid_membership_payment_settings"
    assert field in error["fields"]
    assert not MembershipPaymentSettings.objects.exists()


@pytest.mark.django_db
def test_patch_creates_singleton_and_increments_version(admin_api, active_user):
    created = admin_api.patch(SETTINGS_URL, valid_payload(), format="json")
    updated = admin_api.patch(
        SETTINGS_URL,
        valid_payload(account_number="987654321", direct_transfer_enabled=False),
        format="json",
    )

    assert created.status_code == 200
    assert created.json()["data"]["version"] == 1
    assert updated.status_code == 200
    assert updated.json()["data"]["version"] == 2
    assert updated.json()["data"]["account_number"] == "987654321"
    assert updated.json()["data"]["direct_transfer_enabled"] is False
    assert MembershipPaymentSettings.objects.count() == 1
    item = MembershipPaymentSettings.objects.get(pk="default")
    assert item.updated_by == active_user


@pytest.mark.django_db
def test_bank_directory_failure_uses_stable_error(admin_api, monkeypatch):
    from memberships.banks import BankDirectoryUnavailable

    def unavailable():
        raise BankDirectoryUnavailable("offline")

    monkeypatch.setattr("memberships.views.get_transfer_banks", unavailable)
    response = admin_api.get(BANKS_URL)

    assert response.status_code == 503
    assert response.json() == {"errors": [{"code": "membership_bank_directory_unavailable"}]}
