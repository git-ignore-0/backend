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
from identity.models import User
from memberships.models import (
    Membership,
    MembershipContentLocale,
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPackageTranslation,
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
    LocalePageQuerySerializer,
    LocaleQuerySerializer,
    MembershipAssignSerializer,
    MembershipRevokeSerializer,
    PackageWriteSerializer,
    ProductWriteSerializer,
    PublicPackageQuerySerializer,
    UsageCreateSerializer,
    UsageUndoSerializer,
)
from memberships.services import (
    MembershipError,
    MembershipNotRevocable,
    PriceOptionConflict,
    PriceOptionTotalInvalid,
    assign_membership,
    create_usage_order,
    ensure_due_cycles,
    refresh_membership_statuses,
    revoke_membership,
    sync_package_price_options,
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
    return no_store(
        Response({"errors": [{"code": error.code}]}, status=status.HTTP_409_CONFLICT)
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
        "user_id": str(item.user_id),
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
        data["user"] = {
            "id": str(item.user_id),
            "name": item.user.name,
            "email": item.user.email,
            "phone": getattr(getattr(item.user, "member_profile", None), "phone_number", ""),
        }
    return data


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


def membership_queryset():
    return Membership.objects.select_related(
        "user", "user__member_profile", "package", "price_option", "revoked_by"
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
            .filter(is_active=True, items__isnull=False, price_options__is_active=True)
            .exclude(items__product__is_active=False)
            .distinct()
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
        return no_store(
            Response({"data": [product_payload(item) for item in items], "meta": meta})
        )

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
        return no_store(
            Response({"data": product_payload(item)}, status=status.HTTP_201_CREATED)
        )

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
            raise ValidationError({"product": "Product is in use."}) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminPackageViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminPackageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = package_queryset()
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
        return no_store(
            Response({"data": [package_payload(item) for item in items], "meta": meta})
        )

    @transaction.atomic
    def create(self, request):
        serializer = PackageWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = data.pop("items")
        translations = data.pop("translations")
        options = data.pop("price_options")
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
                {"data": package_payload(package_queryset().get(pk=package.pk))},
                status=status.HTTP_201_CREATED,
            )
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
            Response({"data": package_payload(package_queryset().get(pk=package.pk))})
        )

    def destroy(self, request, pk=None):
        package = get_object_or_404(MembershipPackage, pk=pk)
        try:
            package.delete()
        except ProtectedError as error:
            raise ValidationError({"package": "Package is in use."}) from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminMembershipViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminMembershipQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        refresh_membership_statuses()
        queryset = membership_queryset()
        if search := query.validated_data["search"].strip():
            queryset = queryset.filter(
                Q(user__email__icontains=search)
                | Q(user__name__icontains=search)
                | Q(user__member_profile__phone_number__icontains=search)
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
            item = assign_membership(actor=request.user, **serializer.validated_data)
        except User.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        except PriceOptionTotalInvalid as error:
            raise ValidationError({"price_option_id": str(error)}) from error
        except MembershipError as error:
            return membership_error(error)
        return no_store(
            Response(
                {
                    "data": membership_payload(
                        membership_queryset().get(pk=item.pk),
                        locale=locale,
                        include_user=True,
                        admin=True,
                    )
                },
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
