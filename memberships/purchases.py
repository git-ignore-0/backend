import hashlib
import hmac
import json
import secrets
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from identity.models import AuditLog, User
from members.administrative_units import province_exists, ward_choices
from members.models import MemberProfile
from memberships.contract_pdfs import (
    CSAContractPDFStorageError,
    delete_contract_documents,
    get_contract_pdf_storage,
)
from memberships.contract_versions import issue_contract_version
from memberships.contracts import build_online_contract_snapshot, create_online_contract
from memberships.expiry import (
    expire_csa_purchase_request_if_due,
    expire_open_csa_purchase_request_for_phone,
    purchase_request_expires_at,
)
from memberships.models import (
    CSAContract,
    CSAContractVersionReason,
    CSACustomer,
    CSAPurchasePayment,
    CSAPurchasePaymentStatus,
    CSAPurchaseRefundStatus,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    Membership,
    MembershipPackage,
    MembershipPackagePriceOption,
    MembershipPaymentPlan,
    MembershipPaymentPlanInstallment,
    MembershipPaymentSettings,
    MembershipStatus,
    generate_csa_purchase_request_code,
)
from memberships.payment_plans import (
    PaymentPlanValidationError,
    validate_payment_plan_configuration,
)
from memberships.payments import (
    CSAPaymentError,
    confirm_locked_csa_payment,
    ensure_csa_payment_schedule,
    set_csa_payment_due_dates,
)
from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone
from memberships.services import (
    CSACustomerError,
    MembershipOverlap,
    add_months,
    assign_membership,
    create_csa_customer_for_user,
    create_managed_csa_customer,
    ensure_csa_customer_complete,
    link_csa_customer_to_user,
    update_csa_customer,
)
from memberships.terms import InvalidCSATerms, get_csa_terms, get_current_csa_terms


class CSAPurchaseError(ValueError):
    code = "invalid_csa_purchase_request"


class PurchaseRequestConflict(CSAPurchaseError):
    code = "csa_purchase_request_open_exists"


class PurchasePackageUnavailable(CSAPurchaseError):
    code = "csa_purchase_package_unavailable"


class PurchasePaymentPlanRequired(CSAPurchaseError):
    code = "csa_purchase_payment_plan_required"


class PurchasePaymentPlanUnavailable(CSAPurchaseError):
    code = "csa_purchase_payment_plan_unavailable"


class PaymentSettingsUnavailable(CSAPurchaseError):
    code = "csa_purchase_payment_unavailable"


class PurchaseAddressInvalid(CSAPurchaseError):
    code = "invalid_csa_purchase_address"


class AuthAccountIncomplete(CSAPurchaseError):
    code = "csa_purchase_auth_account_incomplete"
    client_message = (
        "Please update your name, phone number, province, ward, and address in Auth Account "
        "before purchasing a CSA package."
    )


class PurchaseConfirmationDenied(CSAPurchaseError):
    code = "csa_purchase_request_not_found"


class PurchaseTransitionConflict(CSAPurchaseError):
    code = "csa_purchase_request_not_pending"


class PurchaseRequestExpired(CSAPurchaseError):
    code = "csa_purchase_request_expired"


class PaymentQuoteUnavailable(CSAPurchaseError):
    code = "csa_payment_quote_unavailable"


class PaymentQuoteExpired(CSAPurchaseError):
    code = "csa_payment_quote_expired"


class PaymentQuoteIdentityMismatch(CSAPurchaseError):
    code = "csa_payment_quote_identity_mismatch"


class PurchaseAdminNotFound(CSAPurchaseError):
    code = "csa_purchase_request_not_found"


class PurchaseAdminWorkflowConflict(CSAPurchaseError):
    code = "csa_purchase_request_state_conflict"


class PurchaseCustomerResolutionRequired(CSAPurchaseError):
    code = "csa_purchase_customer_resolution_required"

    def __init__(self, message, *, context=None):
        super().__init__(message)
        self.context = context


class PurchaseCustomerConflict(CSAPurchaseError):
    code = "csa_purchase_customer_conflict"


class PurchaseCustomerIncomplete(CSAPurchaseError):
    code = "csa_purchase_customer_incomplete"


class PurchaseStartDateInvalid(CSAPurchaseError):
    code = "csa_purchase_start_date_before_next_available"


class PurchaseRefundConflict(CSAPurchaseError):
    code = "csa_purchase_refund_state_conflict"


DUMMY_GUEST_TOKEN_HASH = make_password(secrets.token_urlsafe(32))
CUSTOMER_RESOLUTIONS = {"keep_existing", "use_request_data", "reject_conflict"}
PAYMENT_QUOTE_TTL = timedelta(minutes=5)
PAYMENT_QUOTE_SALT = "memberships.csa-payment-quote.v1"


def _required_snapshot(value, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CSAPurchaseError(f"{label} is required")
    return normalized


def _validated_address(*, province_code, ward_code, address) -> tuple[str, str, str]:
    province_code = _required_snapshot(province_code, "Province")
    ward_code = _required_snapshot(ward_code, "Ward")
    address = _required_snapshot(address, "Address")
    if not province_exists(province_code):
        raise PurchaseAddressInvalid("Province code is invalid")
    valid_wards = {code for code, _name in ward_choices(province_code, "vi")}
    if ward_code not in valid_wards:
        raise PurchaseAddressInvalid("Ward code does not belong to province")
    return province_code, ward_code, address


def _locked_authenticated_identity(user) -> dict:
    try:
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        profile = MemberProfile.objects.select_for_update().get(user=locked_user)
    except (User.DoesNotExist, MemberProfile.DoesNotExist) as error:
        raise AuthAccountIncomplete("Auth Account profile is incomplete") from error

    values = {
        "name": str(locked_user.name or "").strip(),
        "phone": str(profile.phone_number or "").strip(),
        "province_code": str(profile.province_code or "").strip(),
        "ward_code": str(profile.ward_code or "").strip(),
        "address": str(profile.address or "").strip(),
        "email": str(locked_user.email or "").strip(),
    }
    if any(not value for value in values.values()):
        raise AuthAccountIncomplete("Auth Account profile is incomplete")
    try:
        values["phone"] = normalize_vietnam_phone(values["phone"])
        (
            values["province_code"],
            values["ward_code"],
            values["address"],
        ) = _validated_address(
            province_code=values["province_code"],
            ward_code=values["ward_code"],
            address=values["address"],
        )
    except (InvalidPhoneNumber, CSAPurchaseError) as error:
        raise AuthAccountIncomplete("Auth Account profile is incomplete") from error
    values["user"] = locked_user
    return values


@transaction.atomic
def update_membership_payment_settings(
    *,
    bank_bin,
    bank_name,
    account_number,
    account_name,
    direct_transfer_enabled,
    actor=None,
) -> MembershipPaymentSettings:
    values = {
        "bank_bin": str(bank_bin or "").strip(),
        "bank_name": str(bank_name or "").strip(),
        "account_number": str(account_number or "").strip(),
        "account_name": str(account_name or "").strip(),
        "direct_transfer_enabled": bool(direct_transfer_enabled),
        "updated_by": actor,
    }
    try:
        current = MembershipPaymentSettings.objects.select_for_update().get(pk="default")
    except MembershipPaymentSettings.DoesNotExist:
        try:
            with transaction.atomic():
                return MembershipPaymentSettings.objects.create(key="default", **values)
        except IntegrityError:
            current = MembershipPaymentSettings.objects.select_for_update().get(pk="default")

    for field, value in values.items():
        setattr(current, field, value)
    current.version += 1
    current.save()
    return current


def _locked_payment_settings() -> MembershipPaymentSettings:
    try:
        settings = MembershipPaymentSettings.objects.select_for_update().get(pk="default")
    except MembershipPaymentSettings.DoesNotExist as error:
        raise PaymentSettingsUnavailable("CSA payment settings are missing") from error
    if not settings.direct_transfer_enabled:
        raise PaymentSettingsUnavailable("Direct transfer is disabled")
    try:
        settings.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as error:
        raise PaymentSettingsUnavailable("CSA payment settings are invalid") from error
    return settings


@transaction.atomic
def _create_csa_purchase_request(
    *,
    package_id,
    price_option_id,
    payment_plan_id=None,
    phone=None,
    name=None,
    province_code=None,
    ward_code=None,
    address=None,
    email=None,
    zalo_whatsapp=None,
    terms_locale="vi",
    terms_acceptance_ip=None,
    terms_acceptance_user_agent=None,
    user=None,
    guest_token_hash="",
) -> CSAPurchaseRequest:
    if user is not None:
        identity = _locked_authenticated_identity(user)
        user = identity["user"]
        name = identity["name"]
        normalized_phone = identity["phone"]
        province_code = identity["province_code"]
        ward_code = identity["ward_code"]
        address = identity["address"]
        email = identity["email"]
    else:
        try:
            normalized_phone = normalize_vietnam_phone(str(phone or ""))
        except InvalidPhoneNumber as error:
            raise CSAPurchaseError(str(error)) from error
        name = _required_snapshot(name, "Name")
        province_code, ward_code, address = _validated_address(
            province_code=province_code,
            ward_code=ward_code,
            address=address,
        )
        email = str(email or "").strip() or None
    zalo_whatsapp = str(zalo_whatsapp or "").strip() or None
    terms = get_current_csa_terms(terms_locale)
    terms_accepted_at = timezone.now()
    try:
        package = MembershipPackage.objects.select_for_update().get(pk=package_id, is_active=True)
        option = MembershipPackagePriceOption.objects.select_for_update().get(
            pk=price_option_id,
            package=package,
            is_active=True,
        )
    except (MembershipPackage.DoesNotExist, MembershipPackagePriceOption.DoesNotExist) as error:
        raise PurchasePackageUnavailable("Package or price option is unavailable") from error

    plan = None
    plan_installments = []
    if payment_plan_id is not None:
        try:
            plan = MembershipPaymentPlan.objects.select_for_update().get(pk=payment_plan_id)
        except MembershipPaymentPlan.DoesNotExist as error:
            raise PurchasePaymentPlanUnavailable("Payment plan is unavailable") from error
        if plan.price_option_id != option.pk or not plan.is_active:
            raise PurchasePaymentPlanUnavailable("Payment plan is unavailable")
        plan_installments = list(
            MembershipPaymentPlanInstallment.objects.select_for_update()
            .filter(payment_plan=plan)
            .order_by("sequence", "id")
        )
        try:
            validate_payment_plan_configuration(plan, plan_installments)
        except PaymentPlanValidationError as error:
            raise PurchasePaymentPlanUnavailable("Payment plan is unavailable") from error
    elif MembershipPaymentPlan.objects.filter(price_option=option, is_active=True).exists():
        raise PurchasePaymentPlanRequired("Select a payment plan for this price option")

    plan_snapshot = (
        [
            {
                "sequence": row.sequence,
                "amount": format(row.amount, "f"),
                "cycle_count": row.cycle_count,
            }
            for row in plan_installments
        ]
        if plan is not None
        else None
    )

    expire_open_csa_purchase_request_for_phone(phone_e164=normalized_phone)
    if CSAPurchaseRequest.objects.filter(phone_e164=normalized_phone, open_slot=True).exists():
        raise PurchaseRequestConflict("Phone already has an open CSA purchase request")

    payment = _locked_payment_settings()
    request_code = generate_csa_purchase_request_code()
    item = CSAPurchaseRequest(
        user=user,
        request_code=request_code,
        phone_e164=normalized_phone,
        name_snapshot=name,
        email_snapshot=email,
        zalo_whatsapp_snapshot=zalo_whatsapp,
        province_code_snapshot=province_code,
        ward_code_snapshot=ward_code,
        address_snapshot=address,
        package=package,
        price_option=option,
        payment_plan=plan,
        package_name_snapshot=package.name,
        price_option_name_snapshot=f"{option.duration_months} months",
        duration_months_snapshot=option.duration_months,
        amount_snapshot=int(plan.total_amount) if plan is not None else option.total_price_vnd,
        price_option_monthly_amount_snapshot=option.monthly_price_vnd,
        payment_plan_name_snapshot=plan.name if plan is not None else None,
        payment_type_snapshot=plan.payment_type if plan is not None else None,
        total_payment_amount_snapshot=plan.total_amount if plan is not None else None,
        installment_count_snapshot=plan.installment_count if plan is not None else None,
        payment_installments_snapshot=plan_snapshot,
        currency_snapshot="VND",
        bank_bin_snapshot=payment.bank_bin,
        bank_name_snapshot=payment.bank_name,
        account_number_snapshot=payment.account_number,
        account_name_snapshot=payment.account_name,
        transfer_content=request_code,
        guest_token_hash=guest_token_hash,
        payment_config_version=payment.version,
        terms_version=terms["version"],
        terms_locale=terms["locale"],
        terms_snapshot=terms["content"],
        terms_hash=terms["hash"],
        terms_accepted_at=terms_accepted_at,
        terms_acceptance_ip=terms_acceptance_ip,
        terms_acceptance_user_agent=(str(terms_acceptance_user_agent or "")[:1000] or None),
        terms_acceptance_method="online",
        expires_at=purchase_request_expires_at(now=terms_accepted_at),
    )
    try:
        with transaction.atomic():
            item.save(force_insert=True)
    except IntegrityError as error:
        if CSAPurchaseRequest.objects.filter(phone_e164=normalized_phone, open_slot=True).exists():
            raise PurchaseRequestConflict(
                "Phone already has an open CSA purchase request"
            ) from error
        raise
    try:
        if item.payment_plan_id is not None:
            ensure_csa_payment_schedule(request_id=item.pk)
    except CSAPaymentError as error:
        raise CSAPurchaseError("Purchase payment schedule is invalid") from error
    return item


def _guest_confirmation_credentials() -> tuple[str, str]:
    raw_token = secrets.token_urlsafe(32)
    return raw_token, make_password(raw_token)


def create_csa_purchase_request(**kwargs) -> CSAPurchaseRequest:
    user = kwargs.get("user")
    guest_token_hash = ""
    if user is None:
        _raw_token, guest_token_hash = _guest_confirmation_credentials()
    return _create_csa_purchase_request(**kwargs, guest_token_hash=guest_token_hash)


def create_public_csa_purchase_request(**kwargs) -> tuple[CSAPurchaseRequest, str | None]:
    user = kwargs.get("user")
    raw_token = None
    guest_token_hash = ""
    if user is None:
        raw_token, guest_token_hash = _guest_confirmation_credentials()
    item = _create_csa_purchase_request(**kwargs, guest_token_hash=guest_token_hash)
    return item, raw_token


def _guest_identity(*, identity) -> dict:
    try:
        phone = normalize_vietnam_phone(str(identity.get("phone") or ""))
    except (AttributeError, InvalidPhoneNumber) as error:
        raise PurchaseAddressInvalid("Guest identity is invalid") from error
    province_code, ward_code, address = _validated_address(
        province_code=identity.get("province_code"),
        ward_code=identity.get("ward_code"),
        address=identity.get("address"),
    )
    return {
        "name": _required_snapshot(identity.get("name"), "Name"),
        "phone": phone,
        "province_code": province_code,
        "ward_code": ward_code,
        "address": address,
        "email": None,
        "user": None,
    }


def _guest_identity_fingerprint(identity: dict) -> str:
    canonical = json.dumps(
        {
            "address": identity["address"],
            "name": identity["name"],
            "phone": identity["phone"],
            "province_code": identity["province_code"],
            "ward_code": identity["ward_code"],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


def _quote_plan_snapshot(*, option, payment_plan_id):
    plan = None
    rows = []
    if payment_plan_id is not None:
        try:
            plan = MembershipPaymentPlan.objects.select_for_update().get(pk=payment_plan_id)
        except MembershipPaymentPlan.DoesNotExist as error:
            raise PurchasePaymentPlanUnavailable("Payment plan is unavailable") from error
        if plan.price_option_id != option.pk or not plan.is_active:
            raise PurchasePaymentPlanUnavailable("Payment plan is unavailable")
        rows = list(
            MembershipPaymentPlanInstallment.objects.select_for_update()
            .filter(payment_plan=plan)
            .order_by("sequence", "id")
        )
        try:
            validate_payment_plan_configuration(plan, rows)
        except PaymentPlanValidationError as error:
            raise PurchasePaymentPlanUnavailable("Payment plan is unavailable") from error
    elif MembershipPaymentPlan.objects.filter(price_option=option, is_active=True).exists():
        raise PurchasePaymentPlanRequired("Select a payment plan for this price option")

    if plan is None:
        total_amount = str(option.total_price_vnd)
        installments = [
            {
                "sequence": 1,
                "amount": total_amount,
                "cycle_count": option.duration_months,
            }
        ]
        return {
            "id": None,
            "name": None,
            "payment_type": "full",
            "total_amount": total_amount,
            "initial_payment_amount": total_amount,
            "installment_count": 1,
            "installments": installments,
        }
    installments = [
        {
            "sequence": row.sequence,
            "amount": format(row.amount, "f"),
            "cycle_count": row.cycle_count,
        }
        for row in rows
    ]
    return {
        "id": str(plan.pk),
        "name": plan.name,
        "payment_type": plan.payment_type,
        "total_amount": format(plan.total_amount, "f"),
        "initial_payment_amount": installments[0]["amount"],
        "installment_count": plan.installment_count,
        "installments": installments,
    }


def _quote_token(payload: dict) -> str:
    return signing.dumps(payload, salt=PAYMENT_QUOTE_SALT, compress=True)


def _quote_positive_amount(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Decimal(value) > 0 and Decimal(value) == Decimal(value).to_integral_value()
    except (InvalidOperation, ValueError):
        return False


def _validate_payment_quote(quote: dict, *, now) -> dict:
    """Validate every signed quote field before it is used to create a request."""
    if quote.get("v") != 1 or not isinstance(quote.get("nonce"), str) or len(quote["nonce"]) < 32:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if not isinstance(quote.get("request_code"), str) or not quote["request_code"].startswith(
        "CSA-"
    ):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    expires_at = parse_datetime(str(quote.get("expires_at") or ""))
    if expires_at is None or timezone.is_naive(expires_at) or now >= expires_at:
        raise PaymentQuoteExpired("Payment quote has expired")
    subject = quote.get("subject")
    selection = quote.get("selection")
    payment = quote.get("payment")
    terms = quote.get("terms")
    if not all(isinstance(value, dict) for value in (subject, selection, payment, terms)):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if subject.get("kind") == "user":
        valid_subject = isinstance(subject.get("user_id"), str) and bool(subject["user_id"])
    elif subject.get("kind") == "guest":
        valid_subject = (
            isinstance(subject.get("fingerprint"), str) and len(subject["fingerprint"]) == 64
        )
    else:
        valid_subject = False
    if not valid_subject:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    plan = selection.get("plan")
    if (
        not isinstance(plan, dict)
        or not isinstance(selection.get("package_id"), str)
        or not isinstance(selection.get("price_option_id"), str)
        or not isinstance(selection.get("package_name"), str)
        or not isinstance(selection.get("price_option_name"), str)
        or not isinstance(selection.get("duration_months"), int)
        or selection["duration_months"] < 1
        or not _quote_positive_amount(selection.get("monthly_price"))
    ):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if plan.get("payment_type") not in {"full", "installment"} or not _quote_positive_amount(
        plan.get("total_amount")
    ):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    rows = plan.get("installments")
    if not isinstance(rows, list) or not rows or plan.get("installment_count") != len(rows):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    sequences = []
    total_amount = Decimal("0")
    total_cycles = 0
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("sequence"), int)
            or row["sequence"] < 1
            or not _quote_positive_amount(row.get("amount"))
            or not isinstance(row.get("cycle_count"), int)
            or row["cycle_count"] < 1
        ):
            raise PaymentQuoteUnavailable("Payment quote is unavailable")
        sequences.append(row["sequence"])
        total_amount += Decimal(row["amount"])
        total_cycles += row["cycle_count"]
    if (
        sorted(sequences) != list(range(1, len(rows) + 1))
        or total_amount != Decimal(plan["total_amount"])
        or total_cycles != selection["duration_months"]
    ):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if plan["payment_type"] == "full" and len(rows) != 1:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if plan["payment_type"] == "installment" and len(rows) < 2:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if plan.get("initial_payment_amount") != rows[0]["amount"]:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    required_payment = (
        "bank_bin",
        "bank_name",
        "account_number",
        "account_name",
        "transfer_content",
        "payment_config_version",
    )
    if any(
        not isinstance(payment.get(key), str) or not payment[key].strip()
        for key in required_payment[:-1]
    ) or not isinstance(payment.get("payment_config_version"), int):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    if not all(
        isinstance(terms.get(key), str) and terms[key] for key in ("version", "locale", "hash")
    ):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    return quote


def _load_payment_quote(*, quote_token: str, now) -> dict:
    try:
        quote = signing.loads(
            quote_token,
            salt=PAYMENT_QUOTE_SALT,
            max_age=PAYMENT_QUOTE_TTL,
        )
    except signing.SignatureExpired as error:
        raise PaymentQuoteExpired("Payment quote has expired") from error
    except (signing.BadSignature, TypeError, ValueError) as error:
        raise PaymentQuoteUnavailable("Payment quote is unavailable") from error
    if not isinstance(quote, dict):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    try:
        return _validate_payment_quote(quote, now=now)
    except PaymentQuoteExpired:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise PaymentQuoteUnavailable("Payment quote is unavailable") from error


@transaction.atomic
def create_csa_payment_quote(
    *,
    package_id,
    price_option_id,
    payment_plan_id=None,
    terms_locale="vi",
    user=None,
    guest_identity=None,
    now=None,
) -> tuple[str, dict]:
    if user is not None:
        identity = _locked_authenticated_identity(user)
        subject = {"kind": "user", "user_id": str(identity["user"].pk)}
    else:
        identity = _guest_identity(identity=guest_identity or {})
        subject = {"kind": "guest", "fingerprint": _guest_identity_fingerprint(identity)}
    try:
        package = MembershipPackage.objects.select_for_update().get(pk=package_id, is_active=True)
        option = MembershipPackagePriceOption.objects.select_for_update().get(
            pk=price_option_id, package=package, is_active=True
        )
    except (MembershipPackage.DoesNotExist, MembershipPackagePriceOption.DoesNotExist) as error:
        raise PurchasePackageUnavailable("Package or price option is unavailable") from error
    plan = _quote_plan_snapshot(option=option, payment_plan_id=payment_plan_id)
    payment = _locked_payment_settings()
    terms = get_current_csa_terms(terms_locale)
    now = now or timezone.now()
    expires_at = now + PAYMENT_QUOTE_TTL
    request_code = generate_csa_purchase_request_code()
    quote = {
        "v": 1,
        "nonce": secrets.token_hex(32),
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "subject": subject,
        "request_code": request_code,
        "selection": {
            "package_id": str(package.pk),
            "package_name": package.name,
            "price_option_id": str(option.pk),
            "price_option_name": f"{option.duration_months} months",
            "duration_months": option.duration_months,
            "monthly_price": str(option.monthly_price_vnd),
            "plan": plan,
        },
        "payment": {
            "bank_bin": payment.bank_bin,
            "bank_name": payment.bank_name,
            "account_number": payment.account_number,
            "account_name": payment.account_name,
            "transfer_content": request_code,
            "payment_config_version": payment.version,
        },
        "terms": {
            "version": terms["version"],
            "locale": terms["locale"],
            "hash": terms["hash"],
        },
    }
    return _quote_token(quote), quote


def csa_payment_quote_payload(quote_token: str, quote: dict) -> dict:
    plan = quote["selection"]["plan"]
    payment = quote["payment"]
    initial_amount = plan["initial_payment_amount"]
    return {
        "quote_token": quote_token,
        "expires_at": quote["expires_at"],
        "request_code": quote["request_code"],
        "payment": {
            "amount": initial_amount,
            "bank_code": payment["bank_bin"],
            "bank_name": payment["bank_name"],
            "account_number": payment["account_number"],
            "account_name": payment["account_name"],
            "transfer_content": payment["transfer_content"],
        },
        "payment_summary": {
            "payment_type": plan["payment_type"],
            "total_amount": plan["total_amount"],
            "initial_payment_amount": initial_amount,
            "installment_count": plan["installment_count"],
            "installments": plan["installments"],
        },
        "terms": quote["terms"],
        "qr_payload": {
            "acqId": payment["bank_bin"],
            "accountNo": payment["account_number"],
            "accountName": payment["account_name"],
            "amount": initial_amount,
            "addInfo": payment["transfer_content"],
            "format": "text",
            "template": "compact2",
        },
    }


@transaction.atomic
def confirm_csa_payment_quote(*, quote_token, user=None, guest_identity=None, now=None):
    now = now or timezone.now()
    quote = _load_payment_quote(quote_token=quote_token, now=now)
    subject = quote["subject"]
    if subject.get("kind") == "user":
        if user is None or str(user.pk) != str(subject.get("user_id")):
            raise PaymentQuoteIdentityMismatch("Payment quote is unavailable")
        identity = _locked_authenticated_identity(user)
    elif subject.get("kind") == "guest":
        if user is not None:
            raise PaymentQuoteIdentityMismatch("Payment quote is unavailable")
        identity = _guest_identity(identity=guest_identity or {})
        if not hmac.compare_digest(
            str(subject.get("fingerprint") or ""), _guest_identity_fingerprint(identity)
        ):
            raise PaymentQuoteIdentityMismatch("Payment quote is unavailable")
    else:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")

    existing = (
        CSAPurchaseRequest.objects.select_for_update().filter(quote_nonce=quote["nonce"]).first()
    )
    if existing is not None:
        return existing, None

    selection = quote["selection"]
    plan_data = selection.get("plan")
    payment = quote["payment"]
    terms_data = quote["terms"]
    if not isinstance(selection, dict) or not isinstance(plan_data, dict):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    try:
        package = MembershipPackage.objects.select_for_update().get(pk=selection["package_id"])
        option = MembershipPackagePriceOption.objects.select_for_update().get(
            pk=selection["price_option_id"], package=package
        )
        plan = (
            MembershipPaymentPlan.objects.select_for_update().get(pk=plan_data["id"])
            if plan_data.get("id")
            else None
        )
    except (
        KeyError,
        MembershipPackage.DoesNotExist,
        MembershipPackagePriceOption.DoesNotExist,
        MembershipPaymentPlan.DoesNotExist,
    ) as error:
        raise PaymentQuoteUnavailable("Payment quote is unavailable") from error
    if plan is not None and plan.price_option_id != option.pk:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    try:
        terms = get_csa_terms(version=terms_data["version"], locale=terms_data["locale"])
    except (KeyError, InvalidCSATerms) as error:
        raise PaymentQuoteUnavailable("Payment quote is unavailable") from error
    if not hmac.compare_digest(str(terms_data.get("hash") or ""), terms["hash"]):
        raise PaymentQuoteUnavailable("Payment quote is unavailable")

    expire_open_csa_purchase_request_for_phone(phone_e164=identity["phone"], now=now)
    if CSAPurchaseRequest.objects.filter(phone_e164=identity["phone"], open_slot=True).exists():
        raise PurchaseRequestConflict("Phone already has an open CSA purchase request")
    raw_token = None
    guest_token_hash = ""
    if identity["user"] is None:
        raw_token, guest_token_hash = _guest_confirmation_credentials()
    installments = plan_data.get("installments")
    if not isinstance(installments, list) or not installments:
        raise PaymentQuoteUnavailable("Payment quote is unavailable")
    item = CSAPurchaseRequest(
        user=identity["user"],
        request_code=quote["request_code"],
        quote_nonce=quote["nonce"],
        phone_e164=identity["phone"],
        name_snapshot=identity["name"],
        email_snapshot=identity["email"],
        province_code_snapshot=identity["province_code"],
        ward_code_snapshot=identity["ward_code"],
        address_snapshot=identity["address"],
        package=package,
        price_option=option,
        payment_plan=plan,
        package_name_snapshot=selection["package_name"],
        price_option_name_snapshot=selection["price_option_name"],
        duration_months_snapshot=selection["duration_months"],
        amount_snapshot=int(plan_data["total_amount"]),
        price_option_monthly_amount_snapshot=selection["monthly_price"],
        payment_plan_name_snapshot=plan_data["name"] if plan is not None else None,
        payment_type_snapshot=plan_data["payment_type"] if plan is not None else None,
        total_payment_amount_snapshot=plan_data["total_amount"] if plan is not None else None,
        installment_count_snapshot=plan_data["installment_count"] if plan is not None else None,
        payment_installments_snapshot=installments if plan is not None else None,
        currency_snapshot="VND",
        bank_bin_snapshot=payment["bank_bin"],
        bank_name_snapshot=payment["bank_name"],
        account_number_snapshot=payment["account_number"],
        account_name_snapshot=payment["account_name"],
        transfer_content=payment["transfer_content"],
        guest_token_hash=guest_token_hash,
        payment_config_version=payment["payment_config_version"],
        terms_version=terms["version"],
        terms_locale=terms["locale"],
        terms_snapshot=terms["content"],
        terms_hash=terms["hash"],
        terms_accepted_at=now,
        terms_acceptance_method="online",
        status=CSAPurchaseRequestStatus.PAYMENT_CONFIRMED,
        payment_confirmed_at=now,
        expires_at=purchase_request_expires_at(now=now),
    )
    try:
        with transaction.atomic():
            item.save(force_insert=True)
    except (IntegrityError, ValidationError) as error:
        existing = (
            CSAPurchaseRequest.objects.select_for_update()
            .filter(quote_nonce=quote["nonce"])
            .first()
        )
        if existing is not None:
            return existing, None
        if CSAPurchaseRequest.objects.filter(phone_e164=identity["phone"], open_slot=True).exists():
            raise PurchaseRequestConflict(
                "Phone already has an open CSA purchase request"
            ) from error
        raise PaymentQuoteUnavailable("Payment quote is unavailable") from error
    try:
        ensure_csa_payment_schedule(request_id=item.pk)
        first = CSAPurchasePayment.objects.select_for_update().get(
            purchase_request=item, installment_sequence=1
        )
        first.status = CSAPurchasePaymentStatus.REPORTED
        first.save(update_fields=["status", "updated_at"])
    except CSAPaymentError as error:
        raise CSAPurchaseError("Purchase payment schedule is invalid") from error
    return item, raw_token


def confirm_csa_purchase_payment(
    *, request_id, user=None, guest_confirmation_token=None
) -> CSAPurchaseRequest:
    expire_csa_purchase_request_if_due(request_id=request_id)
    with transaction.atomic():
        try:
            item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
        except CSAPurchaseRequest.DoesNotExist as error:
            check_password(str(guest_confirmation_token or ""), DUMMY_GUEST_TOKEN_HASH)
            raise PurchaseConfirmationDenied("Purchase request is unavailable") from error

        if user is not None:
            if item.user_id != user.pk:
                raise PurchaseConfirmationDenied("Purchase request is unavailable")
        else:
            token_hash = item.guest_token_hash or DUMMY_GUEST_TOKEN_HASH
            token_valid = check_password(str(guest_confirmation_token or ""), token_hash)
            if not item.guest_token_hash or not token_valid:
                raise PurchaseConfirmationDenied("Purchase request is unavailable")

        if item.status == CSAPurchaseRequestStatus.EXPIRED:
            raise PurchaseRequestExpired("Purchase request has expired")
        if item.status != CSAPurchaseRequestStatus.PENDING:
            raise PurchaseTransitionConflict("Purchase request is no longer pending")

        try:
            ensure_csa_payment_schedule(request_id=item.pk)
        except CSAPaymentError as error:
            raise CSAPurchaseError("Purchase payment schedule is invalid") from error
        first = CSAPurchasePayment.objects.select_for_update().get(
            purchase_request=item, installment_sequence=1
        )
        if first.status == CSAPurchasePaymentStatus.PENDING:
            first.status = CSAPurchasePaymentStatus.REPORTED
            first.save(update_fields=["status", "updated_at"])

        item.status = CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
        item.payment_confirmed_at = timezone.now()
        item.save(update_fields=["status", "payment_confirmed_at", "updated_at"])
        return item


def _audit_purchase_request(*, item, actor, action, **context):
    AuditLog.objects.create(
        user=actor,
        action=f"csa_purchase_request.{action}",
        context={"request_id": str(item.pk), **context},
    )


def _reject_locked_purchase_request(*, item, actor, reason, customer_resolution=None):
    refund_status = (
        CSAPurchaseRefundStatus.PENDING
        if item.status == CSAPurchaseRequestStatus.PAYMENT_CONFIRMED
        else CSAPurchaseRefundStatus.NOT_APPLICABLE
    )
    item.status = CSAPurchaseRequestStatus.REJECTED
    item.rejected_by = actor
    item.rejected_at = timezone.now()
    item.rejection_reason = reason.strip()
    item.refund_status = refund_status
    item.save(
        update_fields=[
            "status",
            "rejected_by",
            "rejected_at",
            "rejection_reason",
            "refund_status",
            "updated_at",
        ]
    )
    _audit_purchase_request(
        item=item,
        actor=actor,
        action="rejected",
        rejection_reason=item.rejection_reason,
        customer_resolution=customer_resolution,
        customer_id=str(item.customer_id) if item.customer_id else None,
        membership_id=None,
        refund_status=item.refund_status,
    )


@transaction.atomic
def _reject_csa_purchase_request(*, request_id, reason, actor) -> CSAPurchaseRequest:
    try:
        item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
    except CSAPurchaseRequest.DoesNotExist as error:
        raise PurchaseAdminNotFound("Purchase request does not exist") from error
    if item.status == CSAPurchaseRequestStatus.REJECTED:
        return item
    if item.status == CSAPurchaseRequestStatus.EXPIRED:
        raise PurchaseRequestExpired("Purchase request has expired")
    if item.status not in {
        CSAPurchaseRequestStatus.PENDING,
        CSAPurchaseRequestStatus.PAYMENT_CONFIRMED,
    }:
        raise PurchaseAdminWorkflowConflict("Purchase request can no longer be rejected")
    rejection_reason = str(reason or "").strip()
    if not rejection_reason:
        raise CSAPurchaseError("Rejection reason is required")
    _reject_locked_purchase_request(item=item, actor=actor, reason=rejection_reason)
    return item


def reject_csa_purchase_request(*, request_id, reason, actor) -> CSAPurchaseRequest:
    expire_csa_purchase_request_if_due(request_id=request_id)
    return _reject_csa_purchase_request(request_id=request_id, reason=reason, actor=actor)


@transaction.atomic
def _mark_csa_purchase_request_refunded(*, request_id, actor, note="") -> CSAPurchaseRequest:
    note = str(note or "").strip()
    if len(note) > 1000:
        raise CSAPurchaseError("Refund note is too long")
    try:
        item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
    except CSAPurchaseRequest.DoesNotExist as error:
        raise PurchaseAdminNotFound("Purchase request does not exist") from error
    if item.status == CSAPurchaseRequestStatus.EXPIRED:
        raise PurchaseRequestExpired("Purchase request has expired")
    if (
        item.status == CSAPurchaseRequestStatus.REJECTED
        and item.refund_status == CSAPurchaseRefundStatus.COMPLETED
    ):
        return item
    if (
        item.status != CSAPurchaseRequestStatus.REJECTED
        or item.refund_status != CSAPurchaseRefundStatus.PENDING
    ):
        raise PurchaseRefundConflict("Purchase request is not awaiting a refund")
    item.refund_status = CSAPurchaseRefundStatus.COMPLETED
    item.refund_marked_at = timezone.now()
    item.refund_marked_by = actor
    item.refund_note = note or None
    item.save(
        update_fields=[
            "refund_status",
            "refund_marked_at",
            "refund_marked_by",
            "refund_note",
            "updated_at",
        ]
    )
    _audit_purchase_request(
        item=item,
        actor=actor,
        action="refund_completed",
        refund_status=item.refund_status,
    )
    return item


def mark_csa_purchase_request_refunded(*, request_id, actor, note="") -> CSAPurchaseRequest:
    expire_csa_purchase_request_if_due(request_id=request_id)
    return _mark_csa_purchase_request_refunded(request_id=request_id, actor=actor, note=note)


def _customer_details(customer) -> dict:
    return {
        "name": customer.name,
        "province_code": customer.province_code,
        "ward_code": customer.ward_code,
        "address": customer.address,
    }


def _customer_matches_request(customer, item) -> bool:
    return _customer_details(customer) == {
        "name": item.name_snapshot,
        "province_code": item.province_code_snapshot,
        "ward_code": item.ward_code_snapshot,
        "address": item.address_snapshot,
    }


def _customer_resolution_context(customer, item):
    memberships = Membership.objects.filter(customer=customer).order_by("-start_date", "-id")
    return {
        "customer": {
            "id": str(customer.pk),
            "phone": customer.phone_e164,
            **_customer_details(customer),
            "memberships": [
                {
                    "id": str(membership.pk),
                    "package_name": membership.package_name_snapshot,
                    "status": membership.status,
                    "start_date": membership.start_date.isoformat(),
                    "end_date": membership.end_date.isoformat(),
                }
                for membership in memberships
            ],
        },
        "request": {
            "name": item.name_snapshot,
            "phone": item.phone_e164,
            "province_code": item.province_code_snapshot,
            "ward_code": item.ward_code_snapshot,
            "address": item.address_snapshot,
        },
    }


def _audit_conflict_resolution(*, item, actor, customer, resolution, old_data, new_data):
    context = {
        "request_id": str(item.pk),
        "customer_id": str(customer.pk),
        "membership_id": None,
        "customer_resolution": resolution,
    }
    if resolution == "use_request_data":
        context.update({"old_customer_data": old_data, "new_customer_data": new_data})
    AuditLog.objects.create(
        user=actor,
        action="csa_purchase_request.conflict_resolved",
        context=context,
    )


def _resolve_purchase_customer(*, item, actor, customer_resolution, rejection_reason=None):
    locked_user = None
    query = Q(phone_e164=item.phone_e164)
    if item.user_id:
        try:
            locked_user = User.objects.select_for_update().get(pk=item.user_id)
        except User.DoesNotExist as error:
            raise PurchaseCustomerConflict("Purchase request user no longer exists") from error
        query |= Q(user=locked_user)

    customers = list(CSACustomer.objects.select_for_update().filter(query).order_by("id"))
    user_customer = next(
        (customer for customer in customers if customer.user_id == item.user_id), None
    )
    phone_customer = next(
        (customer for customer in customers if customer.phone_e164 == item.phone_e164), None
    )
    if (
        locked_user is not None
        and user_customer is not None
        and phone_customer is not None
        and user_customer.pk != phone_customer.pk
    ):
        raise PurchaseCustomerConflict("User is linked to a different CSA customer")
    if locked_user is not None and user_customer is not None:
        try:
            ensure_csa_customer_complete(user_customer)
        except CSACustomerError as error:
            raise PurchaseCustomerIncomplete("Linked CSA customer is incomplete") from error
        return user_customer, False

    if phone_customer is None:
        try:
            if locked_user is not None:
                customer = create_csa_customer_for_user(
                    user=locked_user,
                    name=item.name_snapshot,
                    phone=item.phone_e164,
                    province_code=item.province_code_snapshot,
                    ward_code=item.ward_code_snapshot,
                    address=item.address_snapshot,
                )
            else:
                customer = create_managed_csa_customer(
                    name=item.name_snapshot,
                    phone=item.phone_e164,
                    province_code=item.province_code_snapshot,
                    ward_code=item.ward_code_snapshot,
                    address=item.address_snapshot,
                )
        except CSACustomerError as error:
            raise PurchaseCustomerConflict("CSA customer could not be created") from error
        return customer, False

    if locked_user is not None and phone_customer.user_id not in {None, locked_user.pk}:
        raise PurchaseCustomerConflict("Phone belongs to another linked CSA customer")

    details_match = _customer_matches_request(phone_customer, item)
    if not details_match:
        if customer_resolution not in CUSTOMER_RESOLUTIONS:
            raise PurchaseCustomerResolutionRequired(
                "Customer information differs from the purchase request",
                context=_customer_resolution_context(phone_customer, item),
            )
        old_data = _customer_details(phone_customer)
        request_data = {
            "name": item.name_snapshot,
            "province_code": item.province_code_snapshot,
            "ward_code": item.ward_code_snapshot,
            "address": item.address_snapshot,
        }
        if customer_resolution == "reject_conflict":
            reason = str(rejection_reason or "").strip()
            if not reason:
                raise CSAPurchaseError("Rejection reason is required for conflict rejection")
            item.customer = phone_customer
            item.save(update_fields=["customer", "updated_at"])
            _audit_conflict_resolution(
                item=item,
                actor=actor,
                customer=phone_customer,
                resolution=customer_resolution,
                old_data=old_data,
                new_data=old_data,
            )
            _reject_locked_purchase_request(
                item=item,
                actor=actor,
                reason=reason,
                customer_resolution=customer_resolution,
            )
            return phone_customer, True
        if customer_resolution == "use_request_data":
            try:
                phone_customer = update_csa_customer(customer=phone_customer, **request_data)
            except CSACustomerError as error:
                raise PurchaseCustomerConflict("CSA customer could not be updated") from error
        _audit_conflict_resolution(
            item=item,
            actor=actor,
            customer=phone_customer,
            resolution=customer_resolution,
            old_data=old_data,
            new_data=_customer_details(phone_customer),
        )

    if locked_user is not None and phone_customer.user_id is None:
        try:
            phone_customer = link_csa_customer_to_user(
                customer=phone_customer, user=locked_user, actor=actor
            )
        except CSACustomerError as error:
            raise PurchaseCustomerConflict("User is already linked to another customer") from error
    try:
        ensure_csa_customer_complete(phone_customer)
    except CSACustomerError as error:
        raise PurchaseCustomerIncomplete("CSA customer is incomplete") from error
    return phone_customer, False


def approve_csa_purchase_request(
    *, request_id, start_date: date, actor, customer_resolution=None, rejection_reason=None
) -> tuple[CSAPurchaseRequest, Membership | None]:
    expire_csa_purchase_request_if_due(request_id=request_id)
    storage = get_contract_pdf_storage()
    documents = None
    try:
        with transaction.atomic():
            try:
                item = CSAPurchaseRequest.objects.select_for_update().get(pk=request_id)
            except CSAPurchaseRequest.DoesNotExist as error:
                raise PurchaseAdminNotFound("Purchase request does not exist") from error
            if item.status == CSAPurchaseRequestStatus.APPROVED and item.membership_id:
                contract = (
                    CSAContract.objects.select_for_update()
                    .filter(purchase_request=item, membership_id=item.membership_id)
                    .first()
                )
                if not contract:
                    raise PurchaseAdminWorkflowConflict(
                        "Approved purchase request is missing its CSA contract"
                    )
                keys = (contract.pdf_vi_storage_key, contract.pdf_en_storage_key)
                try:
                    storage_complete = all(keys) and all(storage.exists(key) for key in keys)
                except Exception:
                    raise CSAContractPDFStorageError(
                        "Approved purchase request contract PDF storage is unavailable"
                    ) from None
                if not storage_complete:
                    raise CSAContractPDFStorageError(
                        "Approved purchase request contract PDF storage is incomplete"
                    )
                return item, item.membership
            if item.status == CSAPurchaseRequestStatus.EXPIRED:
                raise PurchaseRequestExpired("Purchase request has expired")
            if item.status != CSAPurchaseRequestStatus.PAYMENT_CONFIRMED or item.membership_id:
                raise PurchaseAdminWorkflowConflict(
                    "Only a payment-confirmed request can be approved"
                )

            customer, rejected = _resolve_purchase_customer(
                item=item,
                actor=actor,
                customer_resolution=customer_resolution,
                rejection_reason=rejection_reason,
            )
            if rejected:
                return item, None

            existing_memberships = list(
                Membership.objects.select_for_update()
                .filter(customer=customer)
                .exclude(status=MembershipStatus.REVOKED)
                .order_by("start_date", "id")
            )
            proposed_end = add_months(start_date, item.duration_months_snapshot)
            if any(
                membership.start_date < proposed_end and membership.end_date > start_date
                for membership in existing_memberships
            ):
                raise MembershipOverlap("Membership dates overlap an existing membership")
            today = timezone.localdate()
            next_valid_start = max(
                [
                    today,
                    *(
                        membership.end_date
                        for membership in existing_memberships
                        if membership.end_date > today
                    ),
                ]
            )
            if start_date < next_valid_start:
                raise PurchaseStartDateInvalid(
                    "Start date is before the next available customer date"
                )

            ensure_csa_payment_schedule(request_id=item.pk)

            membership = assign_membership(
                customer_id=customer.pk,
                package_id=item.package_id,
                price_option_id=item.price_option_id,
                start_date=start_date,
                farmbrite_order_reference=item.request_code,
                actor=actor,
                purchase_request=item,
            )
            item.customer = customer
            item.membership = membership
            item.status = CSAPurchaseRequestStatus.APPROVED
            item.approved_by = actor
            item.approved_at = timezone.now()
            item.save(
                update_fields=[
                    "customer",
                    "membership",
                    "status",
                    "approved_by",
                    "approved_at",
                    "updated_at",
                ]
            )
            set_csa_payment_due_dates(request_id=item.pk, start_date=start_date)
            first = CSAPurchasePayment.objects.select_for_update().get(
                purchase_request=item, installment_sequence=1
            )
            confirm_locked_csa_payment(payment=first, amount_paid=first.amount_due, actor=actor)
            from memberships.services import ensure_due_cycles

            ensure_due_cycles(membership)
            snapshot = build_online_contract_snapshot(
                purchase_request=item,
                membership=membership,
            )
            contract = create_online_contract(
                membership_id=membership.pk,
                purchase_request_id=item.pk,
                snapshot=snapshot,
            )
            _audit_purchase_request(
                item=item,
                actor=actor,
                action="approved",
                customer_id=str(customer.pk),
                membership_id=str(membership.pk),
                contract_id=str(contract.pk),
                contract_reference_code=contract.reference_code,
                start_date=start_date.isoformat(),
                customer_resolution=customer_resolution,
            )
            _version, documents = issue_contract_version(
                contract_id=contract.pk,
                reason=CSAContractVersionReason.ISSUED,
                actor=actor,
                storage=storage,
            )
        return item, membership
    except Exception:
        if documents is not None:
            delete_contract_documents(documents, storage=storage)
        raise
