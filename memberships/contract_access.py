import hashlib
import logging
import re
import secrets
from pathlib import PurePosixPath

from memberships.contract_pdfs import get_contract_pdf_storage
from memberships.models import CSAContract

logger = logging.getLogger(__name__)
CONTRACT_LOCALES = ("vi", "en")
STORAGE_PREFIX_PATTERN = re.compile(r"[0-9a-f]{32}")


class CSAContractAccessError(ValueError):
    code = "csa_contract_unavailable"


class InvalidContractLocale(CSAContractAccessError):
    code = "invalid_contract_locale"


class CSAContractPDFUnavailable(CSAContractAccessError):
    code = "csa_contract_pdf_unavailable"


def contract_queryset():
    return CSAContract.objects.select_related("membership__customer", "purchase_request")


def validated_contract_locale(value) -> str:
    locale = str(value or "vi").strip()
    if locale not in CONTRACT_LOCALES:
        raise InvalidContractLocale("Contract locale must be vi or en")
    return locale


def contract_summary(contract: CSAContract | None):
    if contract is None:
        return None
    return {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "status": contract.status,
    }


def contract_payload(contract: CSAContract, *, admin: bool) -> dict:
    data = {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "source": contract.source,
        "status": contract.status,
        "membership_id": str(contract.membership_id),
        "customer_id": str(contract.membership.customer_id),
        "package_name_snapshot": contract.package_name_snapshot,
        "price_option_name_snapshot": contract.price_option_name_snapshot,
        "amount_snapshot": str(contract.amount_snapshot),
        "currency_snapshot": contract.currency_snapshot,
        "duration_months_snapshot": contract.duration_months_snapshot,
        "start_date": contract.start_date.isoformat(),
        "end_date": contract.end_date.isoformat(),
        "issued_at": contract.issued_at.isoformat(),
        "available_locales": [
            locale
            for locale in CONTRACT_LOCALES
            if getattr(contract, f"pdf_{locale}_storage_key")
            and getattr(contract, f"pdf_{locale}_sha256")
        ],
        "pdf_generated_at": (
            contract.pdf_generated_at.isoformat() if contract.pdf_generated_at else None
        ),
        "revoked_at": contract.revoked_at.isoformat() if contract.revoked_at else None,
        "revocation_reason": contract.revocation_reason,
        "terms_version": contract.terms_version,
        "terms_locale": contract.terms_locale,
        "terms_accepted_at": contract.terms_accepted_at.isoformat(),
        "terms_acceptance_method": contract.terms_acceptance_method,
    }
    if admin:
        data.update(
            {
                "purchase_request_id": (
                    str(contract.purchase_request_id) if contract.purchase_request_id else None
                ),
                "pdf_version": contract.pdf_version,
                "revoked_by_name_snapshot": contract.revoked_by_name_snapshot,
            }
        )
    return data


def _safe_reference_code(reference_code: str) -> str:
    return re.sub(r"[^A-Z0-9-]", "-", reference_code.upper())


def _current_pdf_metadata(contract: CSAContract, locale: str) -> tuple[str, str]:
    key = getattr(contract, f"pdf_{locale}_storage_key")
    expected_hash = getattr(contract, f"pdf_{locale}_sha256")
    if not key or not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash or "")):
        raise CSAContractPDFUnavailable("Contract PDF metadata is unavailable")
    path = PurePosixPath(key)
    expected_name = (
        f"{_safe_reference_code(contract.reference_code)}.v{contract.pdf_version}.{locale}.pdf"
    )
    if (
        path.is_absolute()
        or len(path.parts) != 2
        or not STORAGE_PREFIX_PATTERN.fullmatch(path.parts[0])
        or path.name != expected_name
    ):
        raise CSAContractPDFUnavailable("Contract PDF metadata is invalid")
    return key, expected_hash


def open_verified_current_contract_pdf(contract: CSAContract, *, locale: str):
    locale = validated_contract_locale(locale)
    key, expected_hash = _current_pdf_metadata(contract, locale)
    storage = get_contract_pdf_storage()
    opened = None
    try:
        if not storage.exists(key):
            raise CSAContractPDFUnavailable("Contract PDF file is unavailable")
        opened = storage.open(key, "rb")
        digest = hashlib.sha256()
        for chunk in iter(lambda: opened.read(64 * 1024), b""):
            digest.update(chunk)
        if not secrets.compare_digest(expected_hash, digest.hexdigest()):
            logger.warning(
                "CSA contract PDF integrity check failed",
                extra={"contract_id": str(contract.pk), "locale": locale},
            )
            raise CSAContractPDFUnavailable("Contract PDF integrity check failed")
        opened.seek(0)
        return opened
    except CSAContractPDFUnavailable:
        if opened is not None:
            opened.close()
        raise
    except Exception:
        if opened is not None:
            opened.close()
        logger.warning(
            "CSA contract PDF storage is unavailable",
            extra={"contract_id": str(contract.pk), "locale": locale},
        )
        raise CSAContractPDFUnavailable("Contract PDF is unavailable") from None


def contract_download_filename(contract: CSAContract, locale: str) -> str:
    return f"{_safe_reference_code(contract.reference_code)}.{locale}.pdf"
