"""Admin collection, operation and occurrence list for CSA scheduling."""

from datetime import UTC, date, datetime, time, timedelta

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from content.creation_receipts import (
    CreationIdempotencyConflict,
    create_with_receipt,
    creation_receipt_meta,
    find_creation_receipt,
)
from content.models import AdminCreationReceipt
from content.permissions import IsWebsiteAdmin
from memberships import csa_rounds as domain
from memberships.csa_admin_payloads import admin_round_payloads, admin_round_queryset
from memberships.csa_round_views import PrivateView, RoundInput, page_query, paged_rows, validated
from memberships.models import CSARoundConfiguration
from memberships.serializers import PageQuerySerializer, StrictSerializer


class RoundStatusPageQuery(PageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)
    status = serializers.ChoiceField(choices=domain.ROUND_STATUSES, required=False)


class ConfigurationPageQuery(PageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)
    status = serializers.ChoiceField(choices=("draft", "published", "cancelled"), required=False)
    type = serializers.ChoiceField(choices=("standalone", "weekly"), required=False)


class OccurrencePageQuery(RoundStatusPageQuery):
    generated_only = serializers.BooleanField(default=False)
    search = serializers.CharField(max_length=200, allow_blank=True, default="")


def occurrence_search(term):
    matches = Q(name__icontains=term)
    try:
        day = (
            datetime.strptime(term, "%d/%m/%Y").date() if "/" in term else date.fromisoformat(term)
        )
    except ValueError:
        return matches
    start = (
        datetime.combine(day, time.min, domain.VIETNAM).astimezone(UTC) if day > date.min else None
    )
    end = (
        datetime.combine(day + timedelta(days=1), time.min, domain.VIETNAM).astimezone(UTC)
        if day < date.max
        else None
    )
    matches |= Q(delivery_date=day)
    for field in ("opens_at", "closes_at"):
        within_day = Q()
        if start is not None:
            within_day &= Q(**{f"{field}__gte": start})
        if end is not None:
            within_day &= Q(**{f"{field}__lt": end})
        matches |= within_day
    return matches


def configuration_query(request, serializer_class):
    repeated = {
        key: "Provide this parameter once."
        for key, values in request.query_params.lists()
        if len(values) > 1
    }
    if repeated:
        raise serializers.ValidationError(repeated)
    return page_query(request, serializer_class)


class ConfigurationInput(RoundInput):
    recurrence_mode = serializers.ChoiceField(
        choices=CSARoundConfiguration.Mode.choices, required=False, allow_null=True
    )


class ConfigurationCreateInput(ConfigurationInput):
    idempotency_key = serializers.UUIDField(required=False)


class ConfigurationUpdateInput(ConfigurationInput):
    version = serializers.IntegerField(min_value=1, required=False)


class ConfigurationVersionInput(StrictSerializer):
    version = serializers.IntegerField(min_value=1, required=False)


class ConfigurationCreationQuery(StrictSerializer):
    creation_key = serializers.UUIDField()


def configuration_payload(configuration, *, detail=False):
    payload = {
        "id": str(configuration.pk),
        "name": configuration.name,
        "type": "weekly" if configuration.recurrence_mode else "standalone",
        "status": configuration.status,
        "version": configuration.version,
        "created_at": configuration.created_at.isoformat(),
    }
    if detail:
        payload.update(
            opens_at=configuration.opens_at.isoformat(),
            closes_at=configuration.closes_at.isoformat(),
            delivery_date=configuration.delivery_date.isoformat(),
            recurrence_mode=configuration.recurrence_mode,
            published_at=configuration.published_at.isoformat()
            if configuration.published_at
            else None,
        )
    return payload


class AdminRoundConfigurations(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        if "creation_key" in request.query_params:
            query = configuration_query(request, ConfigurationCreationQuery)
            receipt = find_creation_receipt(
                actor=request.user,
                action=AdminCreationReceipt.Action.CSA_CONFIGURATION,
                key=query["creation_key"],
            )
            if receipt is None:
                return Response({"errors": [{"code": "create_receipt_not_found"}]}, status=404)
            return self.creation_response(receipt, created=False)
        query = configuration_query(request, ConfigurationPageQuery)
        rows = CSARoundConfiguration.objects.order_by("-created_at", "-pk")
        if kind := query.get("type"):
            rows = rows.filter(recurrence_mode__isnull=kind == "standalone")
        match query.get("status"):
            case "draft":
                rows = rows.filter(published_at__isnull=True, cancelled_at__isnull=True)
            case "published":
                rows = rows.filter(published_at__isnull=False, cancelled_at__isnull=True)
            case "cancelled":
                rows = rows.filter(cancelled_at__isnull=False)
        page, meta = paged_rows(rows, query)
        return Response({"data": [configuration_payload(row) for row in page], "meta": meta})

    def post(self, request):
        configuration_query(request, StrictSerializer)
        data = dict(validated(ConfigurationCreateInput, request))
        key = data.pop("idempotency_key", None)
        if key is None:
            configuration = domain.create_round_configuration(actor=request.user, **data)
            return Response({"data": configuration_payload(configuration, detail=True)}, status=201)
        # Serializer omission and explicit null select the same standalone schedule.
        data.setdefault("recurrence_mode", None)
        try:
            receipt, created = create_with_receipt(
                actor=request.user,
                action=AdminCreationReceipt.Action.CSA_CONFIGURATION,
                key=key,
                payload=data,
                create=lambda: domain.create_round_configuration(actor=request.user, **data),
            )
        except CreationIdempotencyConflict as error:
            raise domain.CSARoundError("idempotency_conflict") from error
        return self.creation_response(receipt, created=created)

    def creation_response(self, receipt, *, created):
        row = CSARoundConfiguration.objects.filter(pk=receipt.resource_id).first()
        return Response(
            {
                "data": configuration_payload(row, detail=True) if row else None,
                "meta": creation_receipt_meta(receipt, replayed=not created),
            },
            status=201 if created else 200,
        )


class AdminRoundConfiguration(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, configuration_id):
        configuration_query(request, StrictSerializer)
        configuration = get_object_or_404(CSARoundConfiguration, pk=configuration_id)
        return Response({"data": configuration_payload(configuration, detail=True)})

    def patch(self, request, configuration_id):
        configuration_query(request, StrictSerializer)
        configuration = domain.update_round_configuration(
            configuration_id=configuration_id,
            **validated(ConfigurationUpdateInput, request, partial=True),
        )
        return Response({"data": configuration_payload(configuration, detail=True)})

    def delete(self, request, configuration_id):
        configuration_query(request, StrictSerializer)
        domain.delete_round_configuration(
            configuration_id=configuration_id, **validated(ConfigurationVersionInput, request)
        )
        return Response(status=204)


class AdminPublishRoundConfiguration(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, configuration_id):
        configuration_query(request, StrictSerializer)
        configuration = domain.publish_round_configuration(
            configuration_id=configuration_id, **validated(ConfigurationVersionInput, request)
        )
        return Response({"data": configuration_payload(configuration, detail=True)})


class AdminConfigurationOccurrences(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, configuration_id):
        query = configuration_query(request, OccurrencePageQuery)
        configuration = get_object_or_404(CSARoundConfiguration, pk=configuration_id)
        now = timezone.now()
        rows = admin_round_queryset().filter(configuration=configuration).order_by("-sequence")
        if configuration.published_at is None:
            rows = rows.none()
        if query["generated_only"]:
            rows = rows.filter(recurrence_week__gt=0)
        if status := query.get("status"):
            rows = domain.filter_rounds_by_status(rows, status, now=now)
        if term := query["search"]:
            rows = rows.filter(occurrence_search(term))
        page, meta = paged_rows(rows, query)
        return Response(
            {"data": admin_round_payloads(page, now=now, participants=True), "meta": meta}
        )


class AdminStopRoundConfiguration(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, configuration_id):
        configuration_query(request, StrictSerializer)
        configuration = domain.stop_round_recurrence(
            configuration_id=configuration_id, **validated(ConfigurationVersionInput, request)
        )
        return Response({"data": configuration_payload(configuration, detail=True)})
