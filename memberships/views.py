from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q, Sum
from django.db.models.deletion import ProtectedError
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from content.permissions import IsWebsiteAdmin
from identity.models import User
from memberships.admin_coupons import (
    coupon_payload,
    coupon_payloads,
    save_admin_coupon,
    set_admin_coupon_active,
    validate_coupon_configuration,
)
from memberships.banks import BankDirectoryUnavailable, get_transfer_banks
from memberships.contract_access import (
    CSAContractAccessError,
    InvalidContractLocale,
    contract_download_filename,
    contract_payload,
    contract_queryset,
    contract_summary,
    open_verified_contract_pdf,
    selected_contract_version,
    validated_contract_pdf_query,
)
from memberships.contract_pdfs import CSAContractPDFError, CSAContractPDFStorageError
from memberships.contract_verification import (
    CSAContractVerificationUnavailable,
    csa_contract_verification_payload,
    verify_csa_contract,
)
from memberships.contracts import CSAContractError
from memberships.coupons import (
    CouponError,
    coupon_quote_snapshot,
    discounted_plan,
    normalize_coupon_code,
)
from memberships.expiry import expire_open_csa_purchase_requests_for_user
from memberships.models import (
    CSAContract,
    CSACoupon,
    CSACustomer,
    CSAPurchasePayment,
    CSAPurchaseRequest,
    CSAPurchaseRequestStatus,
    CSATermsDocument,
    Membership,
    MembershipContentLocale,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPackageTranslation,
    MembershipPaymentPlan,
    MembershipPaymentSettings,
    MembershipStatus,
    MembershipUsageOrder,
    Product,
    ProductTranslation,
)
from memberships.payment_plans import (
    PaymentPlanValidationError,
    admin_payment_plan_payload,
    admin_payment_plan_queryset,
    delete_admin_payment_plan,
    initial_payment_amount_snapshot,
    payment_plan_snapshot_payload,
    save_admin_payment_plan,
    set_admin_payment_plan_active,
    validate_payment_plan_configuration,
)
from memberships.payments import (
    CSAPaymentAmountMismatch,
    CSAPaymentError,
    CSAPaymentStateConflict,
    CSAPaymentUnavailable,
    confirm_csa_payment,
    csa_payment_schedule_payload,
    reject_csa_payment,
)
from memberships.permissions import HasMembershipSelfScopes
from memberships.purchases import (
    CSAPurchaseError,
    PaymentQuoteExpired,
    PaymentQuoteIdentityMismatch,
    PaymentQuoteTermsChanged,
    PaymentQuoteUnavailable,
    PaymentSettingsUnavailable,
    PurchaseAdminNotFound,
    PurchaseAdminWorkflowConflict,
    PurchaseConfirmationDenied,
    PurchaseCustomerConflict,
    PurchaseCustomerIncomplete,
    PurchaseCustomerResolutionRequired,
    PurchaseCustomerShippingAddressUnavailable,
    PurchasePackageUnavailable,
    PurchasePaymentPlanRequired,
    PurchasePaymentPlanUnavailable,
    PurchaseRefundConflict,
    PurchaseRequestConflict,
    PurchaseRequestExpired,
    PurchaseStartDateInvalid,
    PurchaseTermsChanged,
    PurchaseTransitionConflict,
    _quote_plan_snapshot,
    approve_csa_purchase_request,
    confirm_csa_payment_quote,
    confirm_csa_purchase_payment,
    create_csa_payment_quote,
    create_public_csa_purchase_request,
    csa_payment_quote_payload,
    mark_csa_purchase_request_refunded,
    reject_csa_purchase_request,
    update_membership_payment_settings,
)
from memberships.serializers import (
    AdminCouponPreviewSerializer,
    AdminCouponQuerySerializer,
    AdminCouponWriteSerializer,
    AdminCSACustomerQuerySerializer,
    AdminCSAPurchaseRequestQuerySerializer,
    AdminCSATermsCloneSerializer,
    AdminCSATermsPublishSerializer,
    AdminCSATermsWriteSerializer,
    AdminMembershipQuerySerializer,
    AdminPackageQuerySerializer,
    AdminPaymentPlanWriteSerializer,
    AdminProductQuerySerializer,
    AuthenticatedCSAPaymentQuoteConfirmSerializer,
    AuthenticatedCSAPaymentQuoteCreateSerializer,
    AuthenticatedCSAPurchaseRequestCreateSerializer,
    CSAContractVerificationSerializer,
    CSACustomerCreateSerializer,
    CSACustomerLinkUserSerializer,
    CSAPaymentConfirmSerializer,
    CSAPaymentQuoteConfirmSerializer,
    CSAPaymentQuoteCreateSerializer,
    CSAPaymentRejectSerializer,
    CSAPurchasePaymentConfirmSerializer,
    CSAPurchaseRequestApproveSerializer,
    CSAPurchaseRequestCreateSerializer,
    CSAPurchaseRequestRefundSerializer,
    CSAPurchaseRequestRejectSerializer,
    CSATrackerLookupSerializer,
    LocalePageQuerySerializer,
    LocaleQuerySerializer,
    MembershipAssignSerializer,
    MembershipPaymentSettingsWriteSerializer,
    MembershipRevokeSerializer,
    PackageWriteSerializer,
    ProductWriteSerializer,
    PublicPackageQuerySerializer,
    UsageCreateSerializer,
    UsageUndoSerializer,
)
from memberships.services import (
    CSACustomerError,
    MembershipError,
    MembershipNotRevocable,
    PriceOptionConflict,
    PriceOptionTotalInvalid,
    assign_membership_with_contract,
    create_supported_managed_csa_customer,
    create_usage_order,
    ensure_due_cycles,
    link_csa_customer_to_user,
    refresh_membership_statuses,
    revoke_membership,
    sync_package_price_options,
    undo_usage_order,
)
from memberships.terms import (
    CSATermsConflict,
    clone_csa_terms_document,
    create_csa_terms_document,
    csa_terms_document_payload,
    delete_csa_terms_document,
    get_current_csa_terms,
    publish_csa_terms,
    update_csa_terms_document,
)
from memberships.throttles import (
    CSAContractVerificationThrottle,
    CSAPaymentQuoteConfirmThrottle,
    CSAPaymentQuoteCreateThrottle,
    CSAPurchaseConfirmThrottle,
    CSAPurchaseCreateThrottle,
    CSATrackerCredentialThrottle,
    CSATrackerIPThrottle,
    CSATrackerSessionThrottle,
)
from memberships.tracker import (
    TRACKER_COOKIE_NAME,
    TRACKER_COOKIE_PATH,
    CSATrackerError,
    CSATrackerLookupUnavailable,
    CSATrackerSessionExpired,
    create_tracker_session,
    lookup_tracker_request,
    resolve_tracker_request,
    tracker_contract,
    tracker_contract_payload,
    tracker_request_payload,
    tracker_session_ttl,
)

PAGE_SIZE = 30


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def paginate(queryset, page, page_size=PAGE_SIZE):
    total = queryset.count()
    start = (page - 1) * page_size
    return queryset[start : start + page_size], {
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def membership_error(error):
    http_status = (
        status.HTTP_400_BAD_REQUEST
        if error.code in {"csa_customer_incomplete", "csa_customer_required_for_membership"}
        else status.HTTP_409_CONFLICT
    )
    return no_store(Response({"errors": [{"code": error.code}]}, status=http_status))


def csa_customer_error(error):
    http_status = (
        status.HTTP_400_BAD_REQUEST
        if error.code
        in {
            "invalid_csa_customer",
            "invalid_csa_customer_phone",
            "csa_customer_phone_required",
            "csa_customer_name_required",
            "csa_customer_province_required",
            "csa_customer_ward_required",
            "csa_customer_address_required",
            "csa_customer_shipping_address_unavailable",
            "csa_customer_shipping_area_unsupported",
            "csa_customer_incomplete",
            "invalid_csa_customer_address",
        }
        else status.HTTP_409_CONFLICT
    )
    return no_store(Response({"errors": [{"code": error.code}]}, status=http_status))


def csa_purchase_error(error):
    if isinstance(error, PaymentSettingsUnavailable):
        http_status = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(
        error,
        (
            PurchaseConfirmationDenied,
            PurchaseAdminNotFound,
            PaymentQuoteUnavailable,
            PaymentQuoteIdentityMismatch,
            CSAPaymentUnavailable,
        ),
    ):
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(
        error,
        (
            PurchaseAdminWorkflowConflict,
            PurchaseCustomerConflict,
            PurchaseCustomerResolutionRequired,
            PurchaseRequestConflict,
            PurchaseRequestExpired,
            PaymentQuoteExpired,
            PaymentQuoteTermsChanged,
            PurchaseTermsChanged,
            PurchasePackageUnavailable,
            PurchasePaymentPlanRequired,
            PurchasePaymentPlanUnavailable,
            PurchaseRefundConflict,
            PurchaseTransitionConflict,
            CSAPaymentStateConflict,
        ),
    ):
        http_status = status.HTTP_409_CONFLICT
    elif isinstance(
        error,
        (
            PurchaseCustomerIncomplete,
            PurchaseCustomerShippingAddressUnavailable,
            PurchaseStartDateInvalid,
        ),
    ):
        http_status = status.HTTP_400_BAD_REQUEST
    elif isinstance(error, CSAPaymentAmountMismatch):
        http_status = status.HTTP_400_BAD_REQUEST
    else:
        http_status = status.HTTP_400_BAD_REQUEST
    error_payload = {"code": error.code}
    if client_message := getattr(error, "client_message", None):
        error_payload["message"] = client_message
    if context := getattr(error, "context", None):
        error_payload["context"] = context
    return no_store(Response({"errors": [error_payload]}, status=http_status))


def csa_contract_error(error):
    http_status = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if isinstance(error, CSAContractPDFStorageError)
        else status.HTTP_400_BAD_REQUEST
    )
    return no_store(Response({"errors": [{"code": error.code}]}, status=http_status))


def csa_contract_access_error(error):
    http_status = (
        status.HTTP_400_BAD_REQUEST
        if isinstance(error, InvalidContractLocale)
        else status.HTTP_404_NOT_FOUND
    )
    return no_store(Response({"errors": [{"code": error.code}]}, status=http_status))


def csa_tracker_error(error):
    http_status = (
        status.HTTP_401_UNAUTHORIZED
        if isinstance(error, CSATrackerSessionExpired)
        else status.HTTP_404_NOT_FOUND
    )
    response = no_store(Response({"errors": [{"code": error.code}]}, status=http_status))
    if isinstance(error, CSATrackerSessionExpired):
        response.delete_cookie(
            TRACKER_COOKIE_NAME,
            path=TRACKER_COOKIE_PATH,
            samesite="Lax",
        )
    return response


def set_tracker_cookie(response, token):
    response.set_cookie(
        TRACKER_COOKIE_NAME,
        token,
        max_age=tracker_session_ttl(),
        path=TRACKER_COOKIE_PATH,
        secure=(not settings.DEBUG or settings.SESSION_COOKIE_SECURE),
        httponly=True,
        samesite="Lax",
    )
    return response


def contract_pdf_response(contract, *, locale, version_id=None):
    try:
        version = selected_contract_version(contract, version_id)
        pdf_file = open_verified_contract_pdf(contract, locale=locale, version=version)
    except CSAContractAccessError as error:
        return csa_contract_access_error(error)
    response = FileResponse(
        pdf_file,
        as_attachment=True,
        filename=contract_download_filename(
            contract, locale, version=version if version_id is not None else None
        ),
        content_type="application/pdf",
    )
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def csa_purchase_validation_error(*, code, fields):
    return no_store(
        Response(
            {"errors": [{"code": code, "fields": fields}]},
            status=status.HTTP_400_BAD_REQUEST,
        )
    )


def membership_payment_settings_payload(item=None):
    if item is None:
        return {
            "key": "default",
            "direct_transfer_enabled": False,
            "bank_bin": "",
            "bank_name": "",
            "account_number": "",
            "account_name": "",
            "version": 0,
            "updated_at": None,
        }
    return {
        "key": item.key,
        "direct_transfer_enabled": item.direct_transfer_enabled,
        "bank_bin": item.bank_bin,
        "bank_name": item.bank_name,
        "account_number": item.account_number,
        "account_name": item.account_name,
        "version": item.version,
        "updated_at": item.updated_at.isoformat(),
    }


def customer_payload(item, *, include_memberships=False):
    user = item.user
    today = timezone.localdate()
    memberships = list(item.memberships.all())
    current = next(
        (
            membership
            for membership in memberships
            if membership.status == MembershipStatus.ACTIVE
            and membership.start_date <= today < membership.end_date
        ),
        None,
    )
    scheduled = next(
        (
            membership
            for membership in sorted(memberships, key=lambda value: (value.start_date, value.pk))
            if membership.status == MembershipStatus.SCHEDULED and membership.start_date > today
        ),
        None,
    )
    data = {
        "id": str(item.pk),
        "name": item.name,
        "phone": item.phone_e164,
        "province_code": item.province_code,
        "ward_code": item.ward_code,
        "address": item.address,
        "user": ({"id": str(user.pk), "name": user.name, "email": user.email} if user else None),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }
    data["current_membership"] = (
        {"id": str(current.pk), "status": current.status} if current else None
    )
    data["next_scheduled_membership"] = (
        {
            "id": str(scheduled.pk),
            "status": scheduled.status,
            "start_date": scheduled.start_date.isoformat(),
        }
        if scheduled
        else None
    )
    data["status"] = (
        "active"
        if current
        else "scheduled"
        if scheduled
        else "history_only"
        if memberships
        else "new"
    )
    if include_memberships:
        data["memberships"] = [
            membership_payload(membership, include_user=False, admin=True)
            for membership in memberships
        ]
    return data


def csa_purchase_payload(item: CSAPurchaseRequest, *, guest_confirmation_token=None):
    amount = str(item.amount_snapshot)
    initial_amount = initial_payment_amount_snapshot(item)
    data = {
        "id": str(item.pk),
        "request_code": item.request_code,
        "status": item.status,
        "package_snapshot": {
            "id": str(item.package_id),
            "name": item.package_name_snapshot,
        },
        "price_option_snapshot": {
            "id": str(item.price_option_id),
            "name": item.price_option_name_snapshot,
            "duration_months": item.duration_months_snapshot,
        },
        "payment_plan": payment_plan_snapshot_payload(item),
        "amount": amount,
        "initial_payment_amount": initial_amount,
        "currency": item.currency_snapshot,
        "transfer_content": item.transfer_content,
        "bank_bin": item.bank_bin_snapshot,
        "bank_name": item.bank_name_snapshot,
        "account_number": item.account_number_snapshot,
        "account_name": item.account_name_snapshot,
        "terms_version": item.terms_version,
        "terms_locale": item.terms_locale,
        "terms_accepted_at": (
            item.terms_accepted_at.isoformat() if item.terms_accepted_at else None
        ),
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        "qr_payload": {
            "acqId": item.bank_bin_snapshot,
            "accountNo": item.account_number_snapshot,
            "accountName": item.account_name_snapshot,
            "amount": initial_amount or "",
            "addInfo": item.transfer_content,
            "format": "text",
            "template": "compact2",
        },
    }
    if item.coupon_snapshot:
        coupon = item.coupon_snapshot
        data["coupon"] = {
            "code": coupon["coupon_code"],
            "discount_type": coupon["discount_type"],
            "discount_amount": coupon["discount_amount"],
        }
        data["customer_payable_total"] = coupon["customer_payable_total"]
        data["contract_total_before_discount"] = coupon["contract_total_before_discount"]
    if guest_confirmation_token is not None:
        data["guest_confirmation_token"] = guest_confirmation_token
    return data


def admin_csa_purchase_payload(item: CSAPurchaseRequest):
    try:
        contract = item.csa_contract
    except CSAContract.DoesNotExist:
        contract = None
    return {
        **csa_purchase_payload(item),
        **csa_payment_schedule_payload(item),
        "user": (
            {
                "id": str(item.user_id),
                "name": item.user.name,
                "email": item.user.email,
            }
            if item.user_id
            else None
        ),
        "customer_id": str(item.customer_id) if item.customer_id else None,
        "membership_id": str(item.membership_id) if item.membership_id else None,
        "contract": contract_summary(contract),
        "name": item.name_snapshot,
        "phone": item.phone_e164,
        "province_code": item.province_code_snapshot,
        "ward_code": item.ward_code_snapshot,
        "address": item.address_snapshot,
        "payment_config_version": item.payment_config_version,
        "payment_confirmed_at": (
            item.payment_confirmed_at.isoformat() if item.payment_confirmed_at else None
        ),
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "rejected_at": item.rejected_at.isoformat() if item.rejected_at else None,
        "expired_at": item.expired_at.isoformat() if item.expired_at else None,
        "expired_reason": item.expired_reason or None,
        "approved_by": (
            {"id": str(item.approved_by_id), "name": item.approved_by.name}
            if item.approved_by_id
            else None
        ),
        "rejected_by": (
            {"id": str(item.rejected_by_id), "name": item.rejected_by.name}
            if item.rejected_by_id
            else None
        ),
        "rejection_reason": item.rejection_reason,
        "refund_status": item.refund_status,
        "refund_marked_at": (item.refund_marked_at.isoformat() if item.refund_marked_at else None),
        "refund_marked_by": (
            {"id": str(item.refund_marked_by_id), "name": item.refund_marked_by.name}
            if item.refund_marked_by_id
            else None
        ),
        "refund_note": item.refund_note,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def csa_purchase_admin_queryset():
    return (
        CSAPurchaseRequest.objects.select_related(
            "user",
            "customer",
            "membership",
            "csa_contract",
            "package",
            "price_option",
            "approved_by",
            "rejected_by",
            "refund_marked_by",
        )
        .prefetch_related(
            Prefetch(
                "payments",
                queryset=CSAPurchasePayment.objects.select_related(
                    "confirmed_by", "rejected_by"
                ).order_by("installment_sequence", "id"),
            )
        )
        .order_by("-created_at", "-id")
    )


def requested_locale(request):
    query = LocaleQuerySerializer(data=request.query_params)
    query.is_valid(raise_exception=True)
    return query.validated_data["locale"]


def _translation_payloads(item, description_field):
    return [
        {
            "locale": translation.locale,
            "name": translation.name,
            description_field: getattr(translation, description_field),
        }
        for translation in item.translations.all()
    ]


def _localized_content(item, locale, description_field):
    translations = {translation.locale: translation for translation in item.translations.all()}
    selected = (
        translations.get(locale)
        or translations.get(MembershipContentLocale.VI)
        or translations.get(MembershipContentLocale.EN)
    )
    if selected:
        return {
            "name": selected.name,
            description_field: getattr(selected, description_field),
            "content_locale": selected.locale,
            "is_fallback": selected.locale != locale,
        }
    return {
        "name": item.name,
        description_field: getattr(item, description_field),
        "content_locale": MembershipContentLocale.VI,
        "is_fallback": locale != MembershipContentLocale.VI,
    }


def product_payload(item):
    content = _localized_content(item, MembershipContentLocale.VI, "short_description")
    return {
        "id": str(item.pk),
        "name": content["name"],
        "short_description": content["short_description"],
        "translations": _translation_payloads(item, "short_description"),
        "unit_size": str(item.unit_size),
        "unit_label_vi": item.unit_label_vi,
        "unit_label_en": item.unit_label_en,
        "is_active": item.is_active,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def price_option_payload(option, *, public=False):
    data = {
        "id": str(option.pk),
        "duration_months": option.duration_months,
        "monthly_price_vnd": str(option.monthly_price_vnd),
        "total_price_vnd": str(option.total_price_vnd),
    }
    if not public:
        data.update(
            {
                "is_active": option.is_active,
                "sort_order": option.sort_order,
                "created_at": option.created_at.isoformat(),
                "updated_at": option.updated_at.isoformat(),
            }
        )
        data["payment_plans"] = [
            admin_payment_plan_payload(plan) for plan in getattr(option, "admin_payment_plans", [])
        ]
    else:
        plans = []
        for plan in getattr(option, "active_payment_plans", []):
            installments = list(plan.installments.all())
            try:
                validate_payment_plan_configuration(plan, installments)
            except PaymentPlanValidationError:
                continue
            plans.append(
                {
                    "id": str(plan.pk),
                    "name": plan.name,
                    "payment_type": plan.payment_type,
                    "total_amount": format(plan.total_amount, "f"),
                    "installment_count": plan.installment_count,
                    "initial_payment_amount": format(installments[0].amount, "f"),
                    "installments": [
                        {
                            "sequence": row.sequence,
                            "amount": format(row.amount, "f"),
                            "cycle_count": row.cycle_count,
                        }
                        for row in installments
                    ],
                }
            )
        data["payment_plans"] = plans
    return data


def package_payload(item, *, public=False, locale=MembershipContentLocale.VI):
    content = _localized_content(item, locale, "description")
    data = {
        "id": str(item.pk),
        "name": content["name"],
        "description": content["description"],
        "quota_policy": item.quota_policy,
        "price_options": [
            price_option_payload(option, public=public)
            for option in item.price_options.all()
            if not public or option.is_active
        ],
        "items": [
            {
                "product_id": str(package_item.product_id),
                "product_name": product_content["name"],
                "short_description": product_content["short_description"],
                "unit_size": str(package_item.product.unit_size),
                "unit_label_vi": package_item.product.unit_label_vi,
                "unit_label_en": package_item.product.unit_label_en,
                "quota_units": package_item.quota_units,
                **(
                    {
                        "requested_locale": locale,
                        "content_locale": product_content["content_locale"],
                        "is_fallback": product_content["is_fallback"],
                    }
                    if public
                    else {
                        "product_is_active": package_item.product.is_active,
                        "product_translations": _translation_payloads(
                            package_item.product, "short_description"
                        ),
                    }
                ),
            }
            for package_item in item.items.all()
            for product_content in [
                _localized_content(package_item.product, locale, "short_description")
            ]
        ],
    }
    if public:
        data.update(
            {
                "requested_locale": locale,
                "content_locale": content["content_locale"],
                "is_fallback": content["is_fallback"],
            }
        )
    else:
        data.update(
            {
                "translations": _translation_payloads(item, "description"),
                "is_active": item.is_active,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
            }
        )
    return data


def _localized_snapshot(
    *,
    locale,
    name_vi,
    name_en,
    legacy_name,
    description_vi=None,
    description_en=None,
    legacy_description=None,
):
    names = {MembershipContentLocale.VI: name_vi, MembershipContentLocale.EN: name_en}
    selected_locale = next(
        (
            candidate
            for candidate in (locale, MembershipContentLocale.VI, MembershipContentLocale.EN)
            if names[candidate]
        ),
        MembershipContentLocale.VI,
    )
    result = {
        "name": names.get(selected_locale) or legacy_name,
        "requested_locale": locale,
        "content_locale": selected_locale,
        "is_fallback": selected_locale != locale,
        "snapshot_complete": bool(name_vi and name_en),
    }
    if legacy_description is not None:
        descriptions = {
            MembershipContentLocale.VI: description_vi or "",
            MembershipContentLocale.EN: description_en or "",
        }
        result["description"] = (
            descriptions[selected_locale] if names.get(selected_locale) else legacy_description
        )
    return result


def membership_payload(
    item,
    *,
    locale=MembershipContentLocale.VI,
    include_quota=False,
    include_user=False,
    admin=False,
):
    content = _localized_snapshot(
        locale=locale,
        name_vi=item.package_name_vi_snapshot,
        name_en=item.package_name_en_snapshot,
        legacy_name=item.package_name_snapshot,
        description_vi=item.package_description_vi_snapshot,
        description_en=item.package_description_en_snapshot,
        legacy_description=item.package_description_snapshot,
    )
    data = {
        "id": str(item.pk),
        "customer_id": str(item.customer_id),
        "user_id": str(item.customer.user_id) if item.customer.user_id else None,
        "package_id": str(item.package_id),
        "price_option_id": str(item.price_option_id),
        "status": item.status,
        "package_name": content["name"],
        "package_description": content["description"],
        "requested_locale": locale,
        "content_locale": content["content_locale"],
        "is_fallback": content["is_fallback"],
        **({"snapshot_complete": content["snapshot_complete"]} if admin else {}),
        "price_option": {
            "duration_months": item.duration_months_snapshot,
            "monthly_price_vnd": str(item.monthly_price_vnd_snapshot),
            "total_price_vnd": str(item.total_price_vnd_snapshot),
        },
        "duration_months": item.duration_months_snapshot,
        "monthly_price_vnd": str(item.monthly_price_vnd_snapshot),
        "total_price_vnd": str(item.total_price_vnd_snapshot),
        "farmbrite_order_reference": item.farmbrite_order_reference,
        "quota_policy": item.quota_policy_snapshot,
        "start_date": item.start_date.isoformat(),
        "end_date": item.end_date.isoformat(),
        "activated_at": item.activated_at.isoformat(),
        **({"current_slot": item.current_slot} if admin else {}),
    }
    if item.revoked_at:
        data["revoked_at"] = item.revoked_at.isoformat()
        data["revoked_by"] = (
            {
                "id": str(item.revoked_by_id),
                "name": item.revoked_by.name,
                "email": item.revoked_by.email,
            }
            if item.revoked_by_id
            else None
        )
        data["revoke_reason"] = item.revoke_reason
    if include_quota:
        data["quota"] = quota_payload(item, locale=locale, admin=admin)
    if include_user:
        user = item.customer.user
        data["customer"] = {
            "id": str(item.customer_id),
            "name": item.customer.name,
            "phone": item.customer.phone_e164,
            "user": (
                {"id": str(user.pk), "name": user.name, "email": user.email} if user else None
            ),
        }
    if admin:
        try:
            contract = item.csa_contract
        except CSAContract.DoesNotExist:
            contract = None
        data["contract"] = contract_summary(contract)
    return data


def upcoming_purchase_request_payload(item: CSAPurchaseRequest):
    return {
        "kind": "purchase_request",
        "id": str(item.pk),
        "request_code": item.request_code,
        "status": item.status,
        "package_name": item.package_name_snapshot,
        "duration_months": item.duration_months_snapshot,
        "amount": str(item.amount_snapshot),
        "currency": item.currency_snapshot,
        "created_at": item.created_at.isoformat(),
        "expires_at": item.expires_at.isoformat() if item.expires_at else None,
    }


def quota_payload(membership, *, locale=MembershipContentLocale.VI, admin=False):
    balances = {
        str(row["product_snapshot_id"]): row["remaining"] or 0
        for row in MembershipCycleQuota.objects.filter(cycle__membership=membership)
        .values("product_snapshot_id")
        .annotate(remaining=Sum("remaining_units"))
    }
    return [
        {
            "product_id": str(snapshot.product_id),
            "product_name": content["name"],
            "requested_locale": locale,
            "content_locale": content["content_locale"],
            "is_fallback": content["is_fallback"],
            **({"snapshot_complete": content["snapshot_complete"]} if admin else {}),
            "unit_size": str(snapshot.unit_size),
            "unit_label_vi": snapshot.unit_label_vi,
            "unit_label_en": snapshot.unit_label_en,
            "quota_units_per_cycle": snapshot.quota_units_per_cycle,
            "remaining_units": balances.get(str(snapshot.pk), 0),
        }
        for snapshot in membership.product_snapshots.all()
        for content in [
            _localized_snapshot(
                locale=locale,
                name_vi=snapshot.product_name_vi_snapshot,
                name_en=snapshot.product_name_en_snapshot,
                legacy_name=snapshot.product_name,
            )
        ]
    ]


def cycle_payload(membership, *, locale=MembershipContentLocale.VI):
    return [
        {
            "sequence": cycle.sequence,
            "start_date": cycle.start_date.isoformat(),
            "end_date": cycle.end_date.isoformat(),
            "granted_at": cycle.granted_at.isoformat(),
            "quotas": [
                {
                    "product_id": str(quota.product_snapshot.product_id),
                    "product_name": content["name"],
                    "requested_locale": locale,
                    "content_locale": content["content_locale"],
                    "is_fallback": content["is_fallback"],
                    "snapshot_complete": content["snapshot_complete"],
                    "granted_units": quota.granted_units,
                    "remaining_units": quota.remaining_units,
                    "expired_units": quota.expired_units,
                }
                for quota in cycle.quotas.all()
                for content in [
                    _localized_snapshot(
                        locale=locale,
                        name_vi=quota.product_snapshot.product_name_vi_snapshot,
                        name_en=quota.product_snapshot.product_name_en_snapshot,
                        legacy_name=quota.product_snapshot.product_name,
                    )
                ]
            ],
        }
        for cycle in membership.cycles.all()
    ]


def usage_payload(
    item,
    *,
    locale=MembershipContentLocale.VI,
    include_allocations=False,
    admin=False,
):
    data = {
        "id": str(item.pk),
        "membership_id": str(item.membership_id),
        "status": item.status,
        "note": item.note,
        "created_at": item.created_at.isoformat(),
        "reversed_at": item.reversed_at.isoformat() if item.reversed_at else None,
        "lines": [
            {
                "product_id": str(line.product_snapshot.product_id),
                "product_name": content["name"],
                "requested_locale": locale,
                "content_locale": content["content_locale"],
                "is_fallback": content["is_fallback"],
                **({"snapshot_complete": content["snapshot_complete"]} if admin else {}),
                "unit_size": str(line.product_snapshot.unit_size),
                "unit_label_vi": line.product_snapshot.unit_label_vi,
                "unit_label_en": line.product_snapshot.unit_label_en,
                "units": line.units,
                **(
                    {
                        "allocations": [
                            {
                                "cycle": allocation.cycle_quota.cycle.sequence,
                                "units": allocation.units,
                            }
                            for allocation in line.allocations.all()
                        ]
                    }
                    if include_allocations
                    else {}
                ),
            }
            for line in item.lines.all()
            for content in [
                _localized_snapshot(
                    locale=locale,
                    name_vi=line.product_snapshot.product_name_vi_snapshot,
                    name_en=line.product_snapshot.product_name_en_snapshot,
                    legacy_name=line.product_snapshot.product_name,
                )
            ]
        ],
    }
    if reversal := getattr(item, "reversal", None):
        data["reversal"] = {
            "reason": reversal.reason,
            "created_at": reversal.created_at.isoformat(),
        }
    return data


def package_queryset():
    return MembershipPackage.objects.prefetch_related(
        "translations", "price_options", "items__product__translations"
    )


def admin_package_queryset():
    return package_queryset().prefetch_related(
        Prefetch(
            "price_options__payment_plans",
            queryset=admin_payment_plan_queryset(),
            to_attr="admin_payment_plans",
        )
    )


def public_package_queryset():
    return package_queryset().prefetch_related(
        Prefetch(
            "price_options__payment_plans",
            queryset=MembershipPaymentPlan.objects.filter(is_active=True)
            .select_related("price_option")
            .order_by("sort_order", "created_at", "id")
            .prefetch_related("installments"),
            to_attr="active_payment_plans",
        )
    )


def membership_queryset():
    return Membership.objects.select_related(
        "customer",
        "customer__user",
        "package",
        "price_option",
        "revoked_by",
        "csa_contract",
    ).prefetch_related("product_snapshots")


def usage_queryset():
    return MembershipUsageOrder.objects.select_related(
        "membership", "customer", "customer__user", "reversal"
    ).prefetch_related("lines__product_snapshot", "lines__allocations__cycle_quota__cycle")


def customer_queryset():
    return CSACustomer.objects.select_related("user").prefetch_related(
        "memberships__customer",
        "memberships__customer__user",
        "memberships__package",
        "memberships__price_option",
        "memberships__revoked_by",
        "memberships__csa_contract",
    )


class PublicPackageListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = PublicPackageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = (
            public_package_queryset()
            .filter(is_active=True, items__isnull=False, price_options__is_active=True)
            .exclude(items__product__is_active=False)
            .distinct()
            .order_by("created_at", "id")
        )
        items, meta = paginate(queryset, query.validated_data["page"])
        response = Response(
            {
                "data": [
                    package_payload(item, public=True, locale=query.validated_data["locale"])
                    for item in items
                ],
                "meta": meta,
            }
        )
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class PublicCSATermsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = LocaleQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        response = Response({"data": get_current_csa_terms(query.validated_data["locale"])})
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class PublicCSAPaymentQuoteView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSAPaymentQuoteCreateThrottle]

    def post(self, request):
        user = request.user if request.user and request.user.is_authenticated else None
        serializer_class = (
            AuthenticatedCSAPaymentQuoteCreateSerializer
            if user is not None
            else CSAPaymentQuoteCreateSerializer
        )
        serializer = serializer_class(data=request.data)
        if not serializer.is_valid():
            return csa_purchase_validation_error(
                code=(
                    "csa_purchase_terms_required"
                    if "terms_accepted" in serializer.errors
                    else "invalid_csa_purchase_request"
                ),
                fields=serializer.errors,
            )
        data = serializer.validated_data
        try:
            quote_token, quote = create_csa_payment_quote(
                package_id=data["package_id"],
                price_option_id=data["price_option_id"],
                payment_plan_id=data.get("payment_plan_id"),
                coupon_code=data.get("coupon_code"),
                terms_locale=data["terms_locale"],
                terms_version=data.get("terms_version"),
                terms_hash=data.get("terms_hash"),
                user=user,
                guest_identity=data.get("guest_identity"),
            )
        except (CSAPurchaseError, CouponError) as error:
            return csa_purchase_error(error)
        return no_store(Response({"data": csa_payment_quote_payload(quote_token, quote)}))


class PublicCSAPaymentQuoteConfirmView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSAPaymentQuoteConfirmThrottle]

    def post(self, request):
        user = request.user if request.user and request.user.is_authenticated else None
        serializer_class = (
            AuthenticatedCSAPaymentQuoteConfirmSerializer
            if user is not None
            else CSAPaymentQuoteConfirmSerializer
        )
        serializer = serializer_class(data=request.data)
        if not serializer.is_valid():
            return csa_purchase_validation_error(
                code="invalid_csa_purchase_confirmation",
                fields=serializer.errors,
            )
        try:
            item, guest_token = confirm_csa_payment_quote(
                quote_token=serializer.validated_data["quote_token"],
                user=user,
                guest_identity=serializer.validated_data.get("guest_identity"),
            )
        except (CSAPurchaseError, CouponError) as error:
            return csa_purchase_error(error)
        return no_store(
            Response(
                {"data": csa_purchase_payload(item, guest_confirmation_token=guest_token)},
                status=status.HTTP_201_CREATED if guest_token is not None else status.HTTP_200_OK,
            )
        )


class PublicCSAPurchaseRequestCreateView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSAPurchaseCreateThrottle]

    def post(self, request):
        user = request.user if request.user and request.user.is_authenticated else None
        serializer_class = (
            AuthenticatedCSAPurchaseRequestCreateSerializer
            if user is not None
            else CSAPurchaseRequestCreateSerializer
        )
        serializer = serializer_class(data=request.data)
        if not serializer.is_valid():
            return csa_purchase_validation_error(
                code=(
                    "csa_purchase_terms_required"
                    if "terms_accepted" in serializer.errors
                    else "invalid_csa_purchase_request"
                ),
                fields=serializer.errors,
            )
        data = dict(serializer.validated_data)
        purchase_data = {
            "package_id": data["package_id"],
            "price_option_id": data["price_option_id"],
            "payment_plan_id": data.get("payment_plan_id"),
            "zalo_whatsapp": data.get("zalo_whatsapp"),
            "terms_locale": data["terms_locale"],
            "terms_version": data["terms_version"],
            "terms_hash": data["terms_hash"],
            "terms_acceptance_ip": request.META.get("REMOTE_ADDR") or None,
            "terms_acceptance_user_agent": request.META.get("HTTP_USER_AGENT") or None,
        }
        if user is None:
            purchase_data.update(
                {
                    field: data[field]
                    for field in ("name", "phone", "province_code", "ward_code", "address")
                }
            )
            purchase_data["email"] = data.get("email")
        try:
            item, guest_token = create_public_csa_purchase_request(user=user, **purchase_data)
        except CSAPurchaseError as error:
            return csa_purchase_error(error)
        return no_store(
            Response(
                {"data": csa_purchase_payload(item, guest_confirmation_token=guest_token)},
                status=status.HTTP_201_CREATED,
            )
        )


class PublicCSAPurchasePaymentConfirmView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSAPurchaseConfirmThrottle]

    def post(self, request, request_id):
        serializer = CSAPurchasePaymentConfirmSerializer(data=request.data)
        if not serializer.is_valid():
            return csa_purchase_validation_error(
                code="invalid_csa_purchase_confirmation",
                fields=serializer.errors,
            )
        user = request.user if request.user and request.user.is_authenticated else None
        try:
            item = confirm_csa_purchase_payment(
                request_id=request_id,
                user=user,
                guest_confirmation_token=serializer.validated_data.get("guest_confirmation_token"),
            )
        except CSAPurchaseError as error:
            return csa_purchase_error(error)
        return no_store(Response({"data": csa_purchase_payload(item)}))


class PublicCSAContractTrackerLookupView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSATrackerIPThrottle, CSATrackerCredentialThrottle]

    def post(self, request):
        serializer = CSATrackerLookupSerializer(data=request.data)
        if not serializer.is_valid():
            return csa_tracker_error(CSATrackerLookupUnavailable("Tracker lookup is unavailable"))
        try:
            item = lookup_tracker_request(
                reference_code=serializer.validated_data["reference_code"],
                phone_e164=serializer.validated_data["phone"],
            )
        except CSATrackerError as error:
            return csa_tracker_error(error)
        token = create_tracker_session(
            request_id=item.pk,
            previous_token=request.COOKIES.get(TRACKER_COOKIE_NAME),
        )
        response = no_store(Response({"data": tracker_request_payload(item)}))
        return set_tracker_cookie(response, token)


class PublicCSAContractVerificationView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSAContractVerificationThrottle]

    def finalize_response(self, request, response, *args, **kwargs):
        return no_store(super().finalize_response(request, response, *args, **kwargs))

    def get(self, request):
        serializer = CSAContractVerificationSerializer(data=request.query_params)
        if not serializer.is_valid():
            return no_store(
                Response(
                    {"errors": [{"code": CSAContractVerificationUnavailable.code}]},
                    status=status.HTTP_404_NOT_FOUND,
                )
            )
        try:
            contract = verify_csa_contract(
                reference_code=serializer.validated_data["reference_code"]
            )
        except CSAContractVerificationUnavailable as error:
            return no_store(
                Response(
                    {"errors": [{"code": error.code}]},
                    status=status.HTTP_404_NOT_FOUND,
                )
            )
        return no_store(Response({"data": csa_contract_verification_payload(contract)}))


class PublicCSAContractTrackerContractView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSATrackerSessionThrottle]

    def get(self, request):
        try:
            item = resolve_tracker_request(request.COOKIES.get(TRACKER_COOKIE_NAME))
            contract = tracker_contract(item)
        except CSATrackerError as error:
            return csa_tracker_error(error)
        return no_store(Response({"data": tracker_contract_payload(contract)}))


class PublicCSAContractTrackerPDFView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [CSATrackerSessionThrottle]

    def get(self, request):
        try:
            item = resolve_tracker_request(request.COOKIES.get(TRACKER_COOKIE_NAME))
            contract = tracker_contract(item)
        except CSATrackerError as error:
            return csa_tracker_error(error)
        try:
            locale, version_id = validated_contract_pdf_query(request.query_params)
        except CSAContractAccessError as error:
            return csa_contract_access_error(error)
        return contract_pdf_response(contract, locale=locale, version_id=version_id)


class SelfMembershipView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
        locale = requested_locale(request)
        refresh_membership_statuses()
        today = timezone.localdate()
        item = (
            membership_queryset()
            .select_for_update()
            .filter(
                customer__user=request.user,
                status=MembershipStatus.ACTIVE,
                start_date__lte=today,
                end_date__gt=today,
            )
            .first()
        )
        if not item:
            return no_store(Response({"data": None}))
        ensure_due_cycles(item)
        item.refresh_from_db()
        if item.status != MembershipStatus.ACTIVE:
            return no_store(Response({"data": None}))
        return no_store(Response({"data": membership_payload(item, locale=locale)}))


class SelfMembershipOverviewView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
        locale = requested_locale(request)
        refresh_membership_statuses()
        expire_open_csa_purchase_requests_for_user(user_id=request.user.pk)
        today = timezone.localdate()
        memberships = membership_queryset().filter(customer__user=request.user)
        active = memberships.filter(
            status=MembershipStatus.ACTIVE,
            start_date__lte=today,
            end_date__gt=today,
        ).first()
        scheduled = memberships.filter(status=MembershipStatus.SCHEDULED).order_by(
            "start_date", "id"
        )
        purchase_requests = CSAPurchaseRequest.objects.filter(
            user=request.user,
            membership__isnull=True,
            status__in=(
                CSAPurchaseRequestStatus.PENDING,
                CSAPurchaseRequestStatus.PAYMENT_CONFIRMED,
            ),
        ).order_by("created_at", "id")

        upcoming = [
            {"kind": "membership", **membership_payload(item, locale=locale)} for item in scheduled
        ]
        upcoming.extend(upcoming_purchase_request_payload(item) for item in purchase_requests)
        return no_store(
            Response(
                {
                    "data": {
                        "active_membership": (
                            membership_payload(active, locale=locale) if active else None
                        ),
                        "upcoming": upcoming,
                    }
                }
            )
        )


class SelfMembershipContractView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request, membership_id):
        contract = (
            contract_queryset()
            .filter(
                membership_id=membership_id,
                membership__customer__user_id=request.user.pk,
            )
            .first()
        )
        if contract is None:
            return no_store(Response(status=status.HTTP_404_NOT_FOUND))
        return no_store(Response({"data": contract_payload(contract, admin=False)}))


class SelfMembershipContractPDFView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request, membership_id):
        try:
            locale, version_id = validated_contract_pdf_query(request.query_params)
        except CSAContractAccessError as error:
            return csa_contract_access_error(error)
        contract = (
            contract_queryset()
            .filter(
                membership_id=membership_id,
                membership__customer__user_id=request.user.pk,
            )
            .first()
        )
        if contract is None:
            return no_store(Response(status=status.HTTP_404_NOT_FOUND))
        return contract_pdf_response(contract, locale=locale, version_id=version_id)


class SelfQuotaView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
        locale = requested_locale(request)
        refresh_membership_statuses()
        today = timezone.localdate()
        item = (
            membership_queryset()
            .select_for_update()
            .filter(
                customer__user=request.user,
                status=MembershipStatus.ACTIVE,
                start_date__lte=today,
                end_date__gt=today,
            )
            .first()
        )
        if not item:
            return no_store(Response({"data": None}))
        ensure_due_cycles(item)
        item.refresh_from_db()
        if item.status != MembershipStatus.ACTIVE:
            return no_store(Response({"data": None}))
        return no_store(
            Response(
                {
                    "data": {
                        "membership_id": str(item.pk),
                        "requested_locale": locale,
                        "products": quota_payload(item, locale=locale),
                    }
                }
            )
        )


class SelfUsageHistoryView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        query = LocalePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        items, meta = paginate(
            usage_queryset().filter(customer__user=request.user),
            query.validated_data["page"],
        )
        return no_store(
            Response(
                {
                    "data": [
                        usage_payload(item, locale=query.validated_data["locale"]) for item in items
                    ],
                    "meta": meta,
                }
            )
        )


class AdminProductViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminProductQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = Product.objects.prefetch_related("translations")
        if search := query.validated_data["search"]:
            queryset = queryset.filter(
                Q(translations__name__icontains=search)
                | Q(translations__short_description__icontains=search)
                | Q(name__icontains=search)
                | Q(short_description__icontains=search)
            ).distinct()
        if "active" in request.query_params:
            queryset = queryset.filter(is_active=query.validated_data["active"])
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(Response({"data": [product_payload(item) for item in items], "meta": meta}))

    @transaction.atomic
    def create(self, request):
        serializer = ProductWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        translations = data.pop("translations")
        vi = next(item for item in translations if item["locale"] == MembershipContentLocale.VI)
        item = Product.objects.create(
            name=vi["name"], short_description=vi["short_description"], **data
        )
        ProductTranslation.objects.bulk_create(
            [ProductTranslation(product=item, **translation) for translation in translations]
        )
        item = Product.objects.prefetch_related("translations").get(pk=item.pk)
        return no_store(Response({"data": product_payload(item)}, status=status.HTTP_201_CREATED))

    def retrieve(self, request, pk=None):
        item = get_object_or_404(Product.objects.prefetch_related("translations"), pk=pk)
        return no_store(Response({"data": product_payload(item)}))

    @transaction.atomic
    def partial_update(self, request, pk=None):
        item = get_object_or_404(Product.objects.select_for_update(), pk=pk)
        serializer = ProductWriteSerializer(item, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("is_active") is False:
            from commerce.models import Product as StoreProduct

            if (
                StoreProduct.objects.filter(csa_quota_group=item).exists()
                or MembershipPackageItem.objects.filter(
                    product=item, package__is_active=True
                ).exists()
            ):
                return no_store(
                    Response(
                        {"errors": [{"code": "csa_quota_group_in_use"}]},
                        status=status.HTTP_409_CONFLICT,
                    )
                )
        translations = data.pop("translations", None)
        for field, value in data.items():
            setattr(item, field, value)
        if translations is not None:
            vi = next(item for item in translations if item["locale"] == MembershipContentLocale.VI)
            item.name = vi["name"]
            item.short_description = vi["short_description"]
        item.save()
        if translations is not None:
            for translation in translations:
                ProductTranslation.objects.update_or_create(
                    product=item,
                    locale=translation["locale"],
                    defaults={
                        "name": translation["name"],
                        "short_description": translation["short_description"],
                    },
                )
        from commerce.models import Product as StoreProduct
        from commerce.services import schedule_store_revalidation

        linked_slugs = list(
            StoreProduct.objects.filter(csa_quota_group=item).values_list("slug", flat=True)
        )
        if linked_slugs:
            schedule_store_revalidation(slugs=linked_slugs)
        return no_store(
            Response(
                {
                    "data": product_payload(
                        Product.objects.prefetch_related("translations").get(pk=item.pk)
                    )
                }
            )
        )

    def destroy(self, request, pk=None):
        item = get_object_or_404(Product, pk=pk)
        try:
            item.delete()
        except ProtectedError as error:
            from commerce.models import Product as StoreProduct

            if StoreProduct.objects.filter(csa_quota_group=item).exists():
                return no_store(
                    Response(
                        {"errors": [{"code": "csa_quota_group_in_use"}]},
                        status=status.HTTP_409_CONFLICT,
                    )
                )
            raise ValidationError({"product": "Product is in use."}) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


def _lock_active_package_products(product_ids):
    ids = set(product_ids)
    products = list(Product.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    if len(products) != len(ids) or any(not product.is_active for product in products):
        raise ValidationError({"items": "Package items must use active products."})


class AdminPackageViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminPackageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = admin_package_queryset()
        if search := query.validated_data["search"]:
            queryset = queryset.filter(
                Q(translations__name__icontains=search)
                | Q(translations__description__icontains=search)
                | Q(name__icontains=search)
                | Q(description__icontains=search)
            ).distinct()
        if "active" in request.query_params:
            queryset = queryset.filter(is_active=query.validated_data["active"])
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(Response({"data": [package_payload(item) for item in items], "meta": meta}))

    @transaction.atomic
    def create(self, request):
        serializer = PackageWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = data.pop("items")
        translations = data.pop("translations")
        options = data.pop("price_options")
        if data.get("is_active", True):
            _lock_active_package_products(item["product_id"].pk for item in items)
        vi = next(item for item in translations if item["locale"] == MembershipContentLocale.VI)
        package = MembershipPackage.objects.create(
            name=vi["name"], description=vi["description"], **data
        )
        MembershipPackageTranslation.objects.bulk_create(
            [MembershipPackageTranslation(package=package, **item) for item in translations]
        )
        MembershipPackageItem.objects.bulk_create(
            [
                MembershipPackageItem(
                    package=package, product=item["product_id"], quota_units=item["quota_units"]
                )
                for item in items
            ]
        )
        try:
            sync_package_price_options(package=package, options=options)
        except (PriceOptionConflict, PriceOptionTotalInvalid) as error:
            raise ValidationError({"price_options": str(error)}) from error
        return no_store(
            Response(
                {"data": package_payload(admin_package_queryset().get(pk=package.pk))},
                status=status.HTTP_201_CREATED,
            )
        )

    def retrieve(self, request, pk=None):
        return no_store(
            Response({"data": package_payload(get_object_or_404(admin_package_queryset(), pk=pk))})
        )

    @transaction.atomic
    def partial_update(self, request, pk=None):
        package = get_object_or_404(MembershipPackage.objects.select_for_update(), pk=pk)
        serializer = PackageWriteSerializer(package, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if data.get("is_active", package.is_active):
            submitted_items = data.get("items")
            product_ids = (
                (item["product_id"].pk for item in submitted_items)
                if submitted_items is not None
                else package.items.values_list("product_id", flat=True)
            )
            _lock_active_package_products(product_ids)
        items = data.pop("items", None)
        translations = data.pop("translations", None)
        options = data.pop("price_options", None)
        for field, value in data.items():
            setattr(package, field, value)
        if translations is not None:
            vi = next(item for item in translations if item["locale"] == MembershipContentLocale.VI)
            package.name = vi["name"]
            package.description = vi["description"]
        package.save()
        if translations is not None:
            for item in translations:
                MembershipPackageTranslation.objects.update_or_create(
                    package=package,
                    locale=item["locale"],
                    defaults={"name": item["name"], "description": item["description"]},
                )
        if items is not None:
            package.items.all().delete()
            MembershipPackageItem.objects.bulk_create(
                [
                    MembershipPackageItem(
                        package=package,
                        product=item["product_id"],
                        quota_units=item["quota_units"],
                    )
                    for item in items
                ]
            )
        if options is not None:
            try:
                sync_package_price_options(package=package, options=options)
            except (PriceOptionConflict, PriceOptionTotalInvalid) as error:
                raise ValidationError({"price_options": str(error)}) from error
            except IntegrityError as error:
                raise ValidationError(
                    {"price_options": "Duration months must be unique within a package."}
                ) from error
        return no_store(
            Response({"data": package_payload(admin_package_queryset().get(pk=package.pk))})
        )

    def destroy(self, request, pk=None):
        package = get_object_or_404(MembershipPackage, pk=pk)
        try:
            package.delete()
        except ProtectedError as error:
            raise ValidationError({"package": "Package is in use."}) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


def _payment_plan_error(code, *, fields=None, http_status=status.HTTP_400_BAD_REQUEST):
    item = {"code": code}
    if fields is not None:
        item["fields"] = fields
    return no_store(Response({"errors": [item]}, status=http_status))


def _coupon_error(code, *, http_status=400):
    return no_store(Response({"errors": [{"code": code}]}, status=http_status))


def _csa_terms_error(error):
    code = error.code
    return no_store(
        Response(
            {"errors": [{"code": code}]},
            status=(
                status.HTTP_404_NOT_FOUND
                if code == "csa_terms_not_found"
                else status.HTTP_409_CONFLICT
                if code
                in {
                    "csa_terms_immutable",
                    "csa_terms_revision_conflict",
                    "csa_terms_publish_conflict",
                    "csa_terms_version_exists",
                }
                else status.HTTP_400_BAD_REQUEST
            ),
        )
    )


class AdminCSATermsListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        documents = CSATermsDocument.objects.prefetch_related("clauses").all()
        return no_store(
            Response({"data": [csa_terms_document_payload(item) for item in documents]})
        )

    def post(self, request):
        serializer = AdminCSATermsWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return _csa_terms_error(CSATermsConflict("csa_terms_invalid"))
        try:
            document = create_csa_terms_document(
                data=dict(serializer.validated_data), actor=request.user
            )
        except CSATermsConflict as error:
            return _csa_terms_error(error)
        return no_store(Response({"data": csa_terms_document_payload(document)}, status=201))


class AdminCSATermsDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, document_id):
        document = (
            CSATermsDocument.objects.prefetch_related("clauses").filter(pk=document_id).first()
        )
        if document is None:
            return _csa_terms_error(CSATermsConflict("csa_terms_not_found"))
        return no_store(Response({"data": csa_terms_document_payload(document)}))

    def patch(self, request, document_id):
        serializer = AdminCSATermsWriteSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _csa_terms_error(CSATermsConflict("csa_terms_invalid"))
        try:
            document = update_csa_terms_document(
                document_id=document_id, data=dict(serializer.validated_data)
            )
        except CSATermsConflict as error:
            return _csa_terms_error(error)
        return no_store(Response({"data": csa_terms_document_payload(document)}))

    def delete(self, request, document_id):
        try:
            delete_csa_terms_document(document_id=document_id)
        except CSATermsConflict as error:
            return _csa_terms_error(error)
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminCSATermsCloneView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, document_id):
        serializer = AdminCSATermsCloneSerializer(data=request.data)
        if not serializer.is_valid():
            return _csa_terms_error(CSATermsConflict("csa_terms_invalid"))
        try:
            document = clone_csa_terms_document(
                document_id=document_id,
                version=serializer.validated_data["version"],
                actor=request.user,
            )
        except CSATermsConflict as error:
            return _csa_terms_error(error)
        return no_store(Response({"data": csa_terms_document_payload(document)}, status=201))


class AdminCSATermsPublishView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, document_id):
        serializer = AdminCSATermsPublishSerializer(data=request.data)
        if not serializer.is_valid():
            return _csa_terms_error(CSATermsConflict("csa_terms_invalid"))
        document = CSATermsDocument.objects.filter(pk=document_id).first()
        if document is None:
            return _csa_terms_error(CSATermsConflict("csa_terms_not_found"))
        try:
            document = publish_csa_terms(
                document,
                actor=request.user,
                expected_revision=serializer.validated_data["revision"],
            )
        except CSATermsConflict as error:
            return _csa_terms_error(error)
        return no_store(Response({"data": csa_terms_document_payload(document)}))


def _coupon_input_error(errors):
    field = next(iter(errors), None)
    code = {
        "code": "coupon_invalid",
        "discount_type": "coupon_invalid_type",
        "discount_value": "coupon_invalid_value",
        "usage_limit": "coupon_invalid_usage_limit",
        "starts_at": "coupon_invalid_date_range",
        "expires_at": "coupon_invalid_date_range",
        "minimum_amount": "coupon_invalid_value",
        "maximum_discount_amount": "coupon_invalid_value",
    }.get(field, "coupon_not_applicable")
    return _coupon_error(code)


class AdminCouponListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = AdminCouponQuerySerializer(data=request.query_params)
        if not query.is_valid() or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            return _coupon_error("coupon_invalid")
        now = timezone.now()
        queryset = CSACoupon.objects.all()
        if discount_type := query.validated_data.get("discount_type"):
            queryset = queryset.filter(discount_type=discount_type)
        status_filter = query.validated_data.get("status")
        if status_filter == "inactive":
            queryset = queryset.filter(is_active=False)
        elif status_filter == "scheduled":
            queryset = queryset.filter(is_active=True, starts_at__gt=now)
        elif status_filter == "expired":
            queryset = queryset.filter(is_active=True, starts_at__lte=now, expires_at__lte=now)
        elif status_filter == "exhausted":
            queryset = queryset.filter(
                is_active=True,
                starts_at__lte=now,
                expires_at__gt=now,
                used_count__gte=F("usage_limit"),
            )
        elif status_filter == "active":
            queryset = queryset.filter(
                is_active=True,
                starts_at__lte=now,
                expires_at__gt=now,
                used_count__lt=F("usage_limit"),
            )
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(Response({"data": coupon_payloads(items), "meta": meta}))

    def post(self, request):
        serializer = AdminCouponWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return _coupon_input_error(serializer.errors)
        try:
            coupon = save_admin_coupon(data=serializer.validated_data)
        except CouponError as error:
            return _coupon_error(
                error.code, http_status=409 if error.code == "coupon_duplicate_code" else 400
            )
        return no_store(Response({"data": coupon_payload(coupon)}, status=201))


class AdminCouponDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, coupon_id):
        coupon = CSACoupon.objects.filter(pk=coupon_id).first()
        if coupon is None:
            return _coupon_error("coupon_not_found", http_status=404)
        return no_store(Response({"data": coupon_payload(coupon)}))

    def patch(self, request, coupon_id):
        serializer = AdminCouponWriteSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _coupon_input_error(serializer.errors)
        try:
            coupon = save_admin_coupon(data=serializer.validated_data, coupon_id=coupon_id)
        except CouponError as error:
            code = error.code
            return _coupon_error(
                code,
                http_status=404
                if code == "coupon_not_found"
                else 409
                if code in {"coupon_duplicate_code", "coupon_cannot_edit_used"}
                else 400,
            )
        return no_store(Response({"data": coupon_payload(coupon)}))


class AdminCouponActivationView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, coupon_id, action):
        if action not in {"activate", "deactivate"}:
            return _coupon_error("coupon_not_found", http_status=404)
        if request.data:
            return _coupon_error("coupon_invalid")
        try:
            coupon = set_admin_coupon_active(coupon_id=coupon_id, active=action == "activate")
        except CouponError as error:
            return _coupon_error(
                error.code, http_status=404 if error.code == "coupon_not_found" else 409
            )
        return no_store(Response({"data": coupon_payload(coupon)}))


class AdminCouponPreviewView(APIView):
    permission_classes = [IsWebsiteAdmin]

    @transaction.atomic
    def post(self, request):
        serializer = AdminCouponPreviewSerializer(data=request.data)
        if not serializer.is_valid():
            return _coupon_input_error(serializer.errors)
        data = dict(serializer.validated_data)
        plan_id = data.pop("payment_plan_id")
        data["package_ids"] = [str(value) for value in data.get("package_ids", [])]
        data["price_option_ids"] = [str(value) for value in data.get("price_option_ids", [])]
        coupon = CSACoupon(**data)
        try:
            coupon.code = normalize_coupon_code(coupon.code)
            validate_coupon_configuration(coupon)
            plan = MembershipPaymentPlan.objects.select_related("price_option__package").get(
                pk=plan_id
            )
            option = plan.price_option
            if not plan.is_active or not option.is_active or not option.package.is_active:
                raise CouponError("coupon_not_applicable")
            plan_data = _quote_plan_snapshot(option=option, payment_plan_id=plan.pk)
            snapshot = coupon_quote_snapshot(
                coupon=coupon,
                package_id=option.package_id,
                price_option_id=option.pk,
                plan={**plan_data, "contract_total": str(option.total_price_vnd)},
                now=timezone.now(),
                check_availability=False,
            )
        except (
            CouponError,
            PurchasePaymentPlanUnavailable,
            MembershipPaymentPlan.DoesNotExist,
        ) as error:
            code = error.code if isinstance(error, CouponError) else "coupon_not_applicable"
            if code not in {"coupon_invalid_value", "coupon_not_applicable"}:
                code = "coupon_not_applicable"
            return _coupon_error(code)
        return no_store(
            Response(
                {
                    "data": {
                        **snapshot,
                        "payment_type": plan.payment_type,
                        "installments": discounted_plan(plan_data, snapshot),
                    }
                }
            )
        )


class AdminCouponOptionsView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        packages = MembershipPackage.objects.filter(is_active=True).prefetch_related(
            Prefetch(
                "price_options",
                queryset=MembershipPackagePriceOption.objects.filter(
                    is_active=True
                ).prefetch_related(
                    Prefetch(
                        "payment_plans",
                        queryset=MembershipPaymentPlan.objects.filter(
                            is_active=True
                        ).prefetch_related("installments"),
                    )
                ),
            )
        )
        return no_store(
            Response(
                {
                    "data": [
                        {
                            "id": str(package.pk),
                            "name": package.name,
                            "price_options": [
                                {
                                    "id": str(option.pk),
                                    "duration_months": option.duration_months,
                                    "payment_plans": [
                                        {
                                            "id": str(plan.pk),
                                            "name": plan.name,
                                            "payment_type": plan.payment_type,
                                            "installment_count": plan.installment_count,
                                        }
                                        for plan in option.payment_plans.all()
                                    ],
                                }
                                for option in package.price_options.all()
                            ],
                        }
                        for package in packages
                    ]
                }
            )
        )


class AdminPaymentPlanListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, option_id):
        get_object_or_404(MembershipPackagePriceOption, pk=option_id)
        plans = admin_payment_plan_queryset().filter(price_option_id=option_id)
        return no_store(Response({"data": [admin_payment_plan_payload(plan) for plan in plans]}))

    def post(self, request, option_id):
        serializer = AdminPaymentPlanWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return _payment_plan_error("payment_plan_invalid", fields=serializer.errors)
        try:
            plan = save_admin_payment_plan(option_id=option_id, data=serializer.validated_data)
        except MembershipPackagePriceOption.DoesNotExist:
            return _payment_plan_error("payment_plan_unavailable", http_status=404)
        except PaymentPlanValidationError as error:
            return _payment_plan_error(error.code)
        return no_store(
            Response({"data": admin_payment_plan_payload(plan)}, status=status.HTTP_201_CREATED)
        )


class AdminPaymentPlanDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def _plan(self, option_id, plan_id):
        return get_object_or_404(
            admin_payment_plan_queryset(),
            pk=plan_id,
            price_option_id=option_id,
        )

    def get(self, request, option_id, plan_id):
        return no_store(
            Response({"data": admin_payment_plan_payload(self._plan(option_id, plan_id))})
        )

    def patch(self, request, option_id, plan_id):
        serializer = AdminPaymentPlanWriteSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _payment_plan_error("payment_plan_invalid", fields=serializer.errors)
        try:
            plan = save_admin_payment_plan(
                option_id=option_id, plan_id=plan_id, data=serializer.validated_data
            )
        except (MembershipPackagePriceOption.DoesNotExist, MembershipPaymentPlan.DoesNotExist):
            return _payment_plan_error("payment_plan_unavailable", http_status=404)
        except PaymentPlanValidationError as error:
            return _payment_plan_error(
                error.code, http_status=409 if error.code == "payment_plan_in_use" else 400
            )
        return no_store(Response({"data": admin_payment_plan_payload(plan)}))

    def delete(self, request, option_id, plan_id):
        try:
            delete_admin_payment_plan(option_id=option_id, plan_id=plan_id)
        except (MembershipPackagePriceOption.DoesNotExist, MembershipPaymentPlan.DoesNotExist):
            return _payment_plan_error("payment_plan_unavailable", http_status=404)
        except PaymentPlanValidationError as error:
            return _payment_plan_error(error.code, http_status=409)
        return no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminPaymentPlanActivationView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, option_id, plan_id, action):
        if action not in {"activate", "deactivate"}:
            return _payment_plan_error("payment_plan_unavailable", http_status=404)
        if request.data:
            return _payment_plan_error("payment_plan_invalid")
        try:
            plan = set_admin_payment_plan_active(
                option_id=option_id, plan_id=plan_id, active=action == "activate"
            )
        except (MembershipPackagePriceOption.DoesNotExist, MembershipPaymentPlan.DoesNotExist):
            return _payment_plan_error("payment_plan_unavailable", http_status=404)
        except PaymentPlanValidationError as error:
            return _payment_plan_error(error.code)
        return no_store(Response({"data": admin_payment_plan_payload(plan)}))


class AdminMembershipPaymentSettingsView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        item = MembershipPaymentSettings.objects.filter(pk="default").first()
        return no_store(Response({"data": membership_payment_settings_payload(item)}))

    def patch(self, request):
        serializer = MembershipPaymentSettingsWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return no_store(
                Response(
                    {
                        "errors": [
                            {
                                "code": "invalid_membership_payment_settings",
                                "fields": serializer.errors,
                            }
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        try:
            item = update_membership_payment_settings(
                **serializer.validated_data,
                actor=request.user,
            )
        except DjangoValidationError as error:
            return no_store(
                Response(
                    {
                        "errors": [
                            {
                                "code": "invalid_membership_payment_settings",
                                "fields": error.message_dict,
                            }
                        ]
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        return no_store(Response({"data": membership_payment_settings_payload(item)}))


class AdminMembershipBanksView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        try:
            banks = get_transfer_banks()
        except BankDirectoryUnavailable:
            return no_store(
                Response(
                    {"errors": [{"code": "membership_bank_directory_unavailable"}]},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            )
        return no_store(Response({"data": banks}))


class AdminCSAPurchaseRequestViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]
    lookup_value_converter = "uuid"

    def list(self, request):
        query = AdminCSAPurchaseRequestQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        queryset = csa_purchase_admin_queryset()
        if search := data["search"].strip():
            queryset = queryset.filter(
                Q(request_code__icontains=search)
                | Q(phone_e164__icontains=search)
                | Q(name_snapshot__icontains=search)
            )
        if request_status := data.get("status"):
            queryset = queryset.filter(status=request_status)
        items, meta = paginate(queryset, data["page"], data["page_size"])
        return no_store(
            Response({"data": [admin_csa_purchase_payload(item) for item in items], "meta": meta})
        )

    def retrieve(self, request, pk=None):
        item = get_object_or_404(csa_purchase_admin_queryset(), pk=pk)
        return no_store(Response({"data": admin_csa_purchase_payload(item)}))

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        serializer = CSAPurchaseRequestApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, _membership = approve_csa_purchase_request(
                request_id=pk,
                start_date=serializer.validated_data["start_date"],
                customer_resolution=serializer.validated_data.get("customer_resolution"),
                rejection_reason=serializer.validated_data.get("rejection_reason"),
                actor=request.user,
            )
        except (CSAPurchaseError, CSAPaymentError) as error:
            return csa_purchase_error(error)
        except MembershipError as error:
            return membership_error(error)
        except (CSAContractError, CSAContractPDFError) as error:
            return csa_contract_error(error)
        item = csa_purchase_admin_queryset().get(pk=item.pk)
        return no_store(Response({"data": admin_csa_purchase_payload(item)}))

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        serializer = CSAPurchaseRequestRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = reject_csa_purchase_request(
                request_id=pk,
                reason=serializer.validated_data["rejection_reason"],
                actor=request.user,
            )
        except CSAPurchaseError as error:
            return csa_purchase_error(error)
        item = csa_purchase_admin_queryset().get(pk=item.pk)
        return no_store(Response({"data": admin_csa_purchase_payload(item)}))

    @action(detail=True, methods=["post"], url_path="mark-refunded")
    def mark_refunded(self, request, pk=None):
        serializer = CSAPurchaseRequestRefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = mark_csa_purchase_request_refunded(
                request_id=pk,
                actor=request.user,
                note=serializer.validated_data["refund_note"],
            )
        except CSAPurchaseError as error:
            return csa_purchase_error(error)
        item = csa_purchase_admin_queryset().get(pk=item.pk)
        return no_store(Response({"data": admin_csa_purchase_payload(item)}))


class AdminCSAPaymentConfirmView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, request_id, sequence):
        serializer = CSAPaymentConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            confirm_csa_payment(
                request_id=request_id,
                installment_sequence=sequence,
                amount_paid=serializer.validated_data["amount_paid"],
                actor=request.user,
                note=serializer.validated_data["note"],
            )
        except CSAPaymentError as error:
            return csa_purchase_error(error)
        except CSAContractPDFError as error:
            return csa_contract_error(error)
        item = csa_purchase_admin_queryset().get(pk=request_id)
        return no_store(Response({"data": admin_csa_purchase_payload(item)}))


class AdminCSAPaymentRejectView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, request_id, sequence):
        serializer = CSAPaymentRejectSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reject_csa_payment(
                request_id=request_id,
                installment_sequence=sequence,
                reason=serializer.validated_data["rejection_reason"],
                actor=request.user,
            )
        except CSAPaymentError as error:
            return csa_purchase_error(error)
        item = csa_purchase_admin_queryset().get(pk=request_id)
        return no_store(Response({"data": admin_csa_purchase_payload(item)}))


class AdminCSACustomerViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminCSACustomerQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        refresh_membership_statuses()
        queryset = customer_queryset().order_by("name", "id")
        data = query.validated_data
        if search := data["search"].strip():
            queryset = queryset.filter(Q(name__icontains=search) | Q(phone_e164__icontains=search))
        if data["linked"] == "linked":
            queryset = queryset.filter(user__isnull=False)
        elif data["linked"] == "managed":
            queryset = queryset.filter(user__isnull=True)
        if membership_status := data.get("membership_status"):
            queryset = queryset.filter(memberships__status=membership_status).distinct()
        items, meta = paginate(queryset, data["page"], data["page_size"])
        return no_store(
            Response({"data": [customer_payload(item) for item in items], "meta": meta})
        )

    def create(self, request):
        serializer = CSACustomerCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            item = create_supported_managed_csa_customer(
                name=data.get("name"),
                phone=data.get("phone"),
                province_code=data.get("province_code", ""),
                ward_code=data.get("ward_code", ""),
                address=data.get("address", ""),
            )
        except CSACustomerError as error:
            return csa_customer_error(error)
        return no_store(
            Response(
                {"data": customer_payload(customer_queryset().get(pk=item.pk))},
                status=status.HTTP_201_CREATED,
            )
        )

    def retrieve(self, request, pk=None):
        refresh_membership_statuses()
        item = get_object_or_404(customer_queryset(), pk=pk)
        return no_store(Response({"data": customer_payload(item, include_memberships=True)}))

    @action(detail=True, methods=["post"], url_path="link-user")
    def link_user(self, request, pk=None):
        serializer = CSACustomerLinkUserSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = get_object_or_404(CSACustomer, pk=pk)
        try:
            user = User.objects.get(pk=serializer.validated_data["user_id"])
            customer = link_csa_customer_to_user(customer=customer, user=user, actor=request.user)
        except User.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except CSACustomerError as error:
            return csa_customer_error(error)
        return no_store(
            Response({"data": customer_payload(customer_queryset().get(pk=customer.pk))})
        )


class AdminMembershipViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminMembershipQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        refresh_membership_statuses()
        queryset = membership_queryset()
        if search := query.validated_data["search"].strip():
            queryset = queryset.filter(
                Q(customer__user__email__icontains=search)
                | Q(customer__name__icontains=search)
                | Q(customer__phone_e164__icontains=search)
                | Q(farmbrite_order_reference__icontains=search)
            )
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(
            Response(
                {
                    "data": [
                        membership_payload(
                            item,
                            locale=query.validated_data["locale"],
                            include_user=True,
                            admin=True,
                        )
                        for item in items
                    ],
                    "meta": meta,
                }
            )
        )

    def create(self, request):
        locale = requested_locale(request)
        serializer = MembershipAssignSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = assign_membership_with_contract(actor=request.user, **serializer.validated_data)
        except (User.DoesNotExist, CSACustomer.DoesNotExist):
            return Response(status=status.HTTP_404_NOT_FOUND)
        except PriceOptionTotalInvalid as error:
            raise ValidationError({"price_option_id": str(error)}) from error
        except MembershipError as error:
            return membership_error(error)
        except CSACustomerError as error:
            return membership_error(error)
        except (CSAContractError, CSAContractPDFError) as error:
            return csa_contract_error(error)
        result = membership_payload(
            membership_queryset().get(pk=item.pk),
            locale=locale,
            include_user=True,
            admin=True,
        )
        return no_store(
            Response(
                {"data": result},
                status=status.HTTP_201_CREATED,
            )
        )

    @transaction.atomic
    def retrieve(self, request, pk=None):
        locale = requested_locale(request)
        item = get_object_or_404(membership_queryset(), pk=pk)
        ensure_due_cycles(item)
        item = (
            membership_queryset()
            .prefetch_related("cycles__quotas__product_snapshot")
            .get(pk=item.pk)
        )
        data = membership_payload(
            item, locale=locale, include_quota=True, include_user=True, admin=True
        )
        data["cycles"] = cycle_payload(item, locale=locale)
        return no_store(Response({"data": data}))

    @action(detail=True, methods=["get"], url_path="contract")
    def contract_detail(self, request, pk=None):
        contract = contract_queryset().filter(membership_id=pk).first()
        if contract is None:
            return no_store(Response(status=status.HTTP_404_NOT_FOUND))
        return no_store(Response({"data": contract_payload(contract, admin=True)}))

    @action(detail=True, methods=["get"], url_path="contract/pdf")
    def contract_pdf(self, request, pk=None):
        try:
            locale, version_id = validated_contract_pdf_query(request.query_params)
        except CSAContractAccessError as error:
            return csa_contract_access_error(error)
        contract = contract_queryset().filter(membership_id=pk).first()
        if contract is None:
            return no_store(Response(status=status.HTTP_404_NOT_FOUND))
        return contract_pdf_response(contract, locale=locale, version_id=version_id)

    @action(detail=True, methods=["post"], url_path="revoke")
    def revoke(self, request, pk=None):
        locale = requested_locale(request)
        serializer = MembershipRevokeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = revoke_membership(
                membership_id=pk,
                reason=serializer.validated_data["reason"],
                actor=request.user,
            )
        except Membership.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except MembershipNotRevocable as error:
            return membership_error(error)
        except CSAContractPDFError as error:
            return csa_contract_error(error)
        item = membership_queryset().get(pk=item.pk)
        return no_store(
            Response(
                {
                    "data": membership_payload(
                        item,
                        locale=locale,
                        include_user=True,
                        admin=True,
                    )
                }
            )
        )

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        query = LocalePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        membership = get_object_or_404(Membership, pk=pk)
        items, meta = paginate(
            usage_queryset().filter(membership=membership), query.validated_data["page"]
        )
        return no_store(
            Response(
                {
                    "data": [
                        usage_payload(
                            item,
                            locale=query.validated_data["locale"],
                            include_allocations=True,
                            admin=True,
                        )
                        for item in items
                    ],
                    "meta": meta,
                }
            )
        )


class AdminUsageViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def create(self, request):
        locale = requested_locale(request)
        serializer = UsageCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, created = create_usage_order(actor=request.user, **serializer.validated_data)
        except Membership.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except MembershipError as error:
            return membership_error(error)
        return no_store(
            Response(
                {
                    "data": usage_payload(
                        usage_queryset().get(pk=item.pk),
                        locale=locale,
                        include_allocations=True,
                        admin=True,
                    ),
                    "meta": {"idempotent_replay": not created},
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )

    def retrieve(self, request, pk=None):
        locale = requested_locale(request)
        return no_store(
            Response(
                {
                    "data": usage_payload(
                        get_object_or_404(usage_queryset(), pk=pk),
                        locale=locale,
                        include_allocations=True,
                        admin=True,
                    )
                }
            )
        )

    @action(detail=True, methods=["post"])
    def undo(self, request, pk=None):
        locale = requested_locale(request)
        serializer = UsageUndoSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = undo_usage_order(order_id=pk, actor=request.user, **serializer.validated_data)
        except MembershipUsageOrder.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except MembershipError as error:
            return membership_error(error)
        return no_store(
            Response(
                {
                    "data": usage_payload(
                        usage_queryset().get(pk=item.pk),
                        locale=locale,
                        include_allocations=True,
                        admin=True,
                    )
                }
            )
        )
