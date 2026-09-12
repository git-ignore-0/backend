from django.core.exceptions import ValidationError

from memberships.models import CSAContract, CSAContractStatus


class CSAContractVerificationUnavailable(ValueError):
    code = "csa_contract_verification_unavailable"


def verify_csa_contract(*, reference_code: str) -> CSAContract:
    try:
        contract = CSAContract.objects.select_related(
            "membership",
            "purchase_request",
        ).get(reference_code=reference_code)
    except CSAContract.DoesNotExist as error:
        raise CSAContractVerificationUnavailable("Contract verification is unavailable") from error

    try:
        contract.full_clean(validate_unique=False, validate_constraints=False)
    except (ValidationError, ValueError, TypeError) as error:
        raise CSAContractVerificationUnavailable("Contract verification is unavailable") from error
    if contract.status not in {CSAContractStatus.ACTIVE, CSAContractStatus.REVOKED}:
        raise CSAContractVerificationUnavailable("Contract verification is unavailable")
    return contract


def csa_contract_verification_payload(contract: CSAContract) -> dict:
    return {
        "reference_code": contract.reference_code,
        "status": contract.status,
        "issued_at": contract.issued_at.isoformat(),
        "start_date": contract.start_date.isoformat(),
        "end_date": contract.end_date.isoformat(),
        "revoked_at": contract.revoked_at.isoformat() if contract.revoked_at else None,
        "revocation_reason": contract.revocation_reason or None,
    }
