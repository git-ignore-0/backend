# ruff: noqa: E501
# These assertions intentionally retain the document-mandated clauses verbatim.
import threading
import uuid

import pytest
from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.db import close_old_connections, connection
from rest_framework.test import APIClient

from identity.models import User
from members.models import MemberProfile
from memberships.banks import BankDirectoryUnavailable
from memberships.models import (
    CSAContract,
    CSACustomer,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    Membership,
    MembershipPackage,
    MembershipPackagePriceOption,
    QuotaPolicy,
)
from memberships.purchases import (
    PurchaseRequestConflict,
    create_public_csa_purchase_request,
    update_membership_payment_settings,
)
from memberships.terms import (
    InvalidCSATerms,
    canonical_csa_terms_hash,
    get_current_csa_terms,
    validate_csa_terms_snapshot,
)

CREATE_URL = "/api/v1/public/csa-purchase-requests"
TEST_BANK = {
    "bin": "970436",
    "name": "Ngân hàng TMCP Ngoại Thương Việt Nam",
    "short_name": "Vietcombank",
    "code": "VCB",
}


@pytest.fixture(autouse=True)
def public_purchase_test_isolation(monkeypatch):
    cache.clear()
    monkeypatch.setattr("memberships.banks.get_transfer_banks", lambda: [TEST_BANK])
    yield
    assert CSACustomer.objects.count() == 0
    assert Membership.objects.count() == 0
    assert CSAContract.objects.count() == 0
    cache.clear()


@pytest.fixture
def package_and_option():
    package = MembershipPackage.objects.create(
        name="Gói CSA Online",
        description="Rau theo mùa",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=400_000,
    )
    return package, option


@pytest.fixture
def payment_settings():
    return update_membership_payment_settings(
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="123456789",
        account_name="NATURAL FARMING VIETNAM",
        direct_transfer_enabled=True,
    )


@pytest.fixture
def complete_member_profile(active_user):
    return MemberProfile.objects.create(
        user=active_user,
        phone_number="0901234567",
        province_code="66",
        ward_code="22015",
        address="Địa chỉ từ Auth Account",
    )


@pytest.fixture
def valid_payload(package_and_option):
    package, option = package_and_option
    return {
        "package_id": str(package.pk),
        "price_option_id": str(option.pk),
        "name": "Nguyễn Văn An",
        "phone": "0912345678",
        "province_code": "66",
        "ward_code": "22015",
        "address": "Số 12, ngõ 5",
        "terms_accepted": True,
    }


def bearer_client(access_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def confirm_url(request_id):
    return f"/api/v1/public/csa-purchase-requests/{request_id}/confirm-payment"


@pytest.mark.django_db
def test_terms_validation_rejects_a_noncanonical_hash():
    terms = get_current_csa_terms("vi")
    with pytest.raises(InvalidCSATerms):
        validate_csa_terms_snapshot(
            version=terms["version"],
            locale=terms["locale"],
            content=terms["content"],
            content_hash="0" * 64,
        )


@pytest.mark.django_db
def test_current_v2_terms_match_the_document_without_changing_v1():
    from memberships.terms import CSA_TERMS_V1, CSA_TERMS_V2, get_csa_terms

    expected = {
        "vi": [
            "Canh tác tự nhiên — không dùng thuốc trừ sâu, thuốc diệt cỏ hay thuốc diệt nấm; bón phân từ sản phẩm lên men hữu cơ; kiểm soát sâu bệnh bằng côn trùng có ích, luân canh và quản lý đất hợp lý.",
            "Mỗi tuần tôi chọn từ danh sách CSA của tuần đó (sản phẩm có ký hiệu “|C”). Danh sách chỉ hiển thị những gì đã sẵn sàng, nên món tôi chọn không bao giờ hết hàng.",
            "Tôi có thể phối hợp cả ba nhóm — rau ăn lá; rau thơm; và củ, quả & rau — trong hạn mức mỗi nhóm theo gói (1 người: 10 · 2–3 người: 20 · 4–7 người: 30 món mỗi nhóm, mỗi tuần).",
            "Các loại thực phẩm thay đổi theo tuần, tùy mùa vụ, thời tiết và thu hoạch.",
            "Nếu một món sắp hết, nông trại có thể thay bằng món tươi tương đương và sẽ báo cho tôi trước.",
            "Phần của tôi được giao miễn phí đến địa chỉ đã đăng ký vào mỗi thứ Sáu, trong vòng 24 giờ sau thu hoạch, khoảng 8:00–18:00. Tôi sẽ giữ liên lạc trong ngày giao; nếu không liên lạc được, việc giao và chất lượng sản phẩm ngày hôm sau thuộc trách nhiệm của tôi. Nông trại không chịu trách nhiệm về sự cố từ đơn vị vận chuyển bên thứ ba nhưng sẽ hỗ trợ giải quyết.",
            "Tôi có thể tạm dừng hoặc bỏ qua một tuần (ví dụ khi đi xa) bằng cách báo trước cho nông trại.",
            "Nông trại liên hệ với tôi chủ yếu qua Zalo/WhatsApp, và cập nhật các loại rau có sẵn mỗi tuần trên trang web. Tôi sẽ báo mọi thay đổi về địa chỉ hoặc thông tin liên hệ, và có thể gửi nhận xét bất cứ lúc nào.",
            "Tôi đồng ý được thêm vào nhóm trò chuyện chung của tất cả thành viên trên Zalo và/hoặc WhatsApp.",
            "Nông trại có thể điều chỉnh một số phần của thỏa thuận này (cách trồng và giao hàng) theo thời gian, và sẽ thông báo cho tôi trước khi thay đổi.",
            "Trong phạm vi pháp luật cho phép, tôi miễn trừ trách nhiệm cho Natural Farming Vietnam, chủ sở hữu, đại lý và nhân viên đối với các khiếu nại hay thiệt hại liên quan đến tài sản nông trại, việc giao/nhận hàng, hoặc tư cách thành viên của tôi.",
        ],
        "en": [
            "Naturally farmed — grown with no pesticides, herbicides, or fungicides; fertilized with fermented organic inputs; pests managed with beneficial insects, crop rotation, and healthy soil.",
            "Each week I choose from that week's CSA list (items marked “|C”). The list shows only what's ready, so nothing I pick is out of stock.",
            "I can mix any of the three groups — leafy greens; herbs; and roots, fruits & vegetables — up to my plan's limit per group (1 person: 10 · 2–3 people: 20 · 4–7 people: 30 items per group, each week).",
            "What's offered changes each week with the season, weather, and harvest.",
            "If something runs low, the farm may substitute an equally fresh item and will let me know first.",
            "My share is delivered free to my registered address every Friday, within 24 hours of harvest, roughly 8:00–18:00. I will stay reachable on delivery day; if I cannot be reached, next-day delivery and product quality are my responsibility. The farm is not liable for third-party courier problems but will help resolve them.",
            "I can skip or pause a week (for example, when traveling) by telling the farm in advance.",
            "The farm contacts me mainly by Zalo/WhatsApp, and posts each week's available crops on the website. I will report any change to my address or contact details, and I am welcome to send feedback anytime.",
            "I agree to be added to a group chat of all members on Zalo and/or WhatsApp.",
            "The farm may adjust parts of this agreement (how it grows and delivers) over time, and will notify me before any change.",
            "To the extent permitted by law, I release Natural Farming Vietnam, its owner, agents, and employees from liability for claims or damages relating to farm property, delivery/pickup, or my membership.",
        ],
    }

    for locale, items in expected.items():
        old = get_csa_terms(version=CSA_TERMS_V1, locale=locale)
        current = get_csa_terms(version=CSA_TERMS_V2, locale=locale)
        assert old["hash"] != current["hash"]
        assert [section["paragraphs"][0] for section in current["content"]["sections"]] == items
        assert all(not section["heading"] for section in current["content"]["sections"])


@pytest.mark.django_db
def test_legacy_v2_terms_snapshot_remains_valid():
    import memberships.terms as terms_module
    from memberships.terms import CSA_TERMS_V2

    legacy_content = terms_module._CSA_TERMS_LEGACY_CONTENT[CSA_TERMS_V2]["vi"]
    validate_csa_terms_snapshot(
        version=CSA_TERMS_V2,
        locale="vi",
        content=legacy_content,
        content_hash=canonical_csa_terms_hash(legacy_content),
    )


@pytest.mark.django_db
def test_v1_purchase_terms_remain_valid_after_v2_becomes_current(
    valid_payload, payment_settings, monkeypatch
):
    import memberships.terms as terms_module

    with monkeypatch.context() as patcher:
        patcher.setattr(terms_module, "CURRENT_CSA_TERMS_VERSION", "CSA_TERMS_V1")
        response = APIClient().post(CREATE_URL, valid_payload, format="json")
    assert response.status_code == 201
    item = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    assert item.terms_version == "CSA_TERMS_V1"
    assert terms_module.CURRENT_CSA_TERMS_VERSION == "CSA_TERMS_V2"
    validate_csa_terms_snapshot(
        version=item.terms_version,
        locale=item.terms_locale,
        content=item.terms_snapshot,
        content_hash=item.terms_hash,
    )
    item.full_clean()


@pytest.mark.django_db
def test_guest_creates_request_and_receives_one_time_confirmation_token(
    valid_payload, payment_settings
):
    response = APIClient().post(CREATE_URL, valid_payload, format="json")

    assert response.status_code == 201
    data = response.json()["data"]
    purchase = CSAPurchaseRequest.objects.get(pk=data["id"])
    token = data["guest_confirmation_token"]
    assert purchase.user is None
    assert purchase.phone_e164 == "+84912345678"
    assert purchase.guest_token_hash != token
    assert check_password(token, purchase.guest_token_hash)
    assert purchase.terms_version == "CSA_TERMS_V2"
    assert purchase.terms_locale == "vi"
    assert purchase.terms_accepted_at is not None
    assert purchase.terms_acceptance_method == "online"
    assert purchase.terms_hash == canonical_csa_terms_hash(purchase.terms_snapshot)
    assert data["terms_version"] == purchase.terms_version
    assert data["terms_locale"] == "vi"
    assert data["terms_accepted_at"] == purchase.terms_accepted_at.isoformat()
    assert "terms_snapshot" not in data
    assert "terms_hash" not in data
    assert "terms_acceptance_ip" not in data
    assert "terms_acceptance_user_agent" not in data
    assert "guest_token_hash" not in data
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("terms_accepted", [None, False, "true", 1])
@pytest.mark.django_db
def test_guest_requires_strict_boolean_terms_acceptance(
    terms_accepted, valid_payload, payment_settings
):
    payload = {**valid_payload}
    if terms_accepted is None:
        payload.pop("terms_accepted")
    else:
        payload["terms_accepted"] = terms_accepted

    response = APIClient().post(CREATE_URL, payload, format="json")

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "csa_purchase_terms_required"
    assert "terms_accepted" in response.json()["errors"][0]["fields"]
    assert CSAPurchaseRequest.objects.count() == 0


@pytest.mark.django_db
def test_authenticated_user_must_accept_terms(
    package_and_option,
    payment_settings,
    complete_member_profile,
    access_token,
):
    package, option = package_and_option
    response = bearer_client(access_token).post(
        CREATE_URL,
        {"package_id": str(package.pk), "price_option_id": str(option.pk)},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "csa_purchase_terms_required"
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_guest_optional_contact_fields_are_validated_and_snapshotted(
    valid_payload, payment_settings
):
    response = APIClient().post(
        CREATE_URL,
        {
            **valid_payload,
            "email": "buyer@example.com",
            "zalo_whatsapp": " +84 912 345 678 ",
        },
        format="json",
        REMOTE_ADDR="203.0.113.20",
        HTTP_USER_AGENT="CSA test agent",
    )

    assert response.status_code == 201
    purchase = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    assert purchase.email_snapshot == "buyer@example.com"
    assert purchase.zalo_whatsapp_snapshot == "+84 912 345 678"
    assert purchase.terms_acceptance_ip == "203.0.113.20"
    assert purchase.terms_acceptance_user_agent == "CSA test agent"


@pytest.mark.django_db
def test_guest_rejects_invalid_optional_email(valid_payload, payment_settings):
    response = APIClient().post(
        CREATE_URL,
        {**valid_payload, "email": "not-an-email"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "invalid_csa_purchase_request"
    assert "email" in response.json()["errors"][0]["fields"]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.parametrize("locale", ["vi", "en"])
@pytest.mark.django_db
def test_purchase_snapshots_the_server_terms_for_supported_locale(
    locale, valid_payload, payment_settings
):
    response = APIClient().post(
        CREATE_URL,
        {**valid_payload, "terms_locale": locale},
        format="json",
    )

    assert response.status_code == 201
    purchase = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    expected = get_current_csa_terms(locale)
    assert purchase.terms_version == expected["version"]
    assert purchase.terms_locale == expected["locale"]
    assert purchase.terms_snapshot == expected["content"]
    assert purchase.terms_hash == expected["hash"]


@pytest.mark.django_db
def test_purchase_rejects_unknown_terms_locale(valid_payload, payment_settings):
    response = APIClient().post(
        CREATE_URL,
        {**valid_payload, "terms_locale": "fr"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "invalid_csa_purchase_request"
    assert "terms_locale" in response.json()["errors"][0]["fields"]


@pytest.mark.parametrize("field", ["terms_version", "terms_content", "terms_hash"])
@pytest.mark.django_db
def test_purchase_never_trusts_client_terms_snapshot_fields(field, valid_payload, payment_settings):
    response = APIClient().post(
        CREATE_URL,
        {**valid_payload, field: "client-controlled"},
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "invalid_csa_purchase_request"
    assert field in response.json()["errors"][0]["fields"]
    assert not CSAPurchaseRequest.objects.exists()


@pytest.mark.django_db
def test_purchase_terms_snapshot_does_not_follow_a_new_current_version(
    valid_payload, payment_settings, monkeypatch
):
    import memberships.terms as terms_module

    created = APIClient().post(CREATE_URL, valid_payload, format="json")
    purchase = CSAPurchaseRequest.objects.get(pk=created.json()["data"]["id"])
    original_content = purchase.terms_snapshot
    original_hash = purchase.terms_hash
    version_three_content = {
        "vi": {"title": "Điều khoản V3", "sections": []},
        "en": {"title": "Terms V3", "sections": []},
    }
    monkeypatch.setitem(terms_module._CSA_TERMS_CONTENT, "CSA_TERMS_V3", version_three_content)
    monkeypatch.setattr(terms_module, "CURRENT_CSA_TERMS_VERSION", "CSA_TERMS_V3")

    assert get_current_csa_terms("vi")["version"] == "CSA_TERMS_V3"
    purchase.refresh_from_db()
    assert purchase.terms_version == "CSA_TERMS_V2"
    assert purchase.terms_snapshot == original_content
    assert purchase.terms_hash == original_hash


@pytest.mark.django_db
def test_authenticated_user_creates_only_their_own_request(
    package_and_option,
    payment_settings,
    active_user,
    complete_member_profile,
    access_token,
):
    package, option = package_and_option
    response = bearer_client(access_token).post(
        CREATE_URL,
        {
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "terms_accepted": True,
        },
        format="json",
    )

    assert response.status_code == 201
    data = response.json()["data"]
    purchase = CSAPurchaseRequest.objects.get(pk=data["id"])
    assert purchase.user == active_user
    assert purchase.name_snapshot == active_user.name
    assert purchase.phone_e164 == "+84901234567"
    assert purchase.province_code_snapshot == complete_member_profile.province_code
    assert purchase.ward_code_snapshot == complete_member_profile.ward_code
    assert purchase.address_snapshot == complete_member_profile.address
    assert purchase.email_snapshot == active_user.email
    assert purchase.guest_token_hash == ""
    assert "guest_confirmation_token" not in data


@pytest.mark.django_db
def test_authenticated_request_snapshots_without_updating_auth_or_live_data(
    package_and_option,
    payment_settings,
    active_user,
    complete_member_profile,
    access_token,
):
    package, option = package_and_option
    original_user_name = active_user.name
    original_profile_values = (
        complete_member_profile.phone_number,
        complete_member_profile.province_code,
        complete_member_profile.ward_code,
        complete_member_profile.address,
    )
    response = bearer_client(access_token).post(
        CREATE_URL,
        {
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "terms_accepted": True,
            "zalo_whatsapp": "zalo-member",
        },
        format="json",
    )
    purchase = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    active_user.refresh_from_db()
    complete_member_profile.refresh_from_db()
    assert active_user.name == original_user_name
    assert (
        complete_member_profile.phone_number,
        complete_member_profile.province_code,
        complete_member_profile.ward_code,
        complete_member_profile.address,
    ) == original_profile_values

    active_user.name = "Tên mới sau khi mua"
    active_user.email = "new-email@example.com"
    active_user.save(update_fields=["name", "email", "updated_at"])
    complete_member_profile.phone_number = "0987654321"
    complete_member_profile.address = "Địa chỉ mới"
    complete_member_profile.save(update_fields=["phone_number", "address", "updated_at"])
    package.name = "Tên gói mới"
    package.save(update_fields=["name", "updated_at"])
    option.monthly_price_vnd = 900_000
    option.save(update_fields=["monthly_price_vnd", "updated_at"])
    payment_settings.account_number = "987654321"
    payment_settings.save(update_fields=["account_number", "updated_at"])
    purchase.refresh_from_db()

    assert purchase.name_snapshot == original_user_name
    assert purchase.email_snapshot != active_user.email
    assert purchase.phone_e164 == "+84901234567"
    assert purchase.address_snapshot == original_profile_values[3]
    assert purchase.package_name_snapshot == "Gói CSA Online"
    assert purchase.amount_snapshot == 1_200_000
    assert purchase.account_number_snapshot == "123456789"
    assert purchase.zalo_whatsapp_snapshot == "zalo-member"
    active_user.refresh_from_db()
    complete_member_profile.refresh_from_db()
    assert active_user.name == "Tên mới sau khi mua"
    assert complete_member_profile.phone_number == "0987654321"


@pytest.mark.django_db
def test_authenticated_identity_payload_is_ignored_in_favor_of_locked_profile(
    valid_payload,
    payment_settings,
    active_user,
    complete_member_profile,
    access_token,
):
    response = bearer_client(access_token).post(
        CREATE_URL,
        {
            **valid_payload,
            "name": "Tên giả",
            "phone": "0988888888",
            "province_code": "01",
            "ward_code": "00004",
            "address": "Địa chỉ giả",
            "email": "fake@example.com",
            "zalo_whatsapp": "zalo-contact",
            "terms_accepted": True,
        },
        format="json",
    )

    assert response.status_code == 201
    purchase = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    assert purchase.name_snapshot == active_user.name
    assert purchase.phone_e164 == "+84901234567"
    assert purchase.province_code_snapshot == "66"
    assert purchase.ward_code_snapshot == "22015"
    assert purchase.address_snapshot == "Địa chỉ từ Auth Account"
    assert purchase.email_snapshot == active_user.email
    assert purchase.zalo_whatsapp_snapshot == "zalo-contact"


@pytest.mark.parametrize(
    "missing_field",
    ["profile", "name", "phone_number", "province_code", "ward_code", "address"],
)
@pytest.mark.django_db
def test_authenticated_user_with_incomplete_auth_account_is_rejected(
    missing_field,
    package_and_option,
    payment_settings,
    active_user,
    complete_member_profile,
    access_token,
):
    package, option = package_and_option
    if missing_field == "profile":
        complete_member_profile.delete()
    elif missing_field == "name":
        User.objects.filter(pk=active_user.pk).update(name="")
    else:
        setattr(complete_member_profile, missing_field, "")
        complete_member_profile.save(update_fields=[missing_field, "updated_at"])

    response = bearer_client(access_token).post(
        CREATE_URL,
        {
            "package_id": str(package.pk),
            "price_option_id": str(option.pk),
            "terms_accepted": True,
        },
        format="json",
    )

    assert response.status_code == 400
    assert response.json()["errors"][0]["code"] == "csa_purchase_auth_account_incomplete"
    assert "Auth Account" in response.json()["errors"][0]["message"]
    assert CSAPurchaseRequest.objects.count() == 0


@pytest.mark.django_db
def test_authenticated_user_cannot_use_another_users_phone(
    valid_payload,
    payment_settings,
    complete_member_profile,
    access_token,
):
    other_user = User.objects.create_user(
        email="other-csa-buyer@example.com",
        name="Other buyer",
        password="StrongPassword12",
    )
    other_profile = MemberProfile.objects.create(
        user=other_user,
        phone_number="0988888888",
        province_code="01",
        ward_code="00004",
        address="Địa chỉ người khác",
    )

    response = bearer_client(access_token).post(
        CREATE_URL,
        {
            **valid_payload,
            "phone": other_profile.phone_number,
            "name": other_user.name,
            "province_code": other_profile.province_code,
            "ward_code": other_profile.ward_code,
            "address": other_profile.address,
        },
        format="json",
    )

    assert response.status_code == 201
    purchase = CSAPurchaseRequest.objects.get(pk=response.json()["data"]["id"])
    assert purchase.phone_e164 == "+84901234567"
    assert purchase.phone_e164 != "+84988888888"


@pytest.mark.django_db
def test_authenticated_user_cannot_submit_another_user_id(
    valid_payload, payment_settings, access_token
):
    payload = {**valid_payload, "user_id": str(uuid.uuid4())}

    response = bearer_client(access_token).post(CREATE_URL, payload, format="json")

    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["code"] == "invalid_csa_purchase_request"
    assert "user_id" in error["fields"]
    assert CSAPurchaseRequest.objects.count() == 0


@pytest.mark.parametrize(
    "missing_field",
    [
        "package_id",
        "price_option_id",
        "name",
        "phone",
        "province_code",
        "ward_code",
        "address",
    ],
)
@pytest.mark.django_db
def test_create_requires_every_public_field(missing_field, valid_payload, payment_settings):
    payload = {key: value for key, value in valid_payload.items() if key != missing_field}

    response = APIClient().post(CREATE_URL, payload, format="json")

    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["code"] == "invalid_csa_purchase_request"
    assert missing_field in error["fields"]
    assert CSAPurchaseRequest.objects.count() == 0


@pytest.mark.django_db
def test_create_rejects_invalid_phone(valid_payload, payment_settings):
    response = APIClient().post(
        CREATE_URL, {**valid_payload, "phone": "not-a-phone"}, format="json"
    )

    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["code"] == "invalid_csa_purchase_request"
    assert "phone" in error["fields"]


@pytest.mark.parametrize(
    ("overrides", "error_field"),
    [
        ({"province_code": "00"}, "province_code"),
        ({"province_code": "01", "ward_code": "22015"}, "ward_code"),
    ],
)
@pytest.mark.django_db
def test_create_rejects_invalid_province_or_ward(
    overrides, error_field, valid_payload, payment_settings
):
    response = APIClient().post(CREATE_URL, {**valid_payload, **overrides}, format="json")

    assert response.status_code == 400
    error = response.json()["errors"][0]
    assert error["code"] == "invalid_csa_purchase_request"
    assert error_field in error["fields"]


@pytest.mark.parametrize("inactive_target", ["package", "option"])
@pytest.mark.django_db
def test_create_rejects_inactive_package_or_option(
    inactive_target, valid_payload, payment_settings, package_and_option
):
    package, option = package_and_option
    target = package if inactive_target == "package" else option
    target.is_active = False
    target.save(update_fields=["is_active", "updated_at"])

    response = APIClient().post(CREATE_URL, valid_payload, format="json")

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_package_unavailable"}]


@pytest.mark.django_db
def test_create_rejects_option_from_another_package(valid_payload, payment_settings):
    other_package = MembershipPackage.objects.create(
        name="Gói khác",
        quota_policy=QuotaPolicy.EXPIRE,
    )
    other_option = MembershipPackagePriceOption.objects.create(
        package=other_package,
        duration_months=6,
        monthly_price_vnd=300_000,
    )

    response = APIClient().post(
        CREATE_URL,
        {**valid_payload, "price_option_id": str(other_option.pk)},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_package_unavailable"}]


@pytest.mark.django_db
def test_client_amount_is_ignored_and_payment_snapshot_is_returned(
    valid_payload, payment_settings, package_and_option
):
    package, option = package_and_option
    response = APIClient().post(CREATE_URL, {**valid_payload, "amount": 1}, format="json")

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["package_snapshot"] == {"id": str(package.pk), "name": package.name}
    assert data["price_option_snapshot"] == {
        "id": str(option.pk),
        "name": "3 months",
        "duration_months": 3,
    }
    assert data["amount"] == "1200000"
    assert data["currency"] == "VND"
    assert data["transfer_content"] == data["request_code"]
    assert data["bank_bin"] == TEST_BANK["bin"]
    assert data["bank_name"] == TEST_BANK["name"]
    assert data["account_number"] == payment_settings.account_number
    assert data["account_name"] == payment_settings.account_name
    assert data["qr_payload"] == {
        "acqId": TEST_BANK["bin"],
        "accountNo": payment_settings.account_number,
        "accountName": payment_settings.account_name,
        "amount": "1200000",
        "addInfo": data["request_code"],
        "format": "text",
        "template": "compact2",
    }


@pytest.mark.django_db
def test_duplicate_open_request_is_rejected_after_phone_normalization(
    valid_payload, payment_settings
):
    first = APIClient().post(CREATE_URL, valid_payload, format="json")
    second = APIClient().post(CREATE_URL, {**valid_payload, "phone": "+84912345678"}, format="json")

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["errors"] == [{"code": "csa_purchase_request_open_exists"}]
    assert CSAPurchaseRequest.objects.count() == 1


@pytest.mark.django_db
def test_missing_payment_settings_returns_stable_business_error(valid_payload):
    response = APIClient().post(CREATE_URL, valid_payload, format="json")

    assert response.status_code == 503
    assert response.json()["errors"] == [{"code": "csa_purchase_payment_unavailable"}]
    assert CSAPurchaseRequest.objects.count() == 0


@pytest.mark.django_db
def test_disabled_direct_transfer_returns_stable_business_error(valid_payload, payment_settings):
    payment_settings.direct_transfer_enabled = False
    payment_settings.save(update_fields=["direct_transfer_enabled", "updated_at"])

    response = APIClient().post(CREATE_URL, valid_payload, format="json")

    assert response.status_code == 503
    assert response.json()["errors"] == [{"code": "csa_purchase_payment_unavailable"}]
    assert CSAPurchaseRequest.objects.count() == 0


@pytest.mark.django_db
def test_unavailable_bank_directory_returns_stable_business_error(
    valid_payload, payment_settings, monkeypatch
):
    def unavailable():
        raise BankDirectoryUnavailable

    monkeypatch.setattr("memberships.banks.get_transfer_banks", unavailable)

    response = APIClient().post(CREATE_URL, valid_payload, format="json")

    assert response.status_code == 503
    assert response.json()["errors"] == [{"code": "csa_purchase_payment_unavailable"}]


@pytest.mark.django_db
def test_guest_confirms_payment_with_the_one_time_token(valid_payload, payment_settings):
    created = APIClient().post(CREATE_URL, valid_payload, format="json").json()["data"]

    response = APIClient().post(
        confirm_url(created["id"]),
        {"guest_confirmation_token": created["guest_confirmation_token"]},
        format="json",
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
    assert "guest_confirmation_token" not in data
    assert "guest_token_hash" not in data
    purchase = CSAPurchaseRequest.objects.get(pk=created["id"])
    assert purchase.payment_confirmed_at is not None


@pytest.mark.django_db
def test_confirm_response_keeps_original_package_price_and_payment_snapshots(
    valid_payload, payment_settings, package_and_option
):
    package, option = package_and_option
    created = APIClient().post(CREATE_URL, valid_payload, format="json").json()["data"]
    package.name = "Tên gói đã đổi"
    package.save(update_fields=["name", "updated_at"])
    option.monthly_price_vnd = 900_000
    option.save(update_fields=["monthly_price_vnd", "updated_at"])
    update_membership_payment_settings(
        bank_bin=TEST_BANK["bin"],
        bank_name=TEST_BANK["name"],
        account_number="999999999",
        account_name="NEW ACCOUNT",
        direct_transfer_enabled=True,
    )

    response = APIClient().post(
        confirm_url(created["id"]),
        {"guest_confirmation_token": created["guest_confirmation_token"]},
        format="json",
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["package_snapshot"]["name"] == "Gói CSA Online"
    assert data["amount"] == "1200000"
    assert data["account_number"] == "123456789"
    assert data["account_name"] == "NATURAL FARMING VIETNAM"


@pytest.mark.django_db
def test_authenticated_user_confirms_their_request_without_guest_token(
    valid_payload, payment_settings, complete_member_profile, access_token
):
    client = bearer_client(access_token)
    created = client.post(CREATE_URL, valid_payload, format="json").json()["data"]

    response = client.post(confirm_url(created["id"]), {}, format="json")

    assert response.status_code == 200
    assert response.json()["data"]["status"] == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED


@pytest.mark.django_db
def test_guest_wrong_or_missing_token_cannot_confirm_and_matches_unknown_request(
    valid_payload, payment_settings
):
    created = APIClient().post(CREATE_URL, valid_payload, format="json").json()["data"]

    wrong = APIClient().post(
        confirm_url(created["id"]),
        {"guest_confirmation_token": "wrong-token"},
        format="json",
    )
    missing = APIClient().post(confirm_url(created["id"]), {}, format="json")
    unknown = APIClient().post(
        confirm_url(uuid.uuid4()),
        {"guest_confirmation_token": "wrong-token"},
        format="json",
    )

    assert wrong.status_code == missing.status_code == unknown.status_code == 404
    assert wrong.json() == missing.json() == unknown.json()
    assert wrong.json()["errors"] == [{"code": "csa_purchase_request_not_found"}]
    assert CSAPurchaseRequest.objects.get(pk=created["id"]).status == (
        CSAPurchaseRequestStatus.PENDING
    )


@pytest.mark.parametrize(
    "blocked_status",
    [
        CSAPurchaseRequestStatus.PAYMENT_CONFIRMED,
        CSAPurchaseRequestStatus.APPROVED,
        CSAPurchaseRequestStatus.REJECTED,
    ],
)
@pytest.mark.django_db
def test_guest_cannot_confirm_non_pending_request_again(
    blocked_status, valid_payload, payment_settings
):
    created = APIClient().post(CREATE_URL, valid_payload, format="json").json()["data"]
    purchase = CSAPurchaseRequest.objects.get(pk=created["id"])
    purchase.status = blocked_status
    purchase.save(update_fields=["status", "updated_at"])

    response = APIClient().post(
        confirm_url(purchase.pk),
        {"guest_confirmation_token": created["guest_confirmation_token"]},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"] == [{"code": "csa_purchase_request_not_pending"}]


@pytest.mark.django_db
def test_create_endpoint_is_throttled(valid_payload, payment_settings):
    client = APIClient()
    responses = [
        client.post(CREATE_URL, {**valid_payload, "phone": "invalid"}, format="json")
        for _attempt in range(6)
    ]

    assert [response.status_code for response in responses[:5]] == [400] * 5
    assert responses[5].status_code == 429


@pytest.mark.skipif(
    not connection.features.has_select_for_update,
    reason="SQLite does not provide row-level SELECT FOR UPDATE semantics",
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_create_cannot_create_two_open_requests(package_and_option, payment_settings):
    package, option = package_and_option
    barrier = threading.Barrier(3)
    outcomes = []

    def create_once():
        close_old_connections()
        try:
            barrier.wait()
            create_public_csa_purchase_request(
                package_id=package.pk,
                price_option_id=option.pk,
                phone="0912345678",
                name="Nguyễn Văn An",
                province_code="66",
                ward_code="22015",
                address="Số 12, ngõ 5",
            )
            outcomes.append("created")
        except PurchaseRequestConflict:
            outcomes.append("conflict")
        finally:
            close_old_connections()

    threads = [threading.Thread(target=create_once) for _index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "created"]
    assert CSAPurchaseRequest.objects.filter(phone_e164="+84912345678", open_slot=True).count() == 1
