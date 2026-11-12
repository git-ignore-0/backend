"""Scoped Admin and OAuth member endpoints for CSA rounds."""

from decimal import Decimal

from django.db.models import Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.views import APIView

from commerce.models import Product
from commerce.public_catalog import _translation, public_product_payload, public_product_queryset
from commerce.store_orders import StoreOrderError
from content.permissions import IsWebsiteAdmin
from memberships import csa_rounds as domain
from memberships.csa_payloads import event_payload, round_payload, selection_payload
from memberships.models import CSARound, CSASelection, Membership
from memberships.permissions import HasMembershipSelfScopes
from memberships.serializers import LocalePageQuerySerializer, ReasonSerializer, StrictSerializer


class RoundPageQuery(LocalePageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)


def paged_rows(rows, query):
    page, size = query["page"], query["page_size"]
    return rows[(page - 1) * size : page * size], {
        "page": page,
        "page_size": size,
        "total": rows.count(),
    }


def page_query(request):
    serializer = RoundPageQuery(data=request.query_params.dict())
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class RoundInput(StrictSerializer):
    name = serializers.CharField(max_length=200)
    opens_at = serializers.DateTimeField()
    closes_at = serializers.DateTimeField()
    delivery_at = serializers.DateTimeField()


class LineInput(StrictSerializer):
    product_id = serializers.UUIDField()
    quantity = serializers.IntegerField(min_value=1, max_value=2147483647)


class SelectionInput(StrictSerializer):
    lines = LineInput(many=True, max_length=100)
    locale = serializers.ChoiceField(choices=["vi", "en"], default="vi")
    expected_revision = serializers.IntegerField(min_value=0)


class DeadlineInput(StrictSerializer):
    deadline = serializers.DateTimeField()
    expected_revision = serializers.IntegerField(min_value=0)


class ConfirmInput(StrictSerializer):
    expected_revision = serializers.IntegerField(min_value=0)


ReasonInput = ReasonSerializer


def validated(cls, request, *, partial=False):
    serializer = cls(data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


class PrivateView(APIView):
    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, (domain.CSARoundError, StoreOrderError)):
            return Response({"errors": [{"code": exc.code}]}, status=409)
        if isinstance(exc, (CSARound.DoesNotExist, CSASelection.DoesNotExist)):
            return Response({"errors": [{"code": "not_found"}]}, status=404)
        return super().handle_exception(exc)


class AdminRounds(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = page_query(request)
        rows, meta = paged_rows(CSARound.objects.order_by("-sequence"), query)
        return Response({"data": [round_payload(r) for r in rows], "meta": meta})

    def post(self, request):
        row = domain.create_round(actor=request.user, **validated(RoundInput, request))
        return Response({"data": round_payload(row)}, status=201)


class AdminRound(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, round_id):
        row = get_object_or_404(CSARound, pk=round_id)
        return Response({"data": {**round_payload(row), "can_edit": domain.can_edit_round(row)}})

    def patch(self, request, round_id):
        row = domain.update_round(
            round_id=round_id, actor=request.user, **validated(RoundInput, request, partial=True)
        )
        return Response({"data": round_payload(row)})


class AdminRoundPublish(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, round_id):
        validated(StrictSerializer, request)
        return Response(
            {"data": round_payload(domain.publish_round(round_id=round_id, actor=request.user))}
        )


class AdminRoundCancel(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, round_id):
        return Response(
            {
                "data": round_payload(
                    domain.cancel_round(
                        round_id=round_id, actor=request.user, **validated(ReasonInput, request)
                    )
                )
            }
        )


class AdminRoundSelections(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, round_id):
        query = page_query(request)
        all_rows = CSASelection.objects.filter(round_id=round_id)
        page, meta = paged_rows(all_rows.order_by("created_at", "id"), query)
        page_ids = list(page.values_list("pk", flat=True))
        domain.process_round(round_id, selection_ids=page_ids)
        demand = list(
            all_rows.filter(pk__in=domain.active_stock_holds())
            .values("lines__product_id")
            .annotate(quantity=Sum("lines__quantity"))
            .filter(lines__product_id__isnull=False)
            .order_by("lines__product_id")
        )
        products = {
            product.pk: product
            for product in Product.objects.filter(
                pk__in=[item["lines__product_id"] for item in demand]
            ).prefetch_related("translations")
        }
        demand_payload = []
        for item in demand:
            product = products[item["lines__product_id"]]
            translation, _locales = _translation(product, query["locale"])
            demand_payload.append(
                {
                    "product_id": str(product.pk),
                    "quantity": item["quantity"],
                    "unit_size": str(product.unit_size),
                    "name": translation.name if translation else product.sku,
                    "unit_label": product.unit_label_vi
                    if query["locale"] == "vi"
                    else product.unit_label_en,
                }
            )
        rows = (
            all_rows.filter(pk__in=page_ids)
            .select_related("membership__customer")
            .prefetch_related("lines")
            .order_by("created_at", "id")
        )
        return Response(
            {
                "data": [selection_payload(row, admin=True) for row in rows],
                "meta": meta,
                "demand": demand_payload,
            }
        )


class AdminSelectionEvents(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, selection_id):
        query = page_query(request)
        selection = get_object_or_404(CSASelection, pk=selection_id)
        domain.process_round(selection.round_id, selection_ids=[selection.pk])
        rows, meta = paged_rows(selection.events.order_by("-created_at", "-id"), query)
        return Response({"data": [event_payload(row) for row in rows], "meta": meta})


class AdminReopen(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, selection_id):
        row = domain.reopen_selection(
            selection_id=selection_id, actor=request.user, **validated(DeadlineInput, request)
        )
        return Response({"data": selection_payload(row, admin=True)})


class MemberRounds(PrivateView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request):
        query = page_query(request)
        domain.process_due_rounds(user=request.user)
        rows = (
            CSARound.objects.filter(published_at__isnull=False)
            .filter(
                Q(delivery_at__gte=timezone.now())
                | Q(selections__membership__customer__user=request.user)
            )
            .distinct()
        )
        rows, meta = paged_rows(rows.order_by("-sequence"), query)
        return Response({"data": [round_payload(r) for r in rows], "meta": meta})


class MemberSelection(PrivateView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request, round_id):
        get_object_or_404(CSARound, pk=round_id, published_at__isnull=False)
        domain.process_round(round_id, user=request.user)
        row = (
            CSASelection.objects.filter(round_id=round_id, membership__customer__user=request.user)
            .prefetch_related("lines")
            .first()
        )
        return Response({"data": selection_payload(row) if row else None})

    def put(self, request, round_id):
        domain.process_round(round_id, user=request.user)
        row = domain.save_selection(
            round_id=round_id, user=request.user, **validated(SelectionInput, request)
        )
        return Response({"data": selection_payload(row)})


class MemberConfirm(PrivateView):
    permission_classes = [HasMembershipSelfScopes]

    def post(self, request, selection_id):
        own = get_object_or_404(
            CSASelection, pk=selection_id, membership__customer__user=request.user
        )
        domain.process_round(own.round_id, user=request.user)
        row = domain.confirm_selection(
            selection_id=selection_id, user=request.user, **validated(ConfirmInput, request)
        )
        return Response({"data": selection_payload(row)})


class MemberProducts(PrivateView):
    permission_classes = [HasMembershipSelfScopes]

    def get(self, request, round_id):
        round_ = get_object_or_404(
            CSARound, pk=round_id, published_at__isnull=False, cancelled_at__isnull=True
        )
        domain.process_round(round_id, user=request.user)
        existing = CSASelection.objects.filter(
            round=round_, membership__customer__user=request.user
        ).first()
        today = timezone.localdate()
        member = (
            existing.membership
            if existing
            else Membership.objects.filter(
                customer__user=request.user,
                start_date__lte=today,
                end_date__gt=today,
                status__in=["active", "scheduled"],
            ).first()
        )
        if not member or member.status == "revoked":
            raise domain.CSARoundError("csa_membership_unavailable")
        locale = request.query_params.get("locale", "vi")
        if locale not in ("vi", "en"):
            raise serializers.ValidationError({"locale": "Choose vi or en."})
        from memberships.services import ensure_due_cycles

        ensure_due_cycles(member)
        quota_rows = domain._quota_candidates(
            member,
            existing,
            set(member.product_snapshots.values_list("product_id", flat=True)),
            lock=False,
        )
        quota = [
            {
                "product_id": str(group_id),
                "available_units": format(sum((row[1] for row in rows), Decimal(0)), ".2f"),
            }
            for group_id, rows in quota_rows.items()
        ]
        products = public_product_queryset(csa_selection=existing).filter(
            csa_quota_group__is_active=True, csa_quota_cost__gt=0
        )
        if existing:
            products = products.filter(
                Q(available_quantity__gt=0) | Q(pk__in=existing.lines.values("product_id"))
            )
        else:
            products = products.filter(available_quantity__gt=0)
        data = []
        for product in products:
            data.append(public_product_payload(product, request, locale))
        return Response(
            {
                "data": data,
                "round": round_payload(round_),
                "quota": quota,
                "selection": selection_payload(existing) if existing else None,
            }
        )
