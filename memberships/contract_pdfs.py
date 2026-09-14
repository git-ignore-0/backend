import copy
import hashlib
import re
import secrets
import threading
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.db import transaction
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate

from memberships.contract_pdf_templates import (
    build_contract_story,
    build_pdf_styles,
    footer_labels,
)
from memberships.contract_provider import (
    InvalidCSAContractProvider,
    get_contract_provider,
)
from memberships.models import CSAContract, CSAContractStatus, CSAContractVersion
from memberships.terms import canonical_csa_terms_hash

SUPPORTED_CONTRACT_PDF_LOCALES = ("vi", "en")
_FONT_REGISTRATION_LOCK = threading.Lock()
CONTRACT_PDF_MARGIN_TOP = 14 * mm
CONTRACT_PDF_MARGIN_BOTTOM = 14 * mm
CONTRACT_PDF_MARGIN_HORIZONTAL = 15 * mm


class CSAContractPDFError(ValueError):
    code = "csa_contract_pdf_error"


class CSAContractPDFSnapshotIncomplete(CSAContractPDFError):
    code = "csa_contract_pdf_snapshot_incomplete"


class CSAContractPDFStorageError(CSAContractPDFError):
    code = "csa_contract_pdf_storage_error"


class CSAContractPDFStateError(CSAContractPDFError):
    code = "csa_contract_pdf_state_invalid"


class PrivateContractFileSystemStorage(FileSystemStorage):
    def url(self, name):
        raise NotImplementedError("Private CSA contract files do not have public URLs")


@dataclass(frozen=True)
class ContractPDFDocuments:
    vi_storage_key: str
    en_storage_key: str
    vi_sha256: str
    en_sha256: str
    generated_at: object
    version: int


def get_contract_pdf_storage():
    return PrivateContractFileSystemStorage(
        location=settings.CSA_CONTRACT_PRIVATE_STORAGE_ROOT,
        base_url=None,
        file_permissions_mode=0o600,
        directory_permissions_mode=0o700,
    )


def _font_names():
    regular_path = Path(settings.CSA_CONTRACT_PDF_FONT_PATH)
    bold_path = Path(settings.CSA_CONTRACT_PDF_BOLD_FONT_PATH)
    if not regular_path.is_file() or not bold_path.is_file():
        raise CSAContractPDFError("CSA contract PDF Unicode font is unavailable")
    digest = hashlib.sha256(f"{regular_path}|{bold_path}".encode()).hexdigest()[:12]
    regular_name = f"NFVContractRegular{digest}"
    bold_name = f"NFVContractBold{digest}"
    with _FONT_REGISTRATION_LOCK:
        registered = set(pdfmetrics.getRegisteredFontNames())
        if regular_name not in registered:
            pdfmetrics.registerFont(TTFont(regular_name, regular_path))
        if bold_name not in registered:
            pdfmetrics.registerFont(TTFont(bold_name, bold_path))
    return regular_name, bold_name


def _validated_terms(contract, locale):
    terms = contract.terms_snapshot_vi if locale == "vi" else contract.terms_snapshot_en
    if not isinstance(terms, dict) or not isinstance(terms.get("title"), str):
        raise CSAContractPDFSnapshotIncomplete("Localized CSA terms snapshot is incomplete")
    sections = terms.get("sections")
    if not isinstance(sections, list) or not sections:
        raise CSAContractPDFSnapshotIncomplete("Localized CSA terms snapshot is incomplete")
    for section in sections:
        if (
            not isinstance(section, dict)
            or not isinstance(section.get("heading"), str)
            or not isinstance(section.get("paragraphs"), list)
            or not section["paragraphs"]
            or not all(isinstance(paragraph, str) for paragraph in section["paragraphs"])
        ):
            raise CSAContractPDFSnapshotIncomplete("Localized CSA terms snapshot is incomplete")
    return terms


def _validated_payment(contract):
    payment = contract.payment_snapshot
    if payment is None and isinstance(contract.terms_snapshot, dict):
        payment = contract.terms_snapshot.get("payment")
    required = ("bank_name", "account_number", "account_name")
    if not isinstance(payment, dict) or any(
        not str(payment.get(field) or "").strip() for field in required
    ):
        raise CSAContractPDFSnapshotIncomplete("CSA contract payment snapshot is incomplete")
    return payment


def _validate_contract_snapshot(contract):
    required = (
        contract.reference_code,
        contract.member_name_snapshot,
        contract.phone_e164_snapshot,
        contract.address_snapshot,
        contract.package_name_snapshot,
        contract.price_option_name_snapshot,
        contract.currency_snapshot,
        contract.terms_version,
        contract.provider_version,
    )
    if any(not str(value or "").strip() for value in required):
        raise CSAContractPDFSnapshotIncomplete("CSA contract snapshot is incomplete")
    if contract.status == CSAContractStatus.REVOKED and (
        not contract.revoked_at
        or not contract.revocation_reason.strip()
        or not contract.revoked_by_name_snapshot
    ):
        raise CSAContractPDFSnapshotIncomplete("CSA contract revocation snapshot is incomplete")
    try:
        providers = {
            locale: get_contract_provider(contract.provider_version, locale)
            for locale in SUPPORTED_CONTRACT_PDF_LOCALES
        }
    except InvalidCSAContractProvider as error:
        raise CSAContractPDFSnapshotIncomplete(
            "CSA contract provider snapshot is unavailable"
        ) from error
    terms = {
        locale: _validated_terms(contract, locale) for locale in SUPPORTED_CONTRACT_PDF_LOCALES
    }
    return providers, terms, _validated_payment(contract)


def _page_decorator(*, contract, locale, pdf_version, regular_font, bold_font):
    labels = footer_labels(locale)

    def decorate(page_canvas, document):
        page_canvas.saveState()
        if contract.status == CSAContractStatus.REVOKED:
            page_canvas.setFont(bold_font, 34)
            page_canvas.setFillColor(colors.Color(0.71, 0.14, 0.11, alpha=0.12))
            page_canvas.translate(A4[0] / 2, A4[1] / 2)
            page_canvas.rotate(38)
            watermark = "ĐÃ THU HỒI" if locale == "vi" else "REVOKED"
            page_canvas.drawCentredString(0, 0, watermark)
            page_canvas.rotate(-38)
            page_canvas.translate(-A4[0] / 2, -A4[1] / 2)
        page_canvas.setStrokeColor(colors.HexColor("#D1D5DB"))
        page_canvas.line(
            CONTRACT_PDF_MARGIN_HORIZONTAL,
            12 * mm,
            A4[0] - CONTRACT_PDF_MARGIN_HORIZONTAL,
            12 * mm,
        )
        page_canvas.setFont(regular_font, 7.5)
        page_canvas.setFillColor(colors.HexColor("#4B5563"))
        page_canvas.drawString(CONTRACT_PDF_MARGIN_HORIZONTAL, 7 * mm, contract.reference_code)
        page_canvas.drawCentredString(
            A4[0] / 2,
            7 * mm,
            f"{labels['pdf_version']} {pdf_version}",
        )
        page_canvas.drawRightString(
            A4[0] - CONTRACT_PDF_MARGIN_HORIZONTAL,
            7 * mm,
            f"{labels['page']} {document.page}",
        )
        page_canvas.restoreState()

    return decorate


def _deterministic_canvas(*args, **kwargs):
    kwargs["invariant"] = 1
    return canvas.Canvas(*args, **kwargs)


def render_contract_pdf(
    contract: CSAContract,
    locale: str,
    *,
    pdf_version: int,
    version: CSAContractVersion | None = None,
) -> bytes:
    if locale not in SUPPORTED_CONTRACT_PDF_LOCALES:
        raise CSAContractPDFError("Unsupported CSA contract PDF locale")
    if version is not None:
        if version.contract_id != contract.pk or pdf_version != version.version_number:
            raise CSAContractPDFStateError("Contract version does not belong to this PDF")
        contract = copy.copy(contract)
        for field in (
            "status",
            "issued_at",
            "start_date",
            "end_date",
            "revoked_at",
            "revocation_reason",
            "revoked_by_name_snapshot",
            "package_name_snapshot",
            "price_option_name_snapshot",
            "duration_months_snapshot",
            "amount_snapshot",
            "currency_snapshot",
        ):
            source = "contract_status" if field == "status" else field
            setattr(contract, field, getattr(version, source))
    providers, terms_by_locale, payment = _validate_contract_snapshot(contract)
    regular_font, bold_font = _font_names()
    styles = build_pdf_styles(regular_font=regular_font, bold_font=bold_font)
    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        leftMargin=CONTRACT_PDF_MARGIN_HORIZONTAL,
        rightMargin=CONTRACT_PDF_MARGIN_HORIZONTAL,
        topMargin=CONTRACT_PDF_MARGIN_TOP,
        bottomMargin=CONTRACT_PDF_MARGIN_BOTTOM,
        title=contract.reference_code,
        author="Natural Farming Vietnam",
        subject="CSA Contract",
    )
    terms = terms_by_locale[locale]
    story = build_contract_story(
        contract=contract,
        locale=locale,
        provider=providers[locale],
        payment=payment,
        terms=terms,
        terms_hash=canonical_csa_terms_hash(terms),
        styles=styles,
        version=version,
    )
    decorate = _page_decorator(
        contract=contract,
        locale=locale,
        pdf_version=pdf_version,
        regular_font=regular_font,
        bold_font=bold_font,
    )
    document.build(
        story,
        onFirstPage=decorate,
        onLaterPages=decorate,
        canvasmaker=_deterministic_canvas,
    )
    return output.getvalue()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _storage_key(contract, locale, version):
    safe_reference = re.sub(r"[^A-Z0-9-]", "-", contract.reference_code.upper())
    return f"{secrets.token_hex(16)}/{safe_reference}.v{version}.{locale}.pdf"


def _delete_quietly(storage, keys):
    for key in keys:
        if not key:
            continue
        try:
            storage.delete(key)
        except Exception:
            pass


def delete_contract_documents(documents: ContractPDFDocuments, *, storage=None):
    """Best-effort compensation when the surrounding database transaction rolls back."""
    storage = storage or get_contract_pdf_storage()
    _delete_quietly(
        storage,
        {documents.vi_storage_key, documents.en_storage_key},
    )


def _documents(contract):
    return ContractPDFDocuments(
        vi_storage_key=contract.pdf_vi_storage_key,
        en_storage_key=contract.pdf_en_storage_key,
        vi_sha256=contract.pdf_vi_sha256,
        en_sha256=contract.pdf_en_sha256,
        generated_at=contract.pdf_generated_at,
        version=contract.pdf_version,
    )


def _metadata_is_complete(contract):
    return all(
        (
            contract.pdf_vi_storage_key,
            contract.pdf_en_storage_key,
            contract.pdf_vi_sha256,
            contract.pdf_en_sha256,
            contract.original_pdf_vi_storage_key,
            contract.original_pdf_en_storage_key,
            contract.original_pdf_vi_sha256,
            contract.original_pdf_en_sha256,
            contract.pdf_generated_at,
        )
    )


def _render_document_contents(contract, version):
    try:
        return {
            locale: render_contract_pdf(contract, locale, pdf_version=version)
            for locale in SUPPORTED_CONTRACT_PDF_LOCALES
        }
    except CSAContractPDFError:
        raise
    except Exception:
        raise CSAContractPDFError("CSA contract PDF rendering failed") from None


def _save_documents(*, contract, storage, version, preserve_original):
    contents = _render_document_contents(contract, version)
    saved = {}
    try:
        for locale in SUPPORTED_CONTRACT_PDF_LOCALES:
            key = _storage_key(contract, locale, version)
            saved[locale] = storage.save(key, ContentFile(contents[locale]))
        generated_at = timezone.now()
        values = {
            "pdf_vi_storage_key": saved["vi"],
            "pdf_en_storage_key": saved["en"],
            "pdf_vi_sha256": _sha256(contents["vi"]),
            "pdf_en_sha256": _sha256(contents["en"]),
            "pdf_generated_at": generated_at,
            "pdf_version": version,
            "updated_at": generated_at,
        }
        if not preserve_original:
            values.update(
                {
                    "original_pdf_vi_storage_key": saved["vi"],
                    "original_pdf_en_storage_key": saved["en"],
                    "original_pdf_vi_sha256": values["pdf_vi_sha256"],
                    "original_pdf_en_sha256": values["pdf_en_sha256"],
                }
            )
        updated = CSAContract.objects.filter(pk=contract.pk).update(**values)
        if updated != 1:
            raise CSAContractPDFStorageError("CSA contract PDF metadata could not be saved")
    except CSAContractPDFError:
        _delete_quietly(storage, saved.values())
        raise
    except Exception:
        _delete_quietly(storage, saved.values())
        raise CSAContractPDFStorageError(
            "CSA contract PDF generation could not be completed"
        ) from None
    contract.refresh_from_db()
    return _documents(contract)


def _stored_current_documents_match(contract, storage, expected_contents):
    for locale, key, stored_hash in (
        ("vi", contract.pdf_vi_storage_key, contract.pdf_vi_sha256),
        ("en", contract.pdf_en_storage_key, contract.pdf_en_sha256),
    ):
        if not storage.exists(key):
            return False
        try:
            with storage.open(key, "rb") as stored:
                actual_hash = _sha256(stored.read())
        except Exception:
            return False
        expected_hash = _sha256(expected_contents[locale])
        if actual_hash != stored_hash or stored_hash != expected_hash:
            return False
    return True


@transaction.atomic
def generate_contract_documents(contract: CSAContract, *, storage=None):
    locked = CSAContract.objects.select_for_update().get(pk=contract.pk)
    if locked.status != CSAContractStatus.ACTIVE:
        raise CSAContractPDFStateError("Only an active contract can receive active PDFs")
    storage = storage or get_contract_pdf_storage()
    if _metadata_is_complete(locked):
        if storage.exists(locked.pdf_vi_storage_key) and storage.exists(locked.pdf_en_storage_key):
            return _documents(locked)
        raise CSAContractPDFStorageError("CSA contract PDF storage is incomplete")
    return _save_documents(
        contract=locked,
        storage=storage,
        version=1,
        preserve_original=False,
    )


@transaction.atomic
def generate_revoked_contract_documents(contract: CSAContract, *, storage=None):
    locked = CSAContract.objects.select_for_update().get(pk=contract.pk)
    if locked.status != CSAContractStatus.REVOKED:
        raise CSAContractPDFStateError("Only a revoked contract can receive revoked PDFs")
    storage = storage or get_contract_pdf_storage()
    if not _metadata_is_complete(locked):
        raise CSAContractPDFSnapshotIncomplete(
            "The original CSA contract PDFs must exist before revocation"
        )
    if locked.pdf_version > 1:
        expected_contents = _render_document_contents(locked, locked.pdf_version)
        if _stored_current_documents_match(locked, storage, expected_contents):
            return _documents(locked)
    if not storage.exists(locked.original_pdf_vi_storage_key) or not storage.exists(
        locked.original_pdf_en_storage_key
    ):
        raise CSAContractPDFStorageError("Original CSA contract PDF storage is incomplete")
    return _save_documents(
        contract=locked,
        storage=storage,
        version=locked.pdf_version + 1,
        preserve_original=True,
    )
