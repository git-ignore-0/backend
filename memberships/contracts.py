import secrets
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from memberships.contract_provider import CURRENT_NFV_PROVIDER_VERSION
from memberships.models import (
    CSAContract,
    CSAContractSource,
    CSAContractTermsAcceptanceMethod,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    Membership,
    MembershipContentLocale,
    MembershipPaymentSettings,
)
from memberships.terms import (
    InvalidCSATerms,
    get_csa_terms,
    get_current_csa_terms,
    validate_csa_terms_snapshot,
)

CONTRACT_REFERENCE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CONTRACT_REFERENCE_RETRIES = 8


class CSAContractError(ValueError):
    code = "invalid_csa_contract"


class CSAContractConflict(CSAContractError):
    code = "csa_contract_exists"


class CSAContractReferenceConflict(CSAContractError):
    code = "csa_contract_reference_conflict"


class CSAContractPurchaseInvalid(CSAContractError):
    code = "csa_contract_purchase_invalid"


class CSAContractSnapshotInvalid(CSAContractError):
    code = "csa_contract_snapshot_invalid"


class CSAContractTermsSnapshotIncomplete(CSAContractError):
    code = "csa_contract_terms_snapshot_incomplete"


@dataclass(frozen=True)
class CSAContractSnapshot:
    member_name: str
    phone_e164: str
    email: str | None
    zalo_whatsapp: str | None
    province_code: str
    ward_code: str
    address: str
    package_name: str
    price_option_name: str
    duration_months: int
    amount: int
    currency: str
    quota: Any | None
    terms_version: str
    terms_locale: str
    terms: Any
    terms_accepted_at: datetime
    terms_acceptance_method: str
    terms_acceptance_ip: str | None
    terms_acceptance_user_agent: str | None
    start_date: date
    end_date: date
    payment: Any | None = None
    terms_vi: Any | None = None
    terms_en: Any | None = None
    provider_version: str = CURRENT_NFV_PROVIDER_VERSION


def _quota_snapshot(membership: Membership) -> dict:
    return {
        "policy": membership.quota_policy_snapshot,
        "items": [
            {
                "product_id": str(item.product_id),
                "product_name": item.product_name,
                "product_name_vi": item.product_name_vi_snapshot,
                "product_name_en": item.product_name_en_snapshot,
                "unit_size": str(item.unit_size),
                "unit_label_vi": item.unit_label_vi,
                "unit_label_en": item.unit_label_en,
                "quota_units_per_cycle": item.quota_units_per_cycle,
            }
            for item in membership.product_snapshots.select_related("product").order_by("id")
        ],
    }


def _bilingual_terms(*, version: str) -> tuple[dict, dict]:
    try:
        vi = get_csa_terms(version=version, locale=MembershipContentLocale.VI)["content"]
        en = get_csa_terms(version=version, locale=MembershipContentLocale.EN)["content"]
    except InvalidCSATerms as error:
        raise CSAContractTermsSnapshotIncomplete(
            "Both localized CSA terms snapshots must be available"
        ) from error
    return vi, en


def _current_bilingual_terms() -> tuple[str, dict, dict]:
    try:
        current_vi = get_current_csa_terms(MembershipContentLocale.VI)
        version = current_vi["version"]
        terms_en = get_csa_terms(version=version, locale=MembershipContentLocale.EN)["content"]
    except InvalidCSATerms as error:
        raise CSAContractTermsSnapshotIncomplete(
            "Both localized CSA terms snapshots must be available"
        ) from error
    return version, current_vi["content"], terms_en


def build_online_contract_snapshot(
    *, purchase_request: CSAPurchaseRequest, membership: Membership
) -> CSAContractSnapshot:
    if not all(
        (
            purchase_request.terms_version,
            purchase_request.terms_locale,
            purchase_request.terms_snapshot,
            purchase_request.terms_hash,
            purchase_request.terms_accepted_at,
        )
    ):
        raise CSAContractTermsSnapshotIncomplete(
            "Purchase request terms acceptance snapshot is incomplete"
        )
    try:
        validate_csa_terms_snapshot(
            version=purchase_request.terms_version,
            locale=purchase_request.terms_locale,
            content=purchase_request.terms_snapshot,
            content_hash=purchase_request.terms_hash,
        )
    except InvalidCSATerms as error:
        raise CSAContractTermsSnapshotIncomplete(
            "Purchase request terms acceptance snapshot is invalid"
        ) from error
    terms_vi, terms_en = _bilingual_terms(version=purchase_request.terms_version)
    if purchase_request.terms_locale == MembershipContentLocale.VI:
        terms_vi = purchase_request.terms_snapshot
    else:
        terms_en = purchase_request.terms_snapshot
    return CSAContractSnapshot(
        member_name=purchase_request.name_snapshot,
        phone_e164=purchase_request.phone_e164,
        email=purchase_request.email_snapshot,
        zalo_whatsapp=purchase_request.zalo_whatsapp_snapshot,
        province_code=purchase_request.province_code_snapshot,
        ward_code=purchase_request.ward_code_snapshot,
        address=purchase_request.address_snapshot,
        package_name=purchase_request.package_name_snapshot,
        price_option_name=purchase_request.price_option_name_snapshot,
        duration_months=purchase_request.duration_months_snapshot,
        amount=purchase_request.amount_snapshot,
        currency=purchase_request.currency_snapshot,
        quota=_quota_snapshot(membership),
        payment={
            "bank_bin": purchase_request.bank_bin_snapshot,
            "bank_name": purchase_request.bank_name_snapshot,
            "account_number": purchase_request.account_number_snapshot,
            "account_name": purchase_request.account_name_snapshot,
            "transfer_content": purchase_request.transfer_content,
            "payment_config_version": purchase_request.payment_config_version,
        },
        terms_version=purchase_request.terms_version,
        terms_locale=purchase_request.terms_locale,
        terms=purchase_request.terms_snapshot,
        terms_vi=terms_vi,
        terms_en=terms_en,
        terms_accepted_at=purchase_request.terms_accepted_at,
        terms_acceptance_method=CSAContractTermsAcceptanceMethod.ONLINE,
        terms_acceptance_ip=purchase_request.terms_acceptance_ip,
        terms_acceptance_user_agent=purchase_request.terms_acceptance_user_agent,
        start_date=membership.start_date,
        end_date=membership.end_date,
    )


def build_manual_contract_snapshot(
    *, customer, membership: Membership, payment_settings: MembershipPaymentSettings
) -> CSAContractSnapshot:
    terms_version, terms_vi, terms_en = _current_bilingual_terms()
    accepted_at = timezone.now()
    user = customer.user if customer.user_id else None
    return CSAContractSnapshot(
        member_name=customer.name,
        phone_e164=customer.phone_e164,
        email=user.email if user else None,
        zalo_whatsapp=None,
        province_code=customer.province_code,
        ward_code=customer.ward_code,
        address=customer.address,
        package_name=membership.package_name_snapshot,
        price_option_name=f"{membership.duration_months_snapshot} months",
        duration_months=membership.duration_months_snapshot,
        amount=membership.total_price_vnd_snapshot,
        currency="VND",
        quota=_quota_snapshot(membership),
        payment={
            "bank_bin": payment_settings.bank_bin,
            "bank_name": payment_settings.bank_name,
            "account_number": payment_settings.account_number,
            "account_name": payment_settings.account_name,
            "transfer_content": membership.farmbrite_order_reference,
            "payment_config_version": payment_settings.version,
        },
        terms_version=terms_version,
        terms_locale=MembershipContentLocale.VI,
        terms=terms_vi,
        terms_vi=terms_vi,
        terms_en=terms_en,
        terms_accepted_at=accepted_at,
        terms_acceptance_method=CSAContractTermsAcceptanceMethod.IN_STORE,
        terms_acceptance_ip=None,
        terms_acceptance_user_agent=None,
        start_date=membership.start_date,
        end_date=membership.end_date,
    )


def generate_manual_contract_reference(*, issued_at: datetime | None = None) -> str:
    issued_at = issued_at or timezone.now()
    local_issued_at = timezone.localtime(issued_at)
    suffix = "".join(secrets.choice(CONTRACT_REFERENCE_ALPHABET) for _ in range(6))
    return f"CSA-{local_issued_at:%Y%m}-{suffix}"


def _contract_values(*, snapshot: CSAContractSnapshot, issued_at: datetime) -> dict:
    return {
        "member_name_snapshot": snapshot.member_name,
        "phone_e164_snapshot": snapshot.phone_e164,
        "email_snapshot": snapshot.email,
        "zalo_whatsapp_snapshot": snapshot.zalo_whatsapp,
        "province_code_snapshot": snapshot.province_code,
        "ward_code_snapshot": snapshot.ward_code,
        "address_snapshot": snapshot.address,
        "package_name_snapshot": snapshot.package_name,
        "price_option_name_snapshot": snapshot.price_option_name,
        "duration_months_snapshot": snapshot.duration_months,
        "amount_snapshot": snapshot.amount,
        "currency_snapshot": snapshot.currency,
        "quota_snapshot": snapshot.quota,
        "payment_snapshot": snapshot.payment,
        "terms_version": snapshot.terms_version,
        "terms_locale": snapshot.terms_locale,
        "terms_snapshot": snapshot.terms,
        "terms_accepted_at": snapshot.terms_accepted_at,
        "terms_acceptance_method": snapshot.terms_acceptance_method,
        "terms_acceptance_ip": snapshot.terms_acceptance_ip,
        "terms_acceptance_user_agent": snapshot.terms_acceptance_user_agent,
        "start_date": snapshot.start_date,
        "end_date": snapshot.end_date,
        "issued_at": issued_at,
        "provider_version": snapshot.provider_version,
        "terms_snapshot_vi": (
            snapshot.terms_vi
            if snapshot.terms_vi is not None
            else snapshot.terms
            if snapshot.terms_locale == "vi"
            else None
        ),
        "terms_snapshot_en": (
            snapshot.terms_en
            if snapshot.terms_en is not None
            else snapshot.terms
            if snapshot.terms_locale == "en"
            else None
        ),
    }


def _validate_membership_snapshot(*, membership: Membership, snapshot: CSAContractSnapshot):
    if (
        snapshot.package_name != membership.package_name_snapshot
        or snapshot.duration_months != membership.duration_months_snapshot
        or snapshot.amount != membership.total_price_vnd_snapshot
        or snapshot.start_date != membership.start_date
        or snapshot.end_date != membership.end_date
    ):
        raise CSAContractSnapshotInvalid(
            "Contract package, price, or dates do not match the Membership snapshot"
        )


def _validate_online_snapshot(
    *, purchase_request: CSAPurchaseRequest, snapshot: CSAContractSnapshot
):
    if purchase_request.status == CSAPurchaseRequestStatus.REJECTED:
        raise CSAContractPurchaseInvalid("Rejected purchase requests cannot create contracts")
    if purchase_request.status != CSAPurchaseRequestStatus.APPROVED:
        raise CSAContractPurchaseInvalid("Purchase request must be approved")
    expected = (
        purchase_request.name_snapshot,
        purchase_request.phone_e164,
        purchase_request.province_code_snapshot,
        purchase_request.ward_code_snapshot,
        purchase_request.address_snapshot,
        purchase_request.package_name_snapshot,
        purchase_request.price_option_name_snapshot,
        purchase_request.duration_months_snapshot,
        purchase_request.amount_snapshot,
        purchase_request.currency_snapshot,
    )
    actual = (
        snapshot.member_name,
        snapshot.phone_e164,
        snapshot.province_code,
        snapshot.ward_code,
        snapshot.address,
        snapshot.package_name,
        snapshot.price_option_name,
        snapshot.duration_months,
        snapshot.amount,
        snapshot.currency,
    )
    if actual != expected:
        raise CSAContractSnapshotInvalid(
            "Online contract snapshot does not match the purchase request snapshot"
        )
    if snapshot.terms_acceptance_method != CSAContractTermsAcceptanceMethod.ONLINE:
        raise CSAContractSnapshotInvalid("Online contract requires online terms acceptance")


def _map_integrity_error(*, membership_id, purchase_request_id=None, reference_code=None):
    if CSAContract.objects.filter(membership_id=membership_id).exists():
        raise CSAContractConflict("Membership already has a CSA contract")
    if (
        purchase_request_id
        and CSAContract.objects.filter(purchase_request_id=purchase_request_id).exists()
    ):
        raise CSAContractConflict("Purchase request already has a CSA contract")
    if reference_code and CSAContract.objects.filter(reference_code=reference_code).exists():
        raise CSAContractReferenceConflict("CSA contract reference already exists")


@transaction.atomic
def create_online_contract(
    *, membership_id, purchase_request_id, snapshot: CSAContractSnapshot
) -> CSAContract:
    membership = Membership.objects.select_for_update().get(pk=membership_id)
    purchase_request = CSAPurchaseRequest.objects.select_for_update().get(pk=purchase_request_id)
    if (
        CSAContract.objects.filter(membership=membership).exists()
        or CSAContract.objects.filter(purchase_request=purchase_request).exists()
    ):
        raise CSAContractConflict("Membership or purchase request already has a CSA contract")
    _validate_membership_snapshot(membership=membership, snapshot=snapshot)
    _validate_online_snapshot(purchase_request=purchase_request, snapshot=snapshot)
    if purchase_request.membership_id != membership.pk:
        raise CSAContractPurchaseInvalid(
            "Purchase request does not belong to the supplied Membership"
        )
    issued_at = timezone.now()
    try:
        with transaction.atomic():
            return CSAContract.objects.create(
                reference_code=purchase_request.request_code,
                source=CSAContractSource.ONLINE,
                membership=membership,
                purchase_request=purchase_request,
                **_contract_values(snapshot=snapshot, issued_at=issued_at),
            )
    except ValidationError as error:
        raise CSAContractSnapshotInvalid("CSA contract snapshot is invalid") from error
    except IntegrityError as error:
        _map_integrity_error(
            membership_id=membership.pk,
            purchase_request_id=purchase_request.pk,
            reference_code=purchase_request.request_code,
        )
        raise CSAContractConflict("CSA contract could not be created") from error


@transaction.atomic
def create_manual_contract(*, membership_id, snapshot: CSAContractSnapshot) -> CSAContract:
    membership = Membership.objects.select_for_update().get(pk=membership_id)
    if CSAContract.objects.filter(membership=membership).exists():
        raise CSAContractConflict("Membership already has a CSA contract")
    _validate_membership_snapshot(membership=membership, snapshot=snapshot)
    if snapshot.terms_acceptance_method != CSAContractTermsAcceptanceMethod.IN_STORE:
        raise CSAContractSnapshotInvalid("Manual contract requires in-store terms acceptance")
    issued_at = timezone.now()
    for _attempt in range(CONTRACT_REFERENCE_RETRIES):
        reference_code = generate_manual_contract_reference(issued_at=issued_at)
        try:
            with transaction.atomic():
                return CSAContract.objects.create(
                    reference_code=reference_code,
                    source=CSAContractSource.ADMIN_MANUAL,
                    membership=membership,
                    purchase_request=None,
                    **_contract_values(snapshot=snapshot, issued_at=issued_at),
                )
        except ValidationError as error:
            raise CSAContractSnapshotInvalid("CSA contract snapshot is invalid") from error
        except IntegrityError as error:
            if CSAContract.objects.filter(reference_code=reference_code).exists():
                continue
            _map_integrity_error(membership_id=membership.pk)
            raise CSAContractConflict("CSA contract could not be created") from error
    raise CSAContractReferenceConflict("Could not allocate a unique CSA contract reference")
