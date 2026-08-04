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
    MembershipCycleQuota,
    MembershipPackage,
    MembershipPackageItem,
    MembershipPaymentSettings,
    MembershipRequest,
    MembershipStatus,
    MembershipUsageOrder,
    Product,
)
from memberships.permissions import HasMembershipSelfScopes
from memberships.serializers import (
    AdminMembershipQuerySerializer,
    AdminProductQuerySerializer,
    AdminRequestQuerySerializer,
    ApproveSerializer,
    MembershipRequestCreateSerializer,
    PackageWriteSerializer,
    PageQuerySerializer,
    PaymentSettingsWriteSerializer,
    ProductWriteSerializer,
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


def product_payload(item):
    return {
        "id": str(item.pk),
        "name": item.name,
        "short_description": item.short_description,
        "unit_size": str(item.unit_size),
        "unit_label_vi": item.unit_label_vi,
        "unit_label_en": item.unit_label_en,
        "is_active": item.is_active,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


def package_payload(item, *, public=False):
    data = {
        "id": str(item.pk),
        "name": item.name,
        "description": item.description,
        "upfront_price": str(item.upfront_price),
        "duration_months": item.duration_months,
        "quota_policy": item.quota_policy,
        "items": [
            {
                "product_id": str(package_item.product_id),
                "product_name": package_item.product.name,
                "short_description": package_item.product.short_description,
                "unit_size": str(package_item.product.unit_size),
                "unit_label_vi": package_item.product.unit_label_vi,
                "unit_label_en": package_item.product.unit_label_en,
                "quota_units": package_item.quota_units,
            }
            for package_item in item.items.all()
        ],
    }
    if not public:
        data.update(
            {
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


def request_payload(item, *, admin=False, include_events=False):
    instruction = getattr(item, "payment_instruction", None)
    data = {
        "id": str(item.pk),
        "short_code": item.short_code,
        "status": item.status,
        "package": {
            "id": str(item.package_id),
            "name": item.package_name_snapshot,
            "description": item.package_description_snapshot,
            "upfront_price": str(item.upfront_price_snapshot),
            "duration_months": item.duration_months_snapshot,
            "quota_policy": item.quota_policy_snapshot,
            "items": [
                {
                    "product_id": str(snapshot.product_id),
                    "product_name": snapshot.product_name,
                    "unit_size": str(snapshot.unit_size),
                    "unit_label_vi": snapshot.unit_label_vi,
                    "unit_label_en": snapshot.unit_label_en,
                    "quota_units": snapshot.quota_units_per_cycle,
                }
                for snapshot in item.item_snapshots.all()
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


def membership_payload(item, *, include_quota=False, include_user=False):
    data = {
        "id": str(item.pk),
        "user_id": str(item.user_id),
        "request_id": str(item.request_id),
        "package_id": str(item.package_id),
        "status": item.status,
        "package_name": item.package_name_snapshot,
        "package_description": item.package_description_snapshot,
        "upfront_price": str(item.upfront_price_snapshot),
        "duration_months": item.duration_months_snapshot,
        "quota_policy": item.quota_policy_snapshot,
        "start_date": item.start_date.isoformat(),
        "end_date": item.end_date.isoformat(),
        "activated_at": item.activated_at.isoformat(),
    }
    if include_quota:
        data["quota"] = quota_payload(item)
    if include_user:
        data["user"] = {
            "id": str(item.user_id),
            "name": item.user.name,
            "email": item.user.email,
            "phone": getattr(getattr(item.user, "member_profile", None), "phone_number", ""),
        }
    return data


def quota_payload(membership):
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
            "product_name": snapshot.product_name,
            "unit_size": str(snapshot.unit_size),
            "unit_label_vi": snapshot.unit_label_vi,
            "unit_label_en": snapshot.unit_label_en,
            "quota_units_per_cycle": snapshot.quota_units_per_cycle,
            "remaining_units": balances.get(str(snapshot.pk), 0),
        }
        for snapshot in snapshots
    ]


def cycle_payload(membership):
    return [
        {
            "sequence": cycle.sequence,
            "start_date": cycle.start_date.isoformat(),
            "end_date": cycle.end_date.isoformat(),
            "granted_at": cycle.granted_at.isoformat(),
            "quotas": [
                {
                    "product_id": str(quota.product_snapshot.product_id),
                    "product_name": quota.product_snapshot.product_name,
                    "granted_units": quota.granted_units,
                    "remaining_units": quota.remaining_units,
                    "expired_units": quota.expired_units,
                }
                for quota in cycle.quotas.all()
            ],
        }
        for cycle in membership.cycles.all()
    ]


def usage_payload(item, *, include_allocations=False):
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
                "product_name": line.product_snapshot.product_name,
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
    return MembershipPackage.objects.prefetch_related("items__product")


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
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = (
            package_queryset()
            .filter(is_active=True, items__isnull=False)
            .exclude(items__product__is_active=False)
            .distinct()
        )
        items, meta = paginate(queryset, query.validated_data["page"])
        response = Response(
            {"data": [package_payload(item, public=True) for item in items], "meta": meta}
        )
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class PublicPaymentAvailabilityView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        response = Response(
            {"data": {"direct_transfer_enabled": direct_transfer_is_available()}}
        )
        response["Cache-Control"] = "public, no-store"
        return response


class SelfMembershipRequestView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        item = (
            request_queryset()
            .filter(user=request.user)
            .exclude(status__in=["approved", "rejected"])
            .first()
        )
        return no_store(Response({"data": request_payload(item) if item else None}))

    def post(self, request):
        serializer = MembershipRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, _instruction = create_membership_request(
                user=request.user, **serializer.validated_data
            )
        except MembershipError as error:
            return membership_error(error)
        item = request_queryset().get(pk=item.pk)
        return no_store(Response({"data": request_payload(item)}, status=status.HTTP_201_CREATED))


class SelfPaymentSubmittedView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def post(self, request, request_id):
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
            Response({"data": request_payload(item), "meta": {"idempotent_replay": not changed}})
        )


class SelfMembershipView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
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
        return no_store(Response({"data": membership_payload(item)}))


class SelfQuotaView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    @transaction.atomic
    def get(self, request):
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
            Response({"data": {"membership_id": str(item.pk), "products": quota_payload(item)}})
        )


class SelfUsageHistoryView(APIView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        items, meta = paginate(
            usage_queryset().filter(user=request.user), query.validated_data["page"]
        )
        return no_store(Response({"data": [usage_payload(item) for item in items], "meta": meta}))


class AdminProductViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        query = AdminProductQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = Product.objects.all()
        search = query.validated_data["search"]
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search) | Q(short_description__icontains=search)
            )
        if "active" in request.query_params:
            queryset = queryset.filter(is_active=query.validated_data["active"])
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(Response({"data": [product_payload(item) for item in items], "meta": meta}))

    def create(self, request):
        serializer = ProductWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = Product.objects.create(**serializer.validated_data)
        return no_store(Response({"data": product_payload(item)}, status=status.HTTP_201_CREATED))

    def retrieve(self, request, pk=None):
        item = get_object_or_404(Product, pk=pk)
        return no_store(Response({"data": product_payload(item)}))

    def partial_update(self, request, pk=None):
        item = get_object_or_404(Product, pk=pk)
        serializer = ProductWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        for field, value in serializer.validated_data.items():
            setattr(item, field, value)
        item.save()
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
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        items, meta = paginate(package_queryset(), query.validated_data["page"])
        return no_store(Response({"data": [package_payload(item) for item in items], "meta": meta}))

    @transaction.atomic
    def create(self, request):
        serializer = PackageWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = data.pop("items")
        package = MembershipPackage.objects.create(**data)
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
        serializer = PackageWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        items = data.pop("items", None)
        for field, value in data.items():
            setattr(package, field, value)
        package.save()
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
            Response({"data": [request_payload(item, admin=True) for item in items], "meta": meta})
        )

    def retrieve(self, request, pk=None):
        return no_store(
            Response(
                {
                    "data": request_payload(
                        get_object_or_404(request_queryset(), pk=pk),
                        admin=True,
                        include_events=True,
                    )
                }
            )
        )

    def _empty_action(self, request):
        serializer = RequestActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

    @action(detail=True, methods=["post"])
    def contact(self, request, pk=None):
        self._empty_action(request)
        return self._run(lambda: mark_contacted(request_id=pk, actor=request.user))

    @action(detail=True, methods=["post"], url_path="issue-payment-instruction")
    def issue_instruction(self, request, pk=None):
        self._empty_action(request)
        return self._run(lambda: issue_payment_instruction(request_id=pk, actor=request.user)[0])

    @action(detail=True, methods=["post"], url_path="return-payment-pending")
    def return_payment(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            lambda: return_to_payment_pending(
                request_id=pk, actor=request.user, **serializer.validated_data
            )
        )

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        serializer = ReasonSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self._run(
            lambda: reject_request(request_id=pk, actor=request.user, **serializer.validated_data)
        )

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
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
                    "data": membership_payload(membership),
                    "meta": {"idempotent_replay": not created},
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )

    def _run(self, operation):
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
                    "data": [membership_payload(item, include_user=True) for item in items],
                    "meta": meta,
                }
            )
        )

    @transaction.atomic
    def retrieve(self, request, pk=None):
        item = get_object_or_404(membership_queryset(), pk=pk)
        ensure_due_cycles(item)
        item = (
            membership_queryset()
            .prefetch_related("cycles__quotas__product_snapshot")
            .get(pk=item.pk)
        )
        data = membership_payload(item, include_quota=True, include_user=True)
        data["cycles"] = cycle_payload(item)
        return no_store(Response({"data": data}))

    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, pk=None):
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        membership = get_object_or_404(Membership, pk=pk)
        items, meta = paginate(
            usage_queryset().filter(membership=membership), query.validated_data["page"]
        )
        return no_store(
            Response(
                {
                    "data": [usage_payload(item, include_allocations=True) for item in items],
                    "meta": meta,
                }
            )
        )


class AdminUsageViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def create(self, request):
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
                    "data": usage_payload(item, include_allocations=True),
                    "meta": {"idempotent_replay": not created},
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )

    def retrieve(self, request, pk=None):
        return no_store(
            Response(
                {
                    "data": usage_payload(
                        get_object_or_404(usage_queryset(), pk=pk), include_allocations=True
                    )
                }
            )
        )

    @action(detail=True, methods=["post"])
    def undo(self, request, pk=None):
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
                {"data": usage_payload(usage_queryset().get(pk=item.pk), include_allocations=True)}
            )
        )
