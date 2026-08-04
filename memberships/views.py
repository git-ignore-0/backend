from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.db.models.deletion import ProtectedError
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from content.permissions import IsWebsiteAdmin
from memberships.banks import BankDirectoryUnavailable, find_transfer_bank, get_transfer_banks
from memberships.models import (
    Membership,
    MembershipContentLocale,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackageTranslation,
    MembershipPaymentSettings,
    MembershipRequest,
    MembershipStatus,
    MembershipUsageOrder,
    Product,
    ProductTranslation,
)
from memberships.permissions import HasMembershipSelfScopes
from memberships.serializers import (
    AdminMembershipQuerySerializer,
    AdminPackageQuerySerializer,
    AdminProductQuerySerializer,
    AdminRequestQuerySerializer,
    ApproveSerializer,
    LocalePageQuerySerializer,
    LocaleQuerySerializer,
    MembershipRequestCreateSerializer,
    PackageWriteSerializer,
    PaymentSettingsWriteSerializer,
    ProductWriteSerializer,
    PublicPackageQuerySerializer,
    ReasonSerializer,
    RequestActionSerializer,
    UsageCreateSerializer,
    UsageUndoSerializer,
)
from memberships.services import (
    MembershipError,
    approve_request,
    create_membership_request,
    create_usage_order,
    direct_transfer_is_available,
    ensure_due_cycles,
    issue_payment_instruction,
    mark_contacted,
    refresh_membership_statuses,
    reject_request,
    return_to_payment_pending,
    submit_payment,
    undo_usage_order,
)

PAGE_SIZE = 30


def no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def paginate(queryset, page):
    total = queryset.count()
    start = (page - 1) * PAGE_SIZE
    return queryset[start : start + PAGE_SIZE], {
        "page": page,
        "page_size": PAGE_SIZE,
        "total": total,
    }


def membership_error(error):
    return no_store(Response({"errors": [{"code": error.code}]}, status=status.HTTP_409_CONFLICT))


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


def package_payload(item, *, public=False, locale=MembershipContentLocale.VI):
    content = _localized_content(item, locale, "description")
    data = {
        "id": str(item.pk),
        "name": content["name"],
        "description": content["description"],
        "upfront_price": str(item.upfront_price),
        "duration_months": item.duration_months,
        "quota_policy": item.quota_policy,
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
                        "product_translations": _translation_payloads(
                            package_item.product, "short_description"
                        )
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


def instruction_payload(item):
    return {
        "bank_bin": item.bank_bin,
        "bank_name": item.bank_name,
        "bank_code": item.bank_code,
        "account_number": item.account_number,
        "account_name": item.account_name,
        "amount": str(item.amount),
        "phone_snapshot": item.phone_snapshot,
        "transfer_content": item.transfer_content,
        "issued_at": item.issued_at.isoformat(),
    }


def _localized_snapshot(
    *, locale, name_vi, name_en, legacy_name, description_vi=None,
    description_en=None, legacy_description=None
):
    names = {
        MembershipContentLocale.VI: name_vi,
        MembershipContentLocale.EN: name_en,
    }
    selected_locale = next(
        (
            candidate
            for candidate in (
                locale,
                MembershipContentLocale.VI,
                MembershipContentLocale.EN,
            )
            if names[candidate]
        ),
        MembershipContentLocale.VI,
    )
    selected_name = names.get(selected_locale) or legacy_name
    result = {
        "name": selected_name,
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
            descriptions[selected_locale]
            if names.get(selected_locale)
            else legacy_description
        )
    return result


def request_payload(
    item, *, locale=MembershipContentLocale.VI, admin=False, include_events=False
):
    instruction = getattr(item, "payment_instruction", None)
    package_content = _localized_snapshot(
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
        "short_code": item.short_code,
        "status": item.status,
        "package": {
            "id": str(item.package_id),
            "name": package_content["name"],
            "description": package_content["description"],
            "requested_locale": locale,
            "content_locale": package_content["content_locale"],
            "is_fallback": package_content["is_fallback"],
            **(
                {"snapshot_complete": package_content["snapshot_complete"]}
                if admin
                else {}
            ),
            "upfront_price": str(item.upfront_price_snapshot),
            "duration_months": item.duration_months_snapshot,
            "quota_policy": item.quota_policy_snapshot,
            "items": [
                {
                    "product_id": str(snapshot.product_id),
                    "product_name": product_content["name"],
                    "requested_locale": locale,
                    "content_locale": product_content["content_locale"],
                    "is_fallback": product_content["is_fallback"],
                    **(
                        {"snapshot_complete": product_content["snapshot_complete"]}
                        if admin
                        else {}
                    ),
                    "unit_size": str(snapshot.unit_size),
                    "unit_label_vi": snapshot.unit_label_vi,
                    "unit_label_en": snapshot.unit_label_en,
                    "quota_units": snapshot.quota_units_per_cycle,
                }
                for snapshot in item.item_snapshots.all()
                for product_content in [
                    _localized_snapshot(
                        locale=locale,
                        name_vi=snapshot.product_name_vi_snapshot,
                        name_en=snapshot.product_name_en_snapshot,
                        legacy_name=snapshot.product_name,
                    )
                ]
            ],
        },
        "payment_instruction": instruction_payload(instruction) if instruction else None,
        "return_reason": item.return_reason,
        "rejection_reason": item.rejection_reason,
        "contacted_at": item.contacted_at.isoformat() if item.contacted_at else None,
        "payment_submitted_at": (
            item.payment_submitted_at.isoformat() if item.payment_submitted_at else None
        ),
        "returned_at": item.returned_at.isoformat() if item.returned_at else None,
        "approved_at": item.approved_at.isoformat() if item.approved_at else None,
        "rejected_at": item.rejected_at.isoformat() if item.rejected_at else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }
    if admin:
        data["user"] = {
            "id": str(item.user_id),
            "name": item.user.name,
            "email": item.user.email,
            "phone": item.phone_snapshot,
        }
    if include_events:
        data["events"] = [
            {
                "id": str(event.pk),
                "action": event.action,
                "actor": (
                    {"id": str(event.actor_id), "name": event.actor.name} if event.actor else None
                ),
                "reason": event.reason,
                "created_at": event.created_at.isoformat(),
            }
            for event in item.events.all()
        ]
    return data


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
        "user_id": str(item.user_id),
        "request_id": str(item.request_id),
        "package_id": str(item.package_id),
        "status": item.status,
        "package_name": content["name"],
        "package_description": content["description"],
        "requested_locale": locale,
        "content_locale": content["content_locale"],
        "is_fallback": content["is_fallback"],
        **({"snapshot_complete": content["snapshot_complete"]} if admin else {}),
        "upfront_price": str(item.upfront_price_snapshot),
        "duration_months": item.duration_months_snapshot,
        "quota_policy": item.quota_policy_snapshot,
        "start_date": item.start_date.isoformat(),
        "end_date": item.end_date.isoformat(),
        "activated_at": item.activated_at.isoformat(),
    }
    if include_quota:
        data["quota"] = quota_payload(item, locale=locale, admin=admin)
    if include_user:
        data["user"] = {
            "id": str(item.user_id),
            "name": item.user.name,
            "email": item.user.email,
            "phone": getattr(getattr(item.user, "member_profile", None), "phone_number", ""),
        }
    return data


def quota_payload(membership, *, locale=MembershipContentLocale.VI, admin=False):
    snapshots = membership.product_snapshots.all()
    balances = {
        str(row["product_snapshot_id"]): row["remaining"] or 0
        for row in MembershipCycleQuota.objects.filter(cycle__membership=membership)
        .values("product_snapshot_id")
        .annotate(remaining=Sum("remaining_units"))
    }
    return [
        {
            "product_id": str(snapshot.product_id),
            "product_name": product_content["name"],
            "requested_locale": locale,
            "content_locale": product_content["content_locale"],
            "is_fallback": product_content["is_fallback"],
            **(
                {"snapshot_complete": product_content["snapshot_complete"]}
                if admin
                else {}
            ),
            "unit_size": str(snapshot.unit_size),
            "unit_label_vi": snapshot.unit_label_vi,
            "unit_label_en": snapshot.unit_label_en,
            "quota_units_per_cycle": snapshot.quota_units_per_cycle,
            "remaining_units": balances.get(str(snapshot.pk), 0),
        }
        for snapshot in snapshots
        for product_content in [
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
                    "product_name": product_content["name"],
                    "requested_locale": locale,
                    "content_locale": product_content["content_locale"],
                    "is_fallback": product_content["is_fallback"],
                    "snapshot_complete": product_content["snapshot_complete"],
                    "granted_units": quota.granted_units,
                    "remaining_units": quota.remaining_units,
                    "expired_units": quota.expired_units,
                }
                for quota in cycle.quotas.all()
                for product_content in [
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
    item, *, locale=MembershipContentLocale.VI, include_allocations=False, admin=False
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
                "product_name": product_content["name"],
                "requested_locale": locale,
                "content_locale": product_content["content_locale"],
                "is_fallback": product_content["is_fallback"],
                **(
                    {"snapshot_complete": product_content["snapshot_complete"]}
                    if admin
                    else {}
                ),
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
            for product_content in [
                _localized_snapshot(
                    locale=locale,
                    name_vi=line.product_snapshot.product_name_vi_snapshot,
                    name_en=line.product_snapshot.product_name_en_snapshot,
                    legacy_name=line.product_snapshot.product_name,
                )
            ]
        ],
    }
    reversal = getattr(item, "reversal", None)
    if reversal:
        data["reversal"] = {
            "reason": reversal.reason,
            "created_at": reversal.created_at.isoformat(),
        }
    return data


def package_queryset():
    return MembershipPackage.objects.prefetch_related(
        "translations", "items__product__translations"
    )


def request_queryset():
    return MembershipRequest.objects.select_related(
        "user", "user__member_profile", "package", "payment_instruction"
    ).prefetch_related("item_snapshots", "events__actor")


def membership_queryset():
    return Membership.objects.select_related(
        "user", "user__member_profile", "request", "package"
    ).prefetch_related("product_snapshots")


def usage_queryset():
    return MembershipUsageOrder.objects.select_related(
        "membership", "user", "reversal"
    ).prefetch_related("lines__product_snapshot", "lines__allocations__cycle_quota__cycle")


class PublicPackageListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = PublicPackageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = (
            package_queryset()
            .filter(is_active=True, items__isnull=False)
            .exclude(items__product__is_active=False)
            .distinct()
        )
        items, meta = paginate(queryset, query.validated_data["page"])
        response = Response(
            {
                "data": [
                    package_payload(
                        item,
                        public=True,
                        locale=query.validated_data["locale"],
                    )
                    for item in items
                ],
                "meta": meta,
            }
        )
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class PublicPaymentAvailabilityView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        response = Response({"data": {"direct_transfer_enabled": direct_transfer_is_available()}})
        response["Cache-Control"] = "public, no-store"
        return response


class SelfMembershipRequestView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        locale = requested_locale(request)
        item = (
            request_queryset()
            .filter(user=request.user)
            .exclude(status__in=["approved", "rejected"])
            .first()
        )
        return no_store(
            Response({"data": request_payload(item, locale=locale) if item else None})
        )

    def post(self, request):
        locale = requested_locale(request)
        serializer = MembershipRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, _instruction = create_membership_request(
                user=request.user, **serializer.validated_data
            )
        except MembershipError as error:
            return membership_error(error)
        item = request_queryset().get(pk=item.pk)
        return no_store(
            Response(
                {"data": request_payload(item, locale=locale)},
                status=status.HTTP_201_CREATED,
            )
        )


class SelfPaymentSubmittedView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def post(self, request, request_id):
        locale = requested_locale(request)
        serializer = RequestActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, changed = submit_payment(request_id=request_id, user=request.user)
        except MembershipRequest.DoesNotExist:
            return no_store(Response(status=status.HTTP_404_NOT_FOUND))
        except MembershipError as error:
            return membership_error(error)
        item = request_queryset().get(pk=item.pk)
        return no_store(
            Response(
                {
                    "data": request_payload(item, locale=locale),
                    "meta": {"idempotent_replay": not changed},
                }
            )
        )


class SelfMembershipView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
        locale = requested_locale(request)
        item = (
            membership_queryset()
            .select_for_update()
            .filter(user=request.user, current_slot=True)
            .first()
        )
        if not item:
            return no_store(Response({"data": None}))
        ensure_due_cycles(item)
        item.refresh_from_db()
        if item.status not in {MembershipStatus.SCHEDULED, MembershipStatus.ACTIVE}:
            return no_store(Response({"data": None}))
        return no_store(Response({"data": membership_payload(item, locale=locale)}))


class SelfQuotaView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
        locale = requested_locale(request)
        item = (
            membership_queryset()
            .select_for_update()
            .filter(user=request.user, current_slot=True)
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
            usage_queryset().filter(user=request.user), query.validated_data["page"]
        )
        return no_store(
            Response(
                {
                    "data": [
                        usage_payload(item, locale=query.validated_data["locale"])
                        for item in items
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
        search = query.validated_data["search"]
        if search:
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
        translations = data.pop("translations", None)
        for field, value in data.items():
            setattr(item, field, value)
        if translations is not None:
            vi = next(
                translation
                for translation in translations
                if translation["locale"] == MembershipContentLocale.VI
            )
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
        item = Product.objects.prefetch_related("translations").get(pk=item.pk)
        return no_store(Response({"data": product_payload(item)}))

    def destroy(self, request, pk=None):
        item = get_object_or_404(Product, pk=pk)
        try:
            item.delete()
        except ProtectedError as error:
            raise ValidationError({"product": "Product is in use."}) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPackageViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminPackageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = package_queryset()
        search = query.validated_data["search"]
        if search:
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
        vi = next(item for item in translations if item["locale"] == MembershipContentLocale.VI)
        package = MembershipPackage.objects.create(
            name=vi["name"], description=vi["description"], **data
        )
        MembershipPackageTranslation.objects.bulk_create(
            [
                MembershipPackageTranslation(package=package, **translation)
                for translation in translations
            ]
        )
        MembershipPackageItem.objects.bulk_create(
            [
                MembershipPackageItem(
                    package=package, product=item["product_id"], quota_units=item["quota_units"]
                )
                for item in items
            ]
        )
        package = package_queryset().get(pk=package.pk)
        return no_store(
            Response({"data": package_payload(package)}, status=status.HTTP_201_CREATED)
        )

    def retrieve(self, request, pk=None):
        return no_store(
            Response({"data": package_payload(get_object_or_404(package_queryset(), pk=pk))})
        )

    @transaction.atomic
    def partial_update(self, request, pk=None):
        package = get_object_or_404(MembershipPackage.objects.select_for_update(), pk=pk)
        serializer = PackageWriteSerializer(package, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = data.pop("items", None)
        translations = data.pop("translations", None)
        for field, value in data.items():
            setattr(package, field, value)
        if translations is not None:
            vi = next(
                translation
                for translation in translations
                if translation["locale"] == MembershipContentLocale.VI
            )
            package.name = vi["name"]
            package.description = vi["description"]
        package.save()
        if translations is not None:
            for translation in translations:
                MembershipPackageTranslation.objects.update_or_create(
                    package=package,
                    locale=translation["locale"],
                    defaults={
                        "name": translation["name"],
                        "description": translation["description"],
                    },
                )
        if items is not None:
            package.items.all().delete()
            MembershipPackageItem.objects.bulk_create(
                [
                    MembershipPackageItem(
                        package=package, product=item["product_id"], quota_units=item["quota_units"]
                    )
                    for item in items
                ]
            )
        package = package_queryset().get(pk=package.pk)
        return no_store(Response({"data": package_payload(package)}))

    def destroy(self, request, pk=None):
        package = get_object_or_404(MembershipPackage, pk=pk)
        try:
            package.delete()
        except ProtectedError as error:
            raise ValidationError({"package": "Package is in use."}) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPaymentSettingsView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        item = MembershipPaymentSettings.objects.filter(pk="default").first()
        return no_store(
            Response(
                {
                    "data": (
                        {
                            "direct_transfer_enabled": item.direct_transfer_enabled,
                            "bank_bin": item.bank_bin,
                            "bank_name": item.bank_name,
                            "account_number": item.account_number,
                            "account_name": item.account_name,
                            "version": item.version,
                            "updated_at": item.updated_at.isoformat(),
                        }
                        if item
                        else None
                    )
                }
            )
        )

    def _update(self, request, *, partial):
        with transaction.atomic():
            try:
                current = MembershipPaymentSettings.objects.select_for_update().get(pk="default")
            except MembershipPaymentSettings.DoesNotExist:
                try:
                    with transaction.atomic():
                        current = MembershipPaymentSettings.objects.create(
                            key="default", account_number="", account_name="", version=0
                        )
                except IntegrityError:
                    current = MembershipPaymentSettings.objects.select_for_update().get(
                        pk="default"
                    )
            serializer = PaymentSettingsWriteSerializer(
                data=request.data,
                partial=partial,
                instance=current,
            )
            serializer.is_valid(raise_exception=True)
            if "version" not in serializer.validated_data:
                raise ValidationError({"version": "This field is required."})
            submitted_version = serializer.validated_data.pop("version")
            if submitted_version != current.version:
                return payment_settings_conflict_response()
            bank_bin = serializer.validated_data.get("bank_bin", current.bank_bin).strip()
            bank_name = ""
            if bank_bin:
                try:
                    bank = find_transfer_bank(bank_bin)
                except BankDirectoryUnavailable:
                    return bank_directory_unavailable_response()
                if not bank:
                    raise ValidationError({"bank_bin": "Select a supported VietQR bank."})
                bank_name = bank["name"]
            for field, value in serializer.validated_data.items():
                setattr(current, field, value)
            current.bank_bin = bank_bin
            current.bank_name = bank_name
            current.updated_by = request.user
            current.version += 1
            current.save()
            item = current
        return no_store(
            Response(
                {
                    "data": {
                        "direct_transfer_enabled": item.direct_transfer_enabled,
                        "bank_bin": item.bank_bin,
                        "bank_name": item.bank_name,
                        "account_number": item.account_number,
                        "account_name": item.account_name,
                        "version": item.version,
                        "updated_at": item.updated_at.isoformat(),
                    }
                }
            )
        )

    def put(self, request):
        return self._update(request, partial=False)

    def patch(self, request):
        return self._update(request, partial=True)


def bank_directory_unavailable_response():
    return no_store(
        Response(
            {"errors": [{"code": "bank_directory_unavailable"}]},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    )


def payment_settings_conflict_response():
    return no_store(
        Response(
            {"errors": [{"code": "payment_settings_conflict"}]},
            status=status.HTTP_409_CONFLICT,
        )
    )


class AdminBankOptionsView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        try:
            banks = get_transfer_banks()
        except BankDirectoryUnavailable:
            return bank_directory_unavailable_response()
        return no_store(Response({"data": banks}))


class AdminRequestViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminRequestQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = request_queryset()
        if query.validated_data["status"]:
            queryset = queryset.filter(status=query.validated_data["status"])
        search = query.validated_data["search"].strip()
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search)
                | Q(short_code__icontains=search)
                | Q(phone_snapshot__icontains=search)
                | Q(user__member_profile__phone_number__icontains=search)
            )
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(
            Response(
                {
                    "data": [
                        request_payload(
                            item, locale=query.validated_data["locale"], admin=True
                        )
                        for item in items
                    ],
                    "meta": meta,
                }
            )
        )

    def retrieve(self, request, pk=None):
        locale = requested_locale(request)
        return no_store(
            Response(
                {
                    "data": request_payload(
                        get_object_or_404(request_queryset(), pk=pk),
                        locale=locale,
                        admin=True,
                        include_events=True,
                    )
                }
            )
        )

    def _empty_action(self, request):
        requested_locale(request)
        serializer = RequestActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

    @action(detail=True, methods=["post"])
    def contact(self, request, pk=None):
        self._empty_action(request)
        return self._run(request, lambda: mark_contacted(request_id=pk, actor=request.user))

    @action(detail=True, methods=["post"], url_path="issue-payment-instruction")
    def issue_instruction(self, request, pk=None):
        self._empty_action(request)
        return self._run(
            request, lambda: issue_payment_instruction(request_id=pk, actor=request.user)[0]
        )

    @action(detail=True, methods=["post"], url_path="return-payment-pending")
    def return_payment(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            request,
            lambda: return_to_payment_pending(
                request_id=pk, actor=request.user, **serializer.validated_data
            )
        )

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            request,
            lambda: reject_request(request_id=pk, actor=request.user, **serializer.validated_data)
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        locale = requested_locale(request)
        serializer = ApproveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            membership, created = approve_request(
                request_id=pk, actor=request.user, **serializer.validated_data
            )
        except MembershipRequest.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except MembershipError as error:
            return membership_error(error)
        return no_store(
            Response(
                {
                    "data": membership_payload(
                        membership, locale=locale, include_user=True, admin=True
                    ),
                    "meta": {"idempotent_replay": not created},
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )

    def _run(self, request, operation):
        locale = requested_locale(request)
        try:
            item = operation()
        except MembershipRequest.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except MembershipError as error:
            return membership_error(error)
        return no_store(
            Response(
                {
                    "data": request_payload(
                        request_queryset().get(pk=item.pk),
                        locale=locale,
                        admin=True,
                        include_events=True,
                    )
                }
            )
        )


class AdminMembershipViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminMembershipQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        refresh_membership_statuses()
        queryset = membership_queryset()
        search = query.validated_data["search"].strip()
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search)
                | Q(user__name__icontains=search)
                | Q(user__member_profile__phone_number__icontains=search)
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
            item,
            locale=locale,
            include_quota=True,
            include_user=True,
            admin=True,
        )
        data["cycles"] = cycle_payload(item, locale=locale)
        return no_store(Response({"data": data}))

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
        item = usage_queryset().get(pk=item.pk)
        return no_store(
            Response(
                {
                    "data": usage_payload(
                        item, locale=locale, include_allocations=True, admin=True
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
