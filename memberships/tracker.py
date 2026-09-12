import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from memberships.contract_access import contract_summary
from memberships.models import (
    CSAContract,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    CSATrackerSession,
)

TRACKER_COOKIE_NAME = "nfv_csa_tracker"
TRACKER_COOKIE_PATH = "/api/v1/public/csa-contract-tracker"
TRACKER_SESSION_TTL_SECONDS = 10 * 60
TRACKER_TOKEN_RETRIES = 4


class CSATrackerError(ValueError):
    code = "csa_tracker_unavailable"


class CSATrackerLookupUnavailable(CSATrackerError):
    code = "csa_tracker_lookup_unavailable"


class CSATrackerSessionExpired(CSATrackerError):
    code = "csa_tracker_session_expired"


class CSATrackerContractUnavailable(CSATrackerError):
    code = "csa_tracker_contract_unavailable"


def tracker_session_ttl() -> int:
    configured = int(
        getattr(settings, "CSA_TRACKER_SESSION_TTL_SECONDS", TRACKER_SESSION_TTL_SECONDS)
    )
    return max(1, min(configured, TRACKER_SESSION_TTL_SECONDS))


def _tracker_token_hash(token: str) -> str:
    return hmac.new(
        settings.SECRET_KEY.encode(),
        token.encode(),
        hashlib.sha256,
    ).hexdigest()


@transaction.atomic
def create_tracker_session(*, request_id, previous_token=None) -> str:
    CSATrackerSession.objects.filter(expires_at__lte=timezone.now()).delete()
    if previous_token:
        CSATrackerSession.objects.filter(
            token_hash=_tracker_token_hash(str(previous_token))
        ).delete()
    expires_at = timezone.now() + timedelta(seconds=tracker_session_ttl())
    for _attempt in range(TRACKER_TOKEN_RETRIES):
        token = secrets.token_urlsafe(32)
        try:
            with transaction.atomic():
                CSATrackerSession.objects.create(
                    token_hash=_tracker_token_hash(token),
                    purchase_request_id=request_id,
                    expires_at=expires_at,
                )
            return token
        except IntegrityError:
            continue
    raise CSATrackerSessionExpired("Tracker session could not be created")


def resolve_tracker_request(token) -> CSAPurchaseRequest:
    token = str(token or "")
    if not token or len(token) > 128:
        raise CSATrackerSessionExpired("Tracker session is unavailable")
    token_hash = _tracker_token_hash(token)
    session = (
        CSATrackerSession.objects.select_related(
            "purchase_request__membership",
            "purchase_request__csa_contract",
            "purchase_request__csa_contract__membership",
        )
        .filter(token_hash=token_hash, expires_at__gt=timezone.now())
        .first()
    )
    if session is None:
        CSATrackerSession.objects.filter(token_hash=token_hash).delete()
        raise CSATrackerSessionExpired("Tracker session is unavailable")
    return session.purchase_request


def lookup_tracker_request(*, reference_code: str, phone_e164: str) -> CSAPurchaseRequest:
    item = (
        CSAPurchaseRequest.objects.select_related(
            "membership",
            "csa_contract",
            "csa_contract__membership",
        )
        .filter(request_code=reference_code, phone_e164=phone_e164)
        .first()
    )
    if item is None:
        raise CSATrackerLookupUnavailable("Tracker lookup is unavailable")
    from memberships.expiry import expire_csa_purchase_request_if_due

    if expire_csa_purchase_request_if_due(request_id=item.pk):
        item.refresh_from_db()
    if item.status == CSAPurchaseRequestStatus.APPROVED:
        try:
            contract = item.csa_contract
        except CSAContract.DoesNotExist as error:
            raise CSATrackerLookupUnavailable("Tracker lookup is unavailable") from error
        if not item.membership_id or contract.membership_id != item.membership_id:
            raise CSATrackerLookupUnavailable("Tracker lookup is unavailable")
    return item


def _available_pdf_locales(contract: CSAContract) -> list[str]:
    return [
        locale
        for locale in ("vi", "en")
        if getattr(contract, f"pdf_{locale}_storage_key")
        and getattr(contract, f"pdf_{locale}_sha256")
    ]


def tracker_request_payload(item: CSAPurchaseRequest) -> dict:
    data = {
        "reference_code": item.request_code,
        "reference_type": "request",
        "status": item.status,
        "package_snapshot": {"name": item.package_name_snapshot},
        "price_option_snapshot": {"name": item.price_option_name_snapshot},
        "duration_months": item.duration_months_snapshot,
        "amount": str(item.amount_snapshot),
        "currency": item.currency_snapshot,
        "created_at": item.created_at.isoformat(),
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "payment_confirmed_at": (
            item.payment_confirmed_at.isoformat() if item.payment_confirmed_at else None
        ),
        "contract": None,
        "pdf_available": False,
    }
    if item.status == CSAPurchaseRequestStatus.APPROVED:
        contract = item.csa_contract
        locales = _available_pdf_locales(contract)
        data.update(
            {
                "reference_type": "contract",
                "membership_start_date": item.membership.start_date.isoformat(),
                "membership_end_date": item.membership.end_date.isoformat(),
                "contract": contract_summary(contract),
                "contract_status": contract.status,
                "available_pdf_locales": locales,
                "pdf_available": bool(locales),
                "pdf_action": "/api/v1/public/csa-contract-tracker/contract/pdf",
            }
        )
    elif item.status == CSAPurchaseRequestStatus.REJECTED:
        data.update(
            {
                "rejection_reason": item.rejection_reason,
                "rejected_at": item.rejected_at.isoformat() if item.rejected_at else None,
                "refund_status": item.refund_status,
            }
        )
    return data


def tracker_contract(item: CSAPurchaseRequest) -> CSAContract:
    if item.status != CSAPurchaseRequestStatus.APPROVED or not item.membership_id:
        raise CSATrackerContractUnavailable("Contract is unavailable")
    try:
        contract = item.csa_contract
    except CSAContract.DoesNotExist as error:
        raise CSATrackerContractUnavailable("Contract is unavailable") from error
    if contract.purchase_request_id != item.pk or contract.membership_id != item.membership_id:
        raise CSATrackerContractUnavailable("Contract is unavailable")
    return contract


def tracker_contract_payload(contract: CSAContract) -> dict:
    locales = _available_pdf_locales(contract)
    return {
        "id": str(contract.pk),
        "reference_code": contract.reference_code,
        "source": contract.source,
        "status": contract.status,
        "package_name_snapshot": contract.package_name_snapshot,
        "price_option_name_snapshot": contract.price_option_name_snapshot,
        "amount_snapshot": str(contract.amount_snapshot),
        "currency_snapshot": contract.currency_snapshot,
        "duration_months_snapshot": contract.duration_months_snapshot,
        "start_date": contract.start_date.isoformat(),
        "end_date": contract.end_date.isoformat(),
        "issued_at": contract.issued_at.isoformat(),
        "revoked_at": contract.revoked_at.isoformat() if contract.revoked_at else None,
        "revocation_reason": contract.revocation_reason,
        "terms_version": contract.terms_version,
        "terms_locale": contract.terms_locale,
        "terms_accepted_at": contract.terms_accepted_at.isoformat(),
        "terms_acceptance_method": contract.terms_acceptance_method,
        "available_pdf_locales": locales,
        "pdf_available": bool(locales),
        "pdf_action": "/api/v1/public/csa-contract-tracker/contract/pdf",
    }
