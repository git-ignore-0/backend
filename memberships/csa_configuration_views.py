"""Admin collection, operation and occurrence list for CSA scheduling."""

from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers
from rest_framework.response import Response

from content.permissions import IsWebsiteAdmin
from memberships import csa_rounds as domain
from memberships.csa_admin_payloads import admin_round_payloads, admin_round_queryset
from memberships.csa_round_views import PrivateView, page_query, paged_rows, validated
from memberships.models import CSARoundRecurrence
from memberships.serializers import PageQuerySerializer, StrictSerializer


class OccurrencePageQuery(PageQuerySerializer):
    page_size = serializers.IntegerField(min_value=1, max_value=100, default=20)
    status = serializers.ChoiceField(choices=domain.ROUND_STATUSES, required=False)


class ConfigurationPageQuery(OccurrencePageQuery):
    type = serializers.ChoiceField(choices=("standalone", "weekly"), required=False)


def configuration_query(request, serializer_class):
    repeated = {
        key: "Provide this parameter once."
        for key, values in request.query_params.lists()
        if len(values) > 1
    }
    if repeated:
        raise serializers.ValidationError(repeated)
    return page_query(request, serializer_class)


def configuration_roots():
    return admin_round_queryset().filter(recurrence__isnull=True)


class AdminRoundConfigurations(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = configuration_query(request, ConfigurationPageQuery)
        now = timezone.now()
        rows = configuration_roots().order_by("-sequence")
        if kind := query.get("type"):
            rows = rows.filter(recurrence_schedule__isnull=kind == "standalone")
        if status := query.get("status"):
            rows = domain.filter_rounds_by_status(rows, status, now=now)
        page, meta = paged_rows(rows, query)
        return Response(
            {"data": admin_round_payloads(page, now=now, configurations=True), "meta": meta}
        )


class AdminRoundConfiguration(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, round_id):
        configuration_query(request, StrictSerializer)
        root = get_object_or_404(configuration_roots(), pk=round_id)
        return Response({"data": admin_round_payloads([root], configurations=True)[0]})


class AdminConfigurationOccurrences(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, round_id):
        query = configuration_query(request, OccurrencePageQuery)
        schedule = get_object_or_404(CSARoundRecurrence, root_round_id=round_id)
        now = timezone.now()
        rows = (
            admin_round_queryset()
            .filter(Q(pk=round_id) | Q(recurrence_id=schedule.pk))
            .order_by("-sequence")
        )
        if status := query.get("status"):
            rows = domain.filter_rounds_by_status(rows, status, now=now)
        page, meta = paged_rows(rows, query)
        return Response(
            {"data": admin_round_payloads(page, now=now, participants=True), "meta": meta}
        )


class AdminStopRoundConfiguration(PrivateView):
    permission_classes = [IsWebsiteAdmin]

    def post(self, request, round_id):
        configuration_query(request, StrictSerializer)
        validated(StrictSerializer, request)
        domain.stop_round_recurrence(round_id=round_id)
        root = configuration_roots().get(pk=round_id)
        return Response({"data": admin_round_payloads([root], configurations=True)[0]})
