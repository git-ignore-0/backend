import hashlib
import uuid
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import FileSystemStorage
from django.utils import timezone
from pypdf import PdfReader
from reportlab.graphics.barcode.qr import QrCodeWidget as ReportLabQRCodeWidget

import memberships.terms as terms_catalog
from identity.models import AccountStatus, User
from memberships.contract_pdf_templates import contract_verification_url
from memberships.contract_pdfs import (
    CONTRACT_PDF_MARGIN_BOTTOM,
    CONTRACT_PDF_MARGIN_HORIZONTAL,
    CONTRACT_PDF_MARGIN_TOP,
    CSAContractPDFError,
    CSAContractPDFSnapshotIncomplete,
    CSAContractPDFStorageError,
    generate_contract_documents,
    generate_revoked_contract_documents,
    get_contract_pdf_storage,
    render_contract_pdf,
)
from memberships.contracts import CSAContractSnapshot, create_manual_contract
from memberships.models import (
    CSAContractStatus,
    CSAContractTermsAcceptanceMethod,
    CSACustomer,
    Membership,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentSettings,
    MembershipStatus,
    QuotaPolicy,
)
from memberships.phones import normalize_vietnam_phone
from memberships.terms import CSA_TERMS_V1, get_csa_terms


def _pdf_text(content):
    reader = PdfReader(BytesIO(content))
    return "\n".join(page.extract_text() for page in reader.pages), len(reader.pages)


@pytest.fixture
def contract_snapshot():
    return CSAContractSnapshot(
        member_name="Nguyễn Văn An",
        phone_e164=normalize_vietnam_phone("0912345678"),
        email="an@example.com",
        zalo_whatsapp="0912 345 678",
        province_code="66",
        ward_code="22015",
        address="123 Đường Vườn Rau, Bến Tre",
        package_name="Gói Rau Mùa Việt",
        price_option_name="Gói 3 tháng",
        duration_months=3,
        amount=1_500_000,
        currency="VND",
        quota={"policy": "rollover", "items": [{"product": "Rau củ", "units": 2}]},
        terms_version=CSA_TERMS_V1,
        terms_locale="vi",
        terms=get_csa_terms(version=CSA_TERMS_V1, locale="vi")["content"],
        terms_accepted_at=timezone.now(),
        terms_acceptance_method=CSAContractTermsAcceptanceMethod.IN_STORE,
        terms_acceptance_ip=None,
        terms_acceptance_user_agent=None,
        start_date=date(2026, 9, 10),
        end_date=date(2026, 10, 10),
        payment={
            "bank_bin": "970436",
            "bank_name": "Vietcombank",
            "account_number": "123456789",
            "account_name": "NATURAL FARMING VIETNAM",
            "transfer_content": "CSA THANH TOAN 123",
        },
        terms_vi=get_csa_terms(version=CSA_TERMS_V1, locale="vi")["content"],
        terms_en=get_csa_terms(version=CSA_TERMS_V1, locale="en")["content"],
    )


@pytest.fixture
def contract(db, contract_snapshot):
    package = MembershipPackage.objects.create(
        name="Live package name",
        description="Live description",
        quota_policy=QuotaPolicy.ROLLOVER,
    )
    option = MembershipPackagePriceOption.objects.create(
        package=package,
        duration_months=3,
        monthly_price_vnd=500_000,
    )
    customer = CSACustomer.objects.create(
        name="Live customer name",
        phone_e164=contract_snapshot.phone_e164,
        province_code="66",
        ward_code="22015",
        address="Live address",
    )
    membership = Membership.objects.create(
        customer=customer,
        package=package,
        price_option=option,
        package_name_snapshot=contract_snapshot.package_name,
        package_description_snapshot="Snapshot description",
        monthly_price_vnd_snapshot=500_000,
        total_price_vnd_snapshot=contract_snapshot.amount,
        duration_months_snapshot=contract_snapshot.duration_months,
        quota_policy_snapshot=QuotaPolicy.ROLLOVER,
        farmbrite_order_reference=f"ORDER-{uuid.uuid4()}",
        farmbrite_order_reference_key=f"order-{uuid.uuid4()}",
        start_date=contract_snapshot.start_date,
        end_date=contract_snapshot.end_date,
        status=MembershipStatus.SCHEDULED,
        current_slot=True,
    )
    return create_manual_contract(membership_id=membership.pk, snapshot=contract_snapshot)


@pytest.fixture
def private_storage(tmp_path):
    return FileSystemStorage(location=tmp_path / "private-contracts", base_url=None)


@pytest.mark.django_db
def test_renders_valid_vietnamese_and_english_pdfs_from_contract_snapshot(contract):
    vi = render_contract_pdf(contract, "vi", pdf_version=1)
    en = render_contract_pdf(contract, "en", pdf_version=1)
    vi_text, vi_pages = _pdf_text(vi)
    en_text, en_pages = _pdf_text(en)

    assert vi.startswith(b"%PDF-") and en.startswith(b"%PDF-")
    assert vi_pages > 0 and en_pages > 0
    assert vi != en
    assert "Natural Farming Vietnam" in vi_text
    assert "Phiếu Đăng Ký" in vi_text
    assert "CSA Sign-Up & Agreement" in en_text
    assert "1. Hai bên" in vi_text
    assert "2. Chọn gói của bạn" in vi_text
    assert "3. Thanh toán" in vi_text
    assert "4. Chương trình hoạt động thế nào" in vi_text
    assert "5. Xác nhận & chữ ký" in vi_text
    assert "1. The two parties" in en_text
    assert "2. Choose your plan" in en_text
    assert "3. Payment" in en_text
    assert "4. How the program works" in en_text
    assert "5. Confirmation & signatures" in en_text
    assert contract.reference_code in vi_text and contract.reference_code in en_text
    assert "Gói Rau Mùa Việt" in vi_text
    assert "Gói 3 tháng" in vi_text
    assert "10/09/2026" in vi_text
    assert "09/10/2026" in vi_text
    assert "Nguyễn Văn An" in vi_text
    assert "123 Đường Vườn Rau, Bến Tre" in vi_text
    assert "Vietcombank" in vi_text
    assert "123456789" in vi_text
    assert "CSA THANH TOAN 123" in vi_text
    assert "Thông tin các bên" in vi_text
    assert "Party information" in en_text
    assert "Khách hàng xác nhận đã đọc, hiểu và đồng ý" in vi_text
    assert "The member confirms that they have read, understood" in en_text
    assert "Thanh toán một lần" in vi_text
    assert "Thanh toán một lần" in vi_text
    assert '{"' not in vi_text
    assert "Mã hash điều khoản" not in vi_text
    assert "☐" not in vi_text
    assert "ĐÃ THU HỒI" not in vi_text


@pytest.mark.django_db
def test_quota_snapshot_uses_a_localized_three_column_table(contract):
    text, _pages = _pdf_text(render_contract_pdf(contract, "vi", pdf_version=1))

    assert "Nhóm sản phẩm" in text
    assert "Hạn mức" in text
    assert "Đơn vị" in text
    assert "Rau củ" in text
    assert "2" in text


@pytest.mark.django_db
def test_contract_pdf_uses_a4_template_geometry_and_never_exposes_snapshot_metadata(contract):
    content = render_contract_pdf(contract, "vi", pdf_version=1)
    reader = PdfReader(BytesIO(content))
    page = reader.pages[0]
    text = "\n".join(item.extract_text() for item in reader.pages)

    assert float(page.mediabox.width) == pytest.approx(595.2756)
    assert float(page.mediabox.height) == pytest.approx(841.8898)
    assert CONTRACT_PDF_MARGIN_TOP == pytest.approx(14 * 72 / 25.4)
    assert CONTRACT_PDF_MARGIN_BOTTOM == pytest.approx(14 * 72 / 25.4)
    assert CONTRACT_PDF_MARGIN_HORIZONTAL == pytest.approx(15 * 72 / 25.4)
    assert "quota_snapshot" not in text
    assert "payment_snapshot" not in text
    assert "terms_hash" not in text


@pytest.mark.django_db
def test_installment_version_renders_snapshot_payment_summary_and_schedule(contract):
    version = SimpleNamespace(
        contract_id=contract.pk,
        version_number=2,
        contract_status=CSAContractStatus.ACTIVE,
        issued_at=contract.issued_at,
        start_date=contract.start_date,
        end_date=contract.end_date,
        revoked_at=None,
        revocation_reason="",
        revoked_by_name_snapshot="",
        package_name_snapshot=contract.package_name_snapshot,
        price_option_name_snapshot=contract.price_option_name_snapshot,
        duration_months_snapshot=contract.duration_months_snapshot,
        amount_snapshot=contract.amount_snapshot,
        currency_snapshot=contract.currency_snapshot,
        payment_type="installment",
        installment_count=2,
        total_payment_amount=1_500_000,
        paid_amount=750_000,
        remaining_amount=750_000,
        paid_cycles=1,
        total_cycles=3,
        payment_schedule_snapshot=[
            {
                "sequence": 2,
                "amount_due": "750000",
                "amount_paid": None,
                "cycle_count": 2,
                "due_at": "2026-10-10T00:00:00+00:00",
                "status": "pending",
            },
            {
                "sequence": 1,
                "amount_due": "750000",
                "amount_paid": "750000",
                "cycle_count": 1,
                "due_at": "2026-09-10T00:00:00+00:00",
                "status": "confirmed",
            },
        ],
    )

    text, _pages = _pdf_text(render_contract_pdf(contract, "vi", pdf_version=2, version=version))

    assert "Trả góp 2 lần" in text
    assert "1.500.000 VND" in text
    assert "750.000 VND" in text
    assert "Lịch thanh toán" in text
    assert text.index("10/09/2026") < text.index("10/10/2026")


@pytest.mark.django_db
def test_bilingual_pdfs_embed_only_the_public_contract_verification_url(
    contract, monkeypatch, settings
):
    settings.PUBLIC_SITE_ORIGINS = ("https://nongnghiepthiennhien.com",)
    qr_values = []

    class CapturingQRCodeWidget(ReportLabQRCodeWidget):
        def __init__(self, value, *args, **kwargs):
            qr_values.append(value)
            super().__init__(value, *args, **kwargs)

    monkeypatch.setattr(
        "memberships.contract_pdf_templates.QrCodeWidget",
        CapturingQRCodeWidget,
    )
    expected_url = (
        f"https://nongnghiepthiennhien.com/csa/verify?reference={contract.reference_code}"
    )

    vi_text, _pages = _pdf_text(render_contract_pdf(contract, "vi", pdf_version=1))
    en_text, _pages = _pdf_text(render_contract_pdf(contract, "en", pdf_version=1))

    assert contract_verification_url(contract.reference_code) == expected_url
    assert qr_values == [expected_url, expected_url]
    assert "Quét để xác minh trạng thái hợp đồng" in vi_text
    assert "Scan to verify contract status" in en_text
    assert expected_url in vi_text and expected_url in en_text
    for sensitive in (
        contract.phone_e164_snapshot,
        contract.member_name_snapshot,
        contract.pdf_vi_storage_key,
        "guest_token",
        "private/contracts",
    ):
        assert not sensitive or sensitive not in "".join(qr_values)


@pytest.mark.django_db
def test_pdf_only_contains_selected_package_not_live_catalog(contract):
    MembershipPackage.objects.create(
        name="Gói catalog không được chọn",
        description="Must not appear in PDF",
        quota_policy=QuotaPolicy.EXPIRE,
    )

    text, _pages = _pdf_text(render_contract_pdf(contract, "vi", pdf_version=1))

    assert "Gói Rau Mùa Việt" in text
    assert "Gói catalog không được chọn" not in text
    assert "Must not appear in PDF" not in text


@pytest.mark.django_db
def test_same_snapshot_produces_stable_pdf_hash_and_does_not_mutate_snapshot(contract):
    snapshot_before = {field: getattr(contract, field) for field in contract.IMMUTABLE_FIELDS}

    first = render_contract_pdf(contract, "vi", pdf_version=1)
    second = render_contract_pdf(contract, "vi", pdf_version=1)

    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    contract.refresh_from_db()
    assert snapshot_before == {
        field: getattr(contract, field) for field in contract.IMMUTABLE_FIELDS
    }


@pytest.mark.django_db
def test_live_data_and_current_terms_changes_do_not_change_pdf(contract, monkeypatch):
    user = User.objects.create_user(
        email="live-member@example.com",
        name="Live user before change",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    contract.membership.customer.user = user
    contract.membership.customer.save(update_fields=["user", "updated_at"])
    before = render_contract_pdf(contract, "vi", pdf_version=1)
    membership = contract.membership
    user.name = "Tên User live đã đổi"
    user.email = "changed-live-member@example.com"
    user.save(update_fields=["name", "email", "updated_at"])
    membership.customer.name = "Tên live đã đổi"
    membership.customer.address = "Địa chỉ live đã đổi"
    membership.customer.save(update_fields=["name", "address", "updated_at"])
    membership.package.name = "Tên package live đã đổi"
    membership.package.save(update_fields=["name", "updated_at"])
    MembershipPaymentSettings.objects.bulk_create(
        [
            MembershipPaymentSettings(
                bank_bin="970436",
                bank_name="Vietcombank",
                account_number="999999999",
                account_name="LIVE SETTINGS",
            )
        ]
    )
    monkeypatch.setitem(
        terms_catalog._CSA_TERMS_CONTENT[CSA_TERMS_V1]["vi"],
        "title",
        "Điều khoản live đã đổi",
    )

    after = render_contract_pdf(contract, "vi", pdf_version=1)

    assert before == after
    text, _pages = _pdf_text(after)
    assert "Tên live đã đổi" not in text
    assert "Tên User live đã đổi" not in text
    assert "LIVE SETTINGS" not in text
    assert "Điều khoản live đã đổi" not in text
    assert "NATURAL FARMING VIETNAM" in text


@pytest.mark.django_db
def test_generates_private_current_and_original_documents_with_matching_hashes(
    contract, private_storage
):
    documents = generate_contract_documents(contract, storage=private_storage)
    contract.refresh_from_db()

    assert documents.version == 1
    assert contract.pdf_vi_storage_key == contract.original_pdf_vi_storage_key
    assert contract.pdf_en_storage_key == contract.original_pdf_en_storage_key
    assert contract.pdf_vi_sha256 == contract.original_pdf_vi_sha256
    assert contract.pdf_en_sha256 == contract.original_pdf_en_sha256
    assert contract.pdf_generated_at is not None
    for locale, key, expected_hash in (
        ("vi", contract.pdf_vi_storage_key, contract.pdf_vi_sha256),
        ("en", contract.pdf_en_storage_key, contract.pdf_en_sha256),
    ):
        assert private_storage.exists(key)
        assert key.endswith(f".{locale}.pdf")
        assert len(key.split("/", 1)[0]) == 32
        assert key.split("/", 1)[0] != contract.reference_code
        assert "guest" not in key.lower()
        with private_storage.open(key, "rb") as stored:
            content = stored.read()
        assert hashlib.sha256(content).hexdigest() == expected_hash
        text, pages = _pdf_text(content)
        assert pages > 0
        assert contract.reference_code in text
        assert "guest" not in text.lower()


def test_default_contract_storage_is_private_and_outside_media_root():
    storage = get_contract_pdf_storage()

    assert Path(storage.location) != Path(settings.MEDIA_ROOT)
    assert not Path(storage.location).is_relative_to(Path(settings.MEDIA_ROOT))
    with pytest.raises(NotImplementedError, match="do not have public URLs"):
        storage.url("untrusted.pdf")


@pytest.mark.django_db
def test_active_generation_is_idempotent(contract, private_storage):
    first = generate_contract_documents(contract, storage=private_storage)
    second = generate_contract_documents(contract, storage=private_storage)

    assert first == second
    assert len(list(Path(private_storage.location).glob("**/*.pdf"))) == 2


@pytest.mark.django_db
def test_revoked_pdf_preserves_original_and_updates_current(contract, private_storage):
    generate_contract_documents(contract, storage=private_storage)
    contract.refresh_from_db()
    original_keys = (
        contract.original_pdf_vi_storage_key,
        contract.original_pdf_en_storage_key,
    )
    original_hashes = (
        contract.original_pdf_vi_sha256,
        contract.original_pdf_en_sha256,
    )
    actor = User.objects.create_user(
        email="revoke-admin@example.com",
        name="Quản trị viên CSA",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    contract.status = CSAContractStatus.REVOKED
    contract.revoked_at = timezone.now()
    contract.revoked_by = actor
    contract.revocation_reason = "Khách hàng yêu cầu thu hồi"
    contract.save(
        update_fields=[
            "status",
            "revoked_at",
            "revoked_by",
            "revocation_reason",
            "updated_at",
        ]
    )

    documents = generate_revoked_contract_documents(contract, storage=private_storage)
    contract.refresh_from_db()

    assert documents.version == 2
    assert (
        contract.original_pdf_vi_storage_key,
        contract.original_pdf_en_storage_key,
    ) == original_keys
    assert (
        contract.original_pdf_vi_sha256,
        contract.original_pdf_en_sha256,
    ) == original_hashes
    assert contract.pdf_vi_storage_key != contract.original_pdf_vi_storage_key
    assert contract.pdf_en_storage_key != contract.original_pdf_en_storage_key
    assert contract.pdf_vi_sha256 != contract.original_pdf_vi_sha256
    with private_storage.open(contract.pdf_vi_storage_key, "rb") as stored:
        revoked_content = stored.read()
    revoked_text, _pages = _pdf_text(revoked_content)
    assert "Hợp đồng này đã bị thu hồi." in revoked_text
    assert "ĐÃ THU HỒI" in revoked_text
    assert "Khách hàng yêu cầu thu hồi" in revoked_text
    assert "Quản trị viên CSA" in revoked_text
    assert "Quét để xác minh trạng thái hợp đồng" in revoked_text
    assert contract_verification_url(contract.reference_code) in revoked_text
    with private_storage.open(contract.original_pdf_vi_storage_key, "rb") as stored:
        original_text, _pages = _pdf_text(stored.read())
    assert "HỢP ĐỒNG ĐÃ BỊ THU HỒI" not in original_text
    assert private_storage.exists(original_keys[0])
    assert private_storage.exists(original_keys[1])

    attempted_changes = (
        ("revocation_reason", "Lý do không được phép sửa"),
        ("revoked_at", contract.revoked_at + timedelta(days=1)),
        ("revoked_by_name_snapshot", "Tên admin không được phép sửa"),
        ("status", CSAContractStatus.ACTIVE),
    )
    for field, value in attempted_changes:
        setattr(contract, field, value)
        with pytest.raises(ValidationError, match="immutable after revocation"):
            contract.save(update_fields=[field, "updated_at"])
        contract.refresh_from_db()

    other_actor = User.objects.create_user(
        email="other-revoke-admin@example.com",
        name="Admin khác",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    contract.revoked_by = other_actor
    with pytest.raises(ValidationError, match="immutable after revocation"):
        contract.save(update_fields=["revoked_by", "updated_at"])
    contract.refresh_from_db()

    repeated = generate_revoked_contract_documents(contract, storage=private_storage)
    assert repeated.version == 2
    with private_storage.open(contract.pdf_vi_storage_key, "rb") as stored:
        current_text, _pages = _pdf_text(stored.read())
    assert "Khách hàng yêu cầu thu hồi" in current_text
    assert "Quản trị viên CSA" in current_text
    assert "Lý do không được phép sửa" not in current_text


@pytest.mark.django_db
def test_revoked_generation_repairs_current_pdf_when_persisted_snapshot_changes(
    contract, private_storage
):
    generate_contract_documents(contract, storage=private_storage)
    actor = User.objects.create_user(
        email="repair-admin@example.com",
        name="Admin ban đầu",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    contract.status = CSAContractStatus.REVOKED
    contract.revoked_at = timezone.now()
    contract.revoked_by = actor
    contract.revocation_reason = "Lý do ban đầu"
    contract.save(
        update_fields=[
            "status",
            "revoked_at",
            "revoked_by",
            "revocation_reason",
            "updated_at",
        ]
    )
    generate_revoked_contract_documents(contract, storage=private_storage)
    contract.refresh_from_db()
    original_keys = (
        contract.original_pdf_vi_storage_key,
        contract.original_pdf_en_storage_key,
    )
    old_current_keys = (contract.pdf_vi_storage_key, contract.pdf_en_storage_key)
    changed_at = contract.revoked_at + timedelta(hours=1)
    type(contract).objects.filter(pk=contract.pk).update(
        revocation_reason="Lý do thu hồi hiện tại",
        revoked_at=changed_at,
        revoked_by_name_snapshot="Admin hiện tại",
    )
    contract.refresh_from_db()

    repaired = generate_revoked_contract_documents(contract, storage=private_storage)
    contract.refresh_from_db()

    assert repaired.version == 3
    assert (
        contract.original_pdf_vi_storage_key,
        contract.original_pdf_en_storage_key,
    ) == original_keys
    assert (contract.pdf_vi_storage_key, contract.pdf_en_storage_key) != old_current_keys
    with private_storage.open(contract.pdf_vi_storage_key, "rb") as stored:
        current_text, _pages = _pdf_text(stored.read())
    assert "Lý do thu hồi hiện tại" in current_text
    assert "Admin hiện tại" in current_text
    assert timezone.localtime(changed_at).strftime("%d/%m/%Y %H:%M") in current_text
    assert "Lý do ban đầu" not in current_text


@pytest.mark.django_db
def test_revoked_regeneration_failure_keeps_complete_previous_metadata(
    contract, private_storage, monkeypatch
):
    generate_contract_documents(contract, storage=private_storage)
    actor = User.objects.create_user(
        email="failed-regeneration-admin@example.com",
        name="Admin tạo bản thu hồi",
        password="StrongPassword12",
        status=AccountStatus.ACTIVE,
    )
    contract.status = CSAContractStatus.REVOKED
    contract.revoked_at = timezone.now()
    contract.revoked_by = actor
    contract.revocation_reason = "Lý do trước khi lỗi"
    contract.save(
        update_fields=[
            "status",
            "revoked_at",
            "revoked_by",
            "revocation_reason",
            "updated_at",
        ]
    )
    generate_revoked_contract_documents(contract, storage=private_storage)
    contract.refresh_from_db()
    metadata_before = (
        contract.pdf_version,
        contract.pdf_vi_storage_key,
        contract.pdf_en_storage_key,
        contract.pdf_vi_sha256,
        contract.pdf_en_sha256,
        contract.original_pdf_vi_storage_key,
        contract.original_pdf_en_storage_key,
    )
    type(contract).objects.filter(pk=contract.pk).update(revocation_reason="Lý do buộc sinh lại")
    contract.refresh_from_db()
    original_renderer = render_contract_pdf

    def fail_english(contract, locale, *, pdf_version):
        if locale == "en":
            raise RuntimeError("English renderer unavailable")
        return original_renderer(contract, locale, pdf_version=pdf_version)

    monkeypatch.setattr("memberships.contract_pdfs.render_contract_pdf", fail_english)

    with pytest.raises(CSAContractPDFError, match="rendering failed"):
        generate_revoked_contract_documents(contract, storage=private_storage)

    contract.refresh_from_db()
    assert (
        contract.pdf_version,
        contract.pdf_vi_storage_key,
        contract.pdf_en_storage_key,
        contract.pdf_vi_sha256,
        contract.pdf_en_sha256,
        contract.original_pdf_vi_storage_key,
        contract.original_pdf_en_storage_key,
    ) == metadata_before
    assert private_storage.exists(contract.pdf_vi_storage_key)
    assert private_storage.exists(contract.pdf_en_storage_key)


@pytest.mark.django_db
def test_render_failure_does_not_write_partial_metadata_or_files(
    contract, private_storage, monkeypatch
):
    original_renderer = render_contract_pdf

    def fail_english(contract, locale, *, pdf_version):
        if locale == "en":
            raise RuntimeError("secret guest token must not escape")
        return original_renderer(contract, locale, pdf_version=pdf_version)

    monkeypatch.setattr("memberships.contract_pdfs.render_contract_pdf", fail_english)

    with pytest.raises(CSAContractPDFError) as caught:
        generate_contract_documents(contract, storage=private_storage)

    assert str(caught.value) == "CSA contract PDF rendering failed"
    assert "secret" not in str(caught.value)
    contract.refresh_from_db()
    assert contract.pdf_vi_storage_key is None
    assert contract.pdf_en_storage_key is None
    assert contract.pdf_generated_at is None
    assert not list(Path(private_storage.location).glob("**/*.pdf"))


@pytest.mark.django_db
def test_storage_failure_removes_orphan_and_keeps_metadata_empty(contract, tmp_path):
    class FailSecondSaveStorage(FileSystemStorage):
        saves = 0

        def save(self, name, content, max_length=None):
            self.saves += 1
            if self.saves == 2:
                raise OSError("storage unavailable")
            return super().save(name, content, max_length=max_length)

    storage = FailSecondSaveStorage(location=tmp_path / "failed-private", base_url=None)

    with pytest.raises(CSAContractPDFStorageError):
        generate_contract_documents(contract, storage=storage)

    contract.refresh_from_db()
    assert contract.pdf_vi_storage_key is None
    assert contract.pdf_en_storage_key is None
    assert not list(Path(storage.location).glob("**/*.pdf"))


@pytest.mark.django_db
def test_legacy_contract_with_incomplete_pdf_snapshot_is_rejected(contract, private_storage):
    contract.terms_snapshot_en = None
    contract.payment_snapshot = None
    contract.terms_snapshot = {"title": "Legacy terms without payment"}
    type(contract).objects.filter(pk=contract.pk).update(
        terms_snapshot_en=None,
        payment_snapshot=None,
        terms_snapshot=contract.terms_snapshot,
    )
    contract.refresh_from_db()

    with pytest.raises(CSAContractPDFSnapshotIncomplete) as caught:
        generate_contract_documents(contract, storage=private_storage)

    assert caught.value.code == "csa_contract_pdf_snapshot_incomplete"
    assert not list(Path(private_storage.location).glob("**/*.pdf"))
