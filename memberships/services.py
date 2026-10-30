import calendar
from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from commerce.models import ShippingAddress
from commerce.shipping import active_shipping_area
from identity.models import AuditLog, User
from members.administrative_units import province_exists, ward_choices
from memberships.contract_pdfs import (
    delete_contract_documents,
    generate_revoked_contract_documents,
    get_contract_pdf_storage,
)
from memberships.contract_versions import issue_contract_version
from memberships.contracts import (
    build_manual_contract_snapshot,
    create_manual_contract,
)
from memberships.models import (
    CSAContract,
    CSAContractStatus,
    CSAContractVersionReason,
    CSACustomer,
    CSAPurchasePayment,
    CSAPurchaseRequest,
    Membership,
    MembershipCycle,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackagePriceOption,
    MembershipPackageTranslation,
    MembershipPaymentSettings,
    MembershipProductSnapshot,
    MembershipStatus,
    MembershipUsageAllocation,
    MembershipUsageLine,
    MembershipUsageOrder,
    MembershipUsageReversal,
    Product,
    ProductTranslation,
    QuotaPolicy,
    UsageOrderStatus,
)
from memberships.payments import confirm_locked_csa_payment, paid_cycle_count, payment_due_at
from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone
from memberships.pricing import calculate_total_price_vnd


class MembershipError(ValueError):
    code = "invalid_membership_operation"


class PackageUnavailable(MembershipError):
    code = "package_unavailable"


class PriceOptionUnavailable(MembershipError):
    code = "price_option_unavailable"


class ActiveMembershipExists(MembershipError):
    code = "active_membership_exists"


class DuplicateFarmbriteOrderReference(MembershipError):
    code = "duplicate_farmbrite_order_reference"


class FarmbriteOrderReferenceRequired(MembershipError):
    code = "farmbrite_order_reference_required"


class StartDateInPast(MembershipError):
    code = "start_date_in_past"


class PriceOptionConflict(MembershipError):
    code = "price_option_conflict"


class PriceOptionTotalInvalid(MembershipError):
    code = "price_option_total_invalid"


class QuotaError(MembershipError):
    code = "invalid_quota_usage"


class ProductNotInMembership(QuotaError):
    code = "product_not_in_membership"


class InsufficientQuota(QuotaError):
    code = "insufficient_quota"


class UsageIdempotencyConflict(QuotaError):
    code = "usage_idempotency_conflict"


class UsageUndoConflict(QuotaError):
    code = "usage_undo_conflict"


class MembershipNotRevocable(MembershipError):
    code = "membership_not_revocable"


class MembershipPaymentSettingsUnavailable(MembershipError):
    code = "membership_payment_settings_unavailable"


class UsageIntegrityConflict(QuotaError):
    code = "usage_integrity_conflict"


class CSACustomerError(ValueError):
    code = "invalid_csa_customer"


class DuplicateCSACustomerUser(CSACustomerError):
    code = "csa_customer_user_exists"


class DuplicateCSACustomerPhone(CSACustomerError):
    code = "csa_customer_phone_exists"


class CSACustomerPhoneInvalid(CSACustomerError):
    code = "invalid_csa_customer_phone"


class CSACustomerPhoneRequired(CSACustomerError):
    code = "csa_customer_phone_required"


class CSACustomerNameRequired(CSACustomerError):
    code = "csa_customer_name_required"


class CSACustomerProvinceRequired(CSACustomerError):
    code = "csa_customer_province_required"


class CSACustomerWardRequired(CSACustomerError):
    code = "csa_customer_ward_required"


class CSACustomerAddressRequired(CSACustomerError):
    code = "csa_customer_address_required"


class CSACustomerAddressInvalid(CSACustomerError):
    code = "invalid_csa_customer_address"


class CSACustomerIncomplete(CSACustomerError):
    code = "csa_customer_incomplete"


class CSACustomerShippingAddressUnavailable(CSACustomerError):
    code = "csa_customer_shipping_address_unavailable"


class CSACustomerShippingAreaUnsupported(CSACustomerError):
    code = "csa_customer_shipping_area_unsupported"


class CSACustomerRequiredForMembership(CSACustomerError):
    code = "csa_customer_required_for_membership"


class MembershipOverlap(MembershipError):
    code = "membership_overlap"


def _required_name(name) -> str:
    value = (name or "").strip()
    if not value:
        raise CSACustomerNameRequired("Customer name is required")
    return value


def _normalized_required_phone(phone) -> str:
    if phone is None or not str(phone).strip():
        raise CSACustomerPhoneRequired("Phone number is required")
    try:
        return normalize_vietnam_phone(str(phone))
    except InvalidPhoneNumber as error:
        raise CSACustomerPhoneInvalid(str(error)) from error


def _validated_required_address(*, province_code="", ward_code="", address="") -> dict:
    province_code = (province_code or "").strip()
    ward_code = (ward_code or "").strip()
    address = (address or "").strip()
    if not province_code:
        raise CSACustomerProvinceRequired("Province is required")
    if not ward_code:
        raise CSACustomerWardRequired("Ward is required")
    if not address:
        raise CSACustomerAddressRequired("Detailed address is required")
    if not province_exists(province_code):
        raise CSACustomerAddressInvalid("Province code is invalid")
    valid_wards = {code for code, _name in ward_choices(province_code, "vi")}
    if ward_code not in valid_wards:
        raise CSACustomerAddressInvalid("Ward code does not belong to province")
    return {
        "province_code": province_code,
        "ward_code": ward_code,
        "address": address,
    }


def _validated_supported_address(*, province_code="", ward_code="", address="") -> dict:
    delivery_address = _validated_required_address(
        province_code=province_code,
        ward_code=ward_code,
        address=address,
    )
    if (
        active_shipping_area(
            province_code=delivery_address["province_code"],
            ward_code=delivery_address["ward_code"],
        )
        is None
    ):
        raise CSACustomerShippingAreaUnsupported("Shipping area is not supported")
    return delivery_address


@transaction.atomic
def create_csa_customer_for_user(
    *, user, name=None, phone=None, province_code="", ward_code="", address=""
) -> CSACustomer:
    user = User.objects.select_for_update().get(pk=user.pk)
    if CSACustomer.objects.select_for_update().filter(user=user).exists():
        raise DuplicateCSACustomerUser("User already has a CSA customer")
    customer_name = _required_name(name)
    normalized_phone = _normalized_required_phone(phone)
    delivery_address = _validated_required_address(
        province_code=province_code,
        ward_code=ward_code,
        address=address,
    )
    try:
        with transaction.atomic():
            return CSACustomer.objects.create(
                user=user,
                name=customer_name,
                phone_e164=normalized_phone,
                **delivery_address,
            )
    except IntegrityError as error:
        if normalized_phone and CSACustomer.objects.filter(phone_e164=normalized_phone).exists():
            raise DuplicateCSACustomerPhone(
                "Phone number already belongs to a CSA customer"
            ) from error
        raise DuplicateCSACustomerUser("User already has a CSA customer") from error


def _create_managed_csa_customer(
    *, customer_name, normalized_phone, delivery_address
) -> CSACustomer:
    try:
        with transaction.atomic():
            return CSACustomer.objects.create(
                name=customer_name,
                phone_e164=normalized_phone,
                **delivery_address,
            )
    except IntegrityError as error:
        raise DuplicateCSACustomerPhone("Phone number already belongs to a CSA customer") from error


@transaction.atomic
def create_managed_csa_customer(
    *, name, phone, province_code="", ward_code="", address=""
) -> CSACustomer:
    return _create_managed_csa_customer(
        customer_name=_required_name(name),
        normalized_phone=_normalized_required_phone(phone),
        delivery_address=_validated_required_address(
            province_code=province_code,
            ward_code=ward_code,
            address=address,
        ),
    )


@transaction.atomic
def create_supported_managed_csa_customer(
    *, name, phone, province_code="", ward_code="", address=""
) -> CSACustomer:
    return _create_managed_csa_customer(
        customer_name=_required_name(name),
        normalized_phone=_normalized_required_phone(phone),
        delivery_address=_validated_supported_address(
            province_code=province_code,
            ward_code=ward_code,
            address=address,
        ),
    )


@transaction.atomic
def update_csa_customer(
    *, customer, name=..., phone=..., province_code=..., ward_code=..., address=...
) -> CSACustomer:
    customer = CSACustomer.objects.select_for_update().get(pk=customer.pk)
    if name is not ...:
        customer.name = _required_name(name)
    if phone is not ...:
        customer.phone_e164 = _normalized_required_phone(phone)
    if (
        province_code is not ...
        and (province_code or "").strip()
        and (province_code or "").strip() != customer.province_code
        and ward_code is ...
    ):
        raise CSACustomerWardRequired("Ward is required when province changes")
    delivery_address = _validated_required_address(
        province_code=customer.province_code if province_code is ... else province_code,
        ward_code=customer.ward_code if ward_code is ... else ward_code,
        address=customer.address if address is ... else address,
    )
    for field, value in delivery_address.items():
        setattr(customer, field, value)
    try:
        with transaction.atomic():
            customer.save(
                update_fields=[
                    "name",
                    "phone_e164",
                    "province_code",
                    "ward_code",
                    "address",
                    "updated_at",
                ]
            )
    except IntegrityError as error:
        raise DuplicateCSACustomerPhone("Phone number already belongs to a CSA customer") from error
    return customer


def ensure_csa_customer_complete(customer: CSACustomer) -> None:
    try:
        _required_name(customer.name)
        _normalized_required_phone(customer.phone_e164)
        _validated_required_address(
            province_code=customer.province_code,
            ward_code=customer.ward_code,
            address=customer.address,
        )
    except CSACustomerError as error:
        raise CSACustomerIncomplete(
            "Complete the CSA customer before assigning a package"
        ) from error


@transaction.atomic
def link_csa_customer_to_user(*, customer, user, actor) -> CSACustomer:
    user = User.objects.select_for_update().get(pk=user.pk)
    customer = CSACustomer.objects.select_for_update().get(pk=customer.pk)
    if customer.user_id:
        raise DuplicateCSACustomerUser("CSA customer is already linked to a user")
    if CSACustomer.objects.select_for_update().filter(user=user).exists():
        raise DuplicateCSACustomerUser("User already has a CSA customer")
    try:
        shipping_address = ShippingAddress.objects.select_for_update().get(user=user)
    except ShippingAddress.DoesNotExist as error:
        raise CSACustomerShippingAddressUnavailable(
            "User does not have a supported shipping address"
        ) from error
    if (
        active_shipping_area(
            province_code=shipping_address.province_code,
            ward_code=shipping_address.ward_code,
        )
        is None
    ):
        raise CSACustomerShippingAddressUnavailable(
            "User does not have a supported shipping address"
        )
    try:
        delivery_address = _validated_required_address(
            province_code=shipping_address.province_code,
            ward_code=shipping_address.ward_code,
            address=shipping_address.address,
        )
    except CSACustomerError as error:
        raise CSACustomerShippingAddressUnavailable(
            "User does not have a supported shipping address"
        ) from error
    customer.user = user
    for field, value in delivery_address.items():
        setattr(customer, field, value)
    try:
        with transaction.atomic():
            customer.save(
                update_fields=[
                    "user",
                    "province_code",
                    "ward_code",
                    "address",
                    "updated_at",
                ]
            )
    except IntegrityError as error:
        raise DuplicateCSACustomerUser("User already has a CSA customer") from error
    AuditLog.objects.create(
        user=actor,
        action="csa_customer.link_user",
        context={"customer_id": str(customer.pk), "linked_user_id": str(user.pk)},
    )
    return customer


def add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def normalize_farmbrite_order_reference(value: str) -> tuple[str, str]:
    reference = value.strip()
    if not reference:
        raise FarmbriteOrderReferenceRequired("Farmbrite order reference is required")
    if len(reference) > 120:
        raise FarmbriteOrderReferenceRequired("Farmbrite order reference is too long")
    return reference, reference.casefold()


@transaction.atomic
def sync_package_price_options(*, package: MembershipPackage, options: list[dict]) -> None:
    """Replace a package's option set while retaining any option used by history."""
    package = MembershipPackage.objects.select_for_update().get(pk=package.pk)
    normalized_options = [dict(item) for item in options]
    existing = {
        str(option.pk): option
        for option in MembershipPackagePriceOption.objects.select_for_update()
        .filter(package=package)
        .order_by("id")
    }
    supplied_ids = {str(item["id"]) for item in normalized_options if item.get("id")}
    if unknown := supplied_ids - set(existing):
        raise PriceOptionConflict(f"Price options do not belong to package: {sorted(unknown)}")
    if not any(item["is_active"] for item in normalized_options):
        raise PriceOptionConflict("A package requires at least one active price option")
    active_durations = [item["duration_months"] for item in normalized_options if item["is_active"]]
    if len(active_durations) != len(set(active_durations)):
        raise PriceOptionConflict(
            "A package cannot have two active price options with the same duration"
        )

    for item in normalized_options:
        try:
            calculate_total_price_vnd(
                duration_months=item["duration_months"],
                monthly_price_vnd=item["monthly_price_vnd"],
            )
        except ValueError as error:
            raise PriceOptionTotalInvalid(str(error)) from error
        if not item.get("id"):
            continue
        option = existing[str(item["id"])]
        if option.memberships.exists():
            if (
                option.duration_months != item["duration_months"]
                or option.monthly_price_vnd != item["monthly_price_vnd"]
            ):
                raise PriceOptionConflict(
                    "A used price option's duration and monthly price are immutable"
                )
            if not option.is_active and item["is_active"]:
                raise PriceOptionConflict("A historical used price option cannot be reactivated")

    for option_id, option in existing.items():
        if option_id in supplied_ids:
            continue
        if option.memberships.exists():
            option.is_active = False
            option.active_duration_key = None
            option.save(update_fields=["is_active", "active_duration_key", "updated_at"])
        else:
            option.delete()

    desired_by_id = {str(item["id"]): item for item in normalized_options if item.get("id")}
    for option_id, desired in desired_by_id.items():
        option = existing[option_id]
        desired_key = desired["duration_months"] if desired["is_active"] else None
        if option.active_duration_key is not None and (option.active_duration_key != desired_key):
            option.is_active = False
            option.active_duration_key = None
            option.save(update_fields=["is_active", "active_duration_key", "updated_at"])

    for item in normalized_options:
        option_id = str(item.pop("id")) if item.get("id") else None
        item["active_duration_key"] = item["duration_months"] if item["is_active"] else None
        if option_id:
            option = existing[option_id]
            for field, value in item.items():
                setattr(option, field, value)
            option.save()
        else:
            MembershipPackagePriceOption.objects.create(package=package, **item)

    if not package.price_options.filter(is_active=True).exists():
        raise PriceOptionConflict("The last active price option cannot be deactivated or removed")


@transaction.atomic
def assign_membership_to_customer(
    *,
    customer_id,
    package_id,
    price_option_id,
    start_date: date | None = None,
    farmbrite_order_reference: str,
    actor,
    purchase_request: CSAPurchaseRequest | None = None,
):
    reference, reference_key = normalize_farmbrite_order_reference(farmbrite_order_reference)
    customer = CSACustomer.objects.select_for_update().get(pk=customer_id)
    ensure_csa_customer_complete(customer)
    today = timezone.localdate()
    Membership.objects.filter(customer=customer, current_slot=True, end_date__lte=today).update(
        status=MembershipStatus.ENDED, current_slot=None
    )
    existing_memberships = list(
        Membership.objects.select_for_update()
        .filter(customer=customer)
        .exclude(status=MembershipStatus.REVOKED)
        .order_by("start_date", "id")
    )
    if start_date is None:
        future_boundaries = [
            membership.end_date
            for membership in existing_memberships
            if membership.end_date > today
        ]
        start_date = max([today, *future_boundaries])
    if start_date < today:
        raise StartDateInPast("Membership start date cannot be in the past")

    if purchase_request is None:
        try:
            package = MembershipPackage.objects.select_for_update().get(
                pk=package_id, is_active=True
            )
        except MembershipPackage.DoesNotExist as error:
            raise PackageUnavailable("Package does not exist or is inactive") from error
        try:
            price_option = MembershipPackagePriceOption.objects.select_for_update().get(
                pk=price_option_id, package=package, is_active=True
            )
        except MembershipPackagePriceOption.DoesNotExist as error:
            raise PriceOptionUnavailable(
                "Price option must be active and belong to the selected package"
            ) from error
    else:
        try:
            package = MembershipPackage.objects.select_for_update().get(pk=package_id)
        except MembershipPackage.DoesNotExist as error:
            raise PackageUnavailable("Purchase request package does not exist") from error
        try:
            price_option = MembershipPackagePriceOption.objects.select_for_update().get(
                pk=price_option_id, package=package
            )
        except MembershipPackagePriceOption.DoesNotExist as error:
            raise PriceOptionUnavailable(
                "Purchase request price option must belong to the selected package"
            ) from error

    if purchase_request is None:
        duration_months = price_option.duration_months
        monthly_price_vnd = price_option.monthly_price_vnd
        try:
            total_price_vnd = calculate_total_price_vnd(
                duration_months=duration_months,
                monthly_price_vnd=monthly_price_vnd,
            )
        except ValueError as error:
            raise PriceOptionTotalInvalid(str(error)) from error
        package_name_snapshot = package.name
    else:
        if (
            purchase_request.package_id != package.pk
            or purchase_request.price_option_id != price_option.pk
            or purchase_request.currency_snapshot != "VND"
            or purchase_request.duration_months_snapshot <= 0
            or purchase_request.amount_snapshot <= 0
            or not purchase_request.package_name_snapshot.strip()
            or (
                purchase_request.payment_plan_id is None
                and purchase_request.amount_snapshot % purchase_request.duration_months_snapshot
            )
        ):
            raise PriceOptionTotalInvalid("Purchase request pricing snapshot is invalid")
        duration_months = purchase_request.duration_months_snapshot
        total_price_vnd = purchase_request.amount_snapshot
        monthly_price_vnd = (
            purchase_request.price_option_monthly_amount_snapshot
            or max(1, total_price_vnd // duration_months)
            if purchase_request.payment_plan_id
            else total_price_vnd // duration_months
        )
        package_name_snapshot = purchase_request.package_name_snapshot

    end_date = add_months(start_date, duration_months)
    if any(
        membership.start_date < end_date and membership.end_date > start_date
        for membership in existing_memberships
    ):
        raise MembershipOverlap("Membership dates overlap an existing membership")

    items = list(
        MembershipPackageItem.objects.select_for_update().filter(package=package).order_by("id")
    )
    if not items:
        raise PackageUnavailable("Package has no quota items")
    product_ids = [item.product_id for item in items]
    products = {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=product_ids).order_by("id")
    }
    if len(products) != len(product_ids) or any(
        not products[item.product_id].is_active for item in items
    ):
        raise PackageUnavailable("Package contains an unavailable product")
    package_translations = {
        item.locale: item
        for item in MembershipPackageTranslation.objects.select_for_update()
        .filter(package=package)
        .order_by("locale", "id")
    }
    package_vi = package_translations.get("vi")
    package_en = package_translations.get("en")
    product_translations = {
        (item.product_id, item.locale): item
        for item in ProductTranslation.objects.select_for_update()
        .filter(product_id__in=product_ids)
        .order_by("product_id", "locale", "id")
    }

    try:
        with transaction.atomic():
            membership = Membership.objects.create(
                customer=customer,
                package=package,
                price_option=price_option,
                package_name_snapshot=package_name_snapshot,
                package_description_snapshot=package.description,
                package_name_vi_snapshot=(
                    package_name_snapshot
                    if purchase_request
                    else package_vi.name
                    if package_vi
                    else package.name
                ),
                package_name_en_snapshot=(package_en.name if package_en else ""),
                package_description_vi_snapshot=(
                    package_vi.description if package_vi else package.description
                ),
                package_description_en_snapshot=(package_en.description if package_en else ""),
                monthly_price_vnd_snapshot=monthly_price_vnd,
                total_price_vnd_snapshot=total_price_vnd,
                pricing_basis=(
                    "payment_plan"
                    if purchase_request and purchase_request.payment_plan_id
                    else "monthly"
                ),
                duration_months_snapshot=duration_months,
                quota_policy_snapshot=package.quota_policy,
                farmbrite_order_reference=reference,
                farmbrite_order_reference_key=reference_key,
                start_date=start_date,
                end_date=end_date,
                status=(
                    MembershipStatus.SCHEDULED if start_date > today else MembershipStatus.ACTIVE
                ),
                current_slot=True,
                activated_by=actor,
            )
    except IntegrityError as error:
        if Membership.objects.filter(farmbrite_order_reference_key=reference_key).exists():
            raise DuplicateFarmbriteOrderReference(
                "Farmbrite order reference has already been assigned"
            ) from error
        raise

    MembershipProductSnapshot.objects.bulk_create(
        [
            MembershipProductSnapshot(
                membership=membership,
                product=products[item.product_id],
                product_name=products[item.product_id].name,
                product_name_vi_snapshot=(
                    product_translations[(item.product_id, "vi")].name
                    if (item.product_id, "vi") in product_translations
                    else products[item.product_id].name
                ),
                product_name_en_snapshot=(
                    product_translations[(item.product_id, "en")].name
                    if (item.product_id, "en") in product_translations
                    else ""
                ),
                unit_size=products[item.product_id].unit_size,
                unit_label_vi=products[item.product_id].unit_label_vi,
                unit_label_en=products[item.product_id].unit_label_en,
                quota_units_per_cycle=item.quota_units,
            )
            for item in items
        ]
    )
    if start_date <= today and not (purchase_request and purchase_request.payment_plan_id):
        ensure_due_cycles(membership, as_of=today)
    return membership


@transaction.atomic
def assign_membership(
    *,
    package_id,
    price_option_id,
    start_date: date | None = None,
    farmbrite_order_reference: str,
    actor,
    customer_id=None,
    purchase_request: CSAPurchaseRequest | None = None,
):
    """Assign by canonical customer ID only; assignment never creates customers."""
    if not customer_id:
        raise CSACustomerRequiredForMembership("A CSA customer is required")
    return assign_membership_to_customer(
        customer_id=customer_id,
        package_id=package_id,
        price_option_id=price_option_id,
        start_date=start_date,
        farmbrite_order_reference=farmbrite_order_reference,
        actor=actor,
        purchase_request=purchase_request,
    )


def _locked_manual_payment_settings() -> MembershipPaymentSettings:
    try:
        payment_settings = MembershipPaymentSettings.objects.select_for_update().get(pk="default")
    except MembershipPaymentSettings.DoesNotExist as error:
        raise MembershipPaymentSettingsUnavailable("CSA payment settings are missing") from error
    try:
        payment_settings.full_clean(validate_unique=False, validate_constraints=False)
    except ValidationError as error:
        raise MembershipPaymentSettingsUnavailable("CSA payment settings are invalid") from error
    return payment_settings


def assign_membership_with_contract(
    *,
    package_id,
    price_option_id,
    start_date: date | None = None,
    farmbrite_order_reference: str,
    actor,
    customer_id=None,
):
    """Admin assignment boundary: Membership, Contract, and PDFs succeed together."""
    storage = get_contract_pdf_storage()
    documents = None
    try:
        with transaction.atomic():
            if not customer_id:
                raise CSACustomerRequiredForMembership("A CSA customer is required")
            customer = (
                CSACustomer.objects.select_for_update().select_related("user").get(pk=customer_id)
            )
            ensure_csa_customer_complete(customer)
            membership = assign_membership(
                customer_id=customer.pk,
                package_id=package_id,
                price_option_id=price_option_id,
                start_date=start_date,
                farmbrite_order_reference=farmbrite_order_reference,
                actor=actor,
            )
            manual_payment = CSAPurchasePayment.objects.create(
                membership=membership,
                installment_sequence=1,
                amount_due=membership.total_price_vnd_snapshot,
                cycle_count=membership.duration_months_snapshot,
                due_at=payment_due_at(membership.start_date, 0),
            )
            confirm_locked_csa_payment(
                payment=manual_payment,
                amount_paid=manual_payment.amount_due,
                actor=actor,
            )
            payment_settings = _locked_manual_payment_settings()
            snapshot = build_manual_contract_snapshot(
                customer=customer,
                membership=membership,
                payment_settings=payment_settings,
            )
            contract = create_manual_contract(membership_id=membership.pk, snapshot=snapshot)
            AuditLog.objects.create(
                user=actor,
                action="membership.assigned",
                context={
                    "customer_id": str(customer.pk),
                    "membership_id": str(membership.pk),
                    "contract_id": str(contract.pk),
                    "contract_reference_code": contract.reference_code,
                },
            )
            _version, documents = issue_contract_version(
                contract_id=contract.pk,
                reason=CSAContractVersionReason.ISSUED,
                actor=actor,
                storage=storage,
            )
        return membership
    except Exception:
        if documents is not None:
            delete_contract_documents(documents, storage=storage)
        raise


def _create_cycle(membership: Membership, sequence: int) -> MembershipCycle:
    start = add_months(membership.start_date, sequence - 1)
    end = min(add_months(membership.start_date, sequence), membership.end_date)
    cycle = MembershipCycle.objects.create(
        membership=membership, sequence=sequence, start_date=start, end_date=end
    )
    MembershipCycleQuota.objects.bulk_create(
        [
            MembershipCycleQuota(
                cycle=cycle,
                product_snapshot=snapshot,
                granted_units=snapshot.quota_units_per_cycle,
                remaining_units=snapshot.quota_units_per_cycle,
            )
            for snapshot in membership.product_snapshots.all()
        ]
    )
    return cycle


@transaction.atomic
def sync_membership_status(membership: Membership, *, as_of: date | None = None) -> Membership:
    as_of = as_of or timezone.localdate()
    membership = Membership.objects.select_for_update().get(pk=membership.pk)
    if membership.status == MembershipStatus.REVOKED:
        return membership
    if membership.status == MembershipStatus.ENDED or membership.end_date <= as_of:
        expected_status = MembershipStatus.ENDED
        expected_slot = None
    elif membership.start_date <= as_of:
        expected_status = MembershipStatus.ACTIVE
        expected_slot = True
    else:
        expected_status = MembershipStatus.SCHEDULED
        expected_slot = True
    if membership.status != expected_status or membership.current_slot != expected_slot:
        membership.status = expected_status
        membership.current_slot = expected_slot
        membership.save(update_fields=["status", "current_slot", "updated_at"])
    return membership


@transaction.atomic
def refresh_membership_statuses(*, as_of: date | None = None) -> None:
    as_of = as_of or timezone.localdate()
    updated_at = timezone.now()
    Membership.objects.filter(current_slot=True, end_date__lte=as_of).exclude(
        status=MembershipStatus.REVOKED
    ).update(status=MembershipStatus.ENDED, current_slot=None, updated_at=updated_at)
    Membership.objects.filter(
        current_slot=True,
        status=MembershipStatus.SCHEDULED,
        start_date__lte=as_of,
        end_date__gt=as_of,
    ).update(status=MembershipStatus.ACTIVE, updated_at=updated_at)


@transaction.atomic
def ensure_due_cycles(
    membership: Membership, *, as_of: date | None = None
) -> list[MembershipCycle]:
    as_of = as_of or timezone.localdate()
    membership = sync_membership_status(membership, as_of=as_of)
    if membership.status == MembershipStatus.REVOKED:
        return []
    due = 0
    for sequence in range(1, membership.duration_months_snapshot + 1):
        if add_months(membership.start_date, sequence - 1) <= as_of:
            due = sequence
        else:
            break
    due = min(due, membership.duration_months_snapshot)
    plan_request_id = (
        CSAPurchaseRequest.objects.filter(membership=membership, payment_plan__isnull=False)
        .values_list("pk", flat=True)
        .first()
    )
    if plan_request_id is not None:
        paid = paid_cycle_count(request_id=plan_request_id)
        if paid > membership.duration_months_snapshot:
            raise QuotaError("Confirmed payment cycles exceed Membership duration")
        due = min(due, paid)
    existing = {item.sequence: item for item in membership.cycles.all()}
    created = []
    for sequence in range(1, due + 1):
        if sequence not in existing:
            created.append(_create_cycle(membership, sequence))
    if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE:
        MembershipCycleQuota.objects.filter(
            cycle__membership=membership,
            cycle__end_date__lte=as_of,
            remaining_units__gt=0,
        ).update(
            expired_units=F("expired_units") + F("remaining_units"),
            remaining_units=0,
        )
    return created


def _normalized_usage_lines(lines: list[dict]) -> list[tuple[str, int]]:
    normalized = sorted((str(item["product_id"]), int(item["units"])) for item in lines)
    if not normalized or any(units <= 0 for _product_id, units in normalized):
        raise QuotaError("Usage lines and positive units are required")
    if len({product_id for product_id, _units in normalized}) != len(normalized):
        raise QuotaError("Each product may appear only once")
    return normalized


def validate_usage_integrity(order: MembershipUsageOrder) -> None:
    membership = order.membership
    if order.customer_id != membership.customer_id:
        raise UsageIntegrityConflict("Usage order customer does not match membership customer")
    lines = list(
        order.lines.select_related("product_snapshot").prefetch_related(
            "allocations__cycle_quota__cycle"
        )
    )
    if not lines:
        raise UsageIntegrityConflict("Usage order has no lines")
    for line in lines:
        if line.product_snapshot.membership_id != order.membership_id:
            raise UsageIntegrityConflict("Usage line product does not belong to membership")
        allocations = list(line.allocations.all())
        if not allocations or sum(item.units for item in allocations) != line.units:
            raise UsageIntegrityConflict("Usage line allocations do not match deducted units")
        for allocation in allocations:
            quota = allocation.cycle_quota
            if (
                quota.cycle.membership_id != order.membership_id
                or quota.product_snapshot_id != line.product_snapshot_id
            ):
                raise UsageIntegrityConflict("Usage allocation does not match its line")


@transaction.atomic
def create_usage_order(*, membership_id, actor, idempotency_key, lines: list[dict], note=""):
    membership = (
        Membership.objects.select_for_update().select_related("customer").get(pk=membership_id)
    )
    today = timezone.localdate()
    membership = sync_membership_status(membership, as_of=today)
    if membership.status != MembershipStatus.ACTIVE or not (
        membership.start_date <= today < membership.end_date
    ):
        raise QuotaError("Membership is not active today")
    requested = _normalized_usage_lines(lines)
    existing = (
        MembershipUsageOrder.objects.filter(membership=membership, idempotency_key=idempotency_key)
        .prefetch_related("lines__product_snapshot")
        .first()
    )
    if existing:
        validate_usage_integrity(existing)
        existing_lines = sorted(
            (str(line.product_snapshot.product_id), line.units) for line in existing.lines.all()
        )
        if existing_lines != requested or existing.note != note.strip():
            raise UsageIdempotencyConflict(
                "Idempotency key was used with a different usage payload"
            )
        return existing, False
    ensure_due_cycles(membership, as_of=today)
    membership.refresh_from_db()
    if membership.status != MembershipStatus.ACTIVE or not (
        membership.start_date <= today < membership.end_date
    ):
        raise QuotaError("Membership is not active today")
    snapshots = {
        str(item.product_id): item
        for item in MembershipProductSnapshot.objects.filter(membership=membership)
    }
    for product_id, _units in requested:
        if product_id not in snapshots:
            raise ProductNotInMembership("Product is not included in this membership")
    quotas = list(
        MembershipCycleQuota.objects.select_for_update()
        .filter(
            cycle__membership=membership,
            product_snapshot__product_id__in=[product_id for product_id, _units in requested],
            remaining_units__gt=0,
        )
        .select_related("cycle", "product_snapshot")
        .order_by("cycle__sequence", "id")
    )
    if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE:
        quotas = [
            quota for quota in quotas if quota.cycle.start_date <= today < quota.cycle.end_date
        ]
    by_product = {}
    for quota in quotas:
        by_product.setdefault(str(quota.product_snapshot.product_id), []).append(quota)
    if any(
        sum(q.remaining_units for q in by_product.get(pid, [])) < units for pid, units in requested
    ):
        raise InsufficientQuota("Requested units exceed available quota")
    order = MembershipUsageOrder.objects.create(
        membership=membership,
        customer=membership.customer,
        idempotency_key=idempotency_key,
        note=note.strip(),
        created_by=actor,
    )
    for product_id, units in requested:
        line = MembershipUsageLine.objects.create(
            order=order, product_snapshot=snapshots[product_id], units=units
        )
        needed = units
        for quota in by_product[product_id]:
            allocated = min(needed, quota.remaining_units)
            if not allocated:
                continue
            quota.remaining_units -= allocated
            quota.save(update_fields=["remaining_units"])
            MembershipUsageAllocation.objects.create(line=line, cycle_quota=quota, units=allocated)
            needed -= allocated
            if not needed:
                break
    return order, True


@transaction.atomic
def undo_usage_order(*, order_id, actor, reason: str):
    if not reason.strip():
        raise UsageUndoConflict("An undo reason is required")
    membership_id = (
        MembershipUsageOrder.objects.only("membership_id").get(pk=order_id).membership_id
    )
    membership = Membership.objects.select_for_update().get(pk=membership_id)
    if membership.status == MembershipStatus.REVOKED:
        raise UsageUndoConflict("Usage cannot be undone for a revoked membership")
    order = MembershipUsageOrder.objects.select_for_update().get(pk=order_id)
    if (
        order.status != UsageOrderStatus.APPLIED
        or MembershipUsageReversal.objects.filter(order=order).exists()
    ):
        raise UsageUndoConflict("Usage order was already undone")
    validate_usage_integrity(order)
    allocations = list(
        MembershipUsageAllocation.objects.filter(line__order=order)
        .select_related("cycle_quota__cycle__membership")
        .order_by("id")
    )
    quota_ids = [item.cycle_quota_id for item in allocations]
    locked_quotas = {
        item.pk: item
        for item in MembershipCycleQuota.objects.select_for_update().filter(pk__in=quota_ids)
    }
    today = timezone.localdate()
    for allocation in allocations:
        quota = locked_quotas[allocation.cycle_quota_id]
        membership = quota.cycle.membership
        if membership.quota_policy_snapshot == QuotaPolicy.EXPIRE and quota.cycle.end_date <= today:
            quota.expired_units += allocation.units
            quota.save(update_fields=["expired_units"])
        else:
            quota.remaining_units += allocation.units
            quota.save(update_fields=["remaining_units"])
    MembershipUsageReversal.objects.create(order=order, actor=actor, reason=reason.strip())
    order.status = UsageOrderStatus.REVERSED
    order.reversed_at = timezone.now()
    order.save(update_fields=["status", "reversed_at"])
    return order


def revoke_membership(*, membership_id, reason: str, actor) -> Membership:
    """Revoke an active or scheduled membership. Sets status=REVOKED and clears current_slot."""
    if not reason.strip():
        raise MembershipNotRevocable("A revoke reason is required")
    storage = get_contract_pdf_storage()
    documents = None
    rejected_status = None
    try:
        with transaction.atomic():
            membership = Membership.objects.select_for_update().get(pk=membership_id)
            membership = sync_membership_status(membership, as_of=timezone.localdate())
            if membership.status not in {MembershipStatus.SCHEDULED, MembershipStatus.ACTIVE}:
                rejected_status = membership.status
            else:
                membership.status = MembershipStatus.REVOKED
                membership.current_slot = None
                membership.revoked_at = timezone.now()
                membership.revoked_by = actor
                membership.revoke_reason = reason.strip()
                membership.save(
                    update_fields=[
                        "status",
                        "current_slot",
                        "revoked_at",
                        "revoked_by",
                        "revoke_reason",
                        "updated_at",
                    ]
                )
                contract = (
                    CSAContract.objects.select_for_update().filter(membership=membership).first()
                )
                if contract is not None:
                    contract.status = CSAContractStatus.REVOKED
                    contract.revoked_at = membership.revoked_at
                    contract.revoked_by = actor
                    contract.revoked_by_name_snapshot = actor.name or actor.email
                    contract.revocation_reason = reason.strip()
                    contract.save(
                        update_fields=[
                            "status",
                            "revoked_at",
                            "revoked_by",
                            "revoked_by_name_snapshot",
                            "revocation_reason",
                            "updated_at",
                        ]
                    )
                    if contract.current_version_id:
                        _version, documents = issue_contract_version(
                            contract_id=contract.pk,
                            reason=CSAContractVersionReason.MEMBERSHIP_REVOKED,
                            actor=actor,
                            storage=storage,
                        )
                    elif contract.pdf_generated_at:
                        documents = generate_revoked_contract_documents(contract, storage=storage)
        if rejected_status is not None:
            raise MembershipNotRevocable(
                f"Membership cannot be revoked: current status is '{rejected_status}'"
            )
        return membership
    except Exception:
        if documents is not None:
            delete_contract_documents(documents, storage=storage)
        raise
