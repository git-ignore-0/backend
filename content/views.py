from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.signing import BadSignature, SignatureExpired
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from content.models import (
    ContentAuditLog,
    ExternalLink,
    MediaAsset,
    SiteSettings,
    Testimonial,
    TestimonialStatus,
    TrackerFarm,
    Workshop,
    WorkshopRevision,
    WorkshopTranslation,
)
from content.permissions import IsWebsiteAdmin
from content.serializers import (
    AdminTestimonialQuerySerializer,
    PublicTestimonialQuerySerializer,
    PublicTrackerFarmQuerySerializer,
    PublicWorkshopQuerySerializer,
    SiteSettingsWriteSerializer,
    TestimonialActionSerializer,
    TestimonialUpdateSerializer,
    TestimonialWriteSerializer,
    TrackerFarmReorderSerializer,
    TrackerFarmUpdateSerializer,
    TrackerFarmWriteSerializer,
    WorkshopDraftWriteSerializer,
    WorkshopPublishSerializer,
    admin_revision_payload,
    admin_testimonial_payload,
    admin_tracker_farm_payload,
    media_payload,
    public_testimonial_payload,
    public_tracker_farm_payload,
    public_workshop_payload,
)
from content.services import (
    RevisionConflict,
    TestimonialTransitionConflict,
    TrackerFarmMediaReferenced,
    TrackerFarmMediaStorageMismatch,
    TrackerFarmVersionConflict,
    audit,
    create_preview_token,
    create_testimonial,
    create_tracker_farm,
    delete_temporary_tracker_farm_media,
    delete_tracker_farm,
    hide_testimonial,
    publish_revision,
    publish_testimonial,
    read_preview_token,
    reorder_tracker_farms,
    request_frontend_revalidation,
    store_media_image,
    store_testimonial_image,
    testimonial_queryset,
    tracker_farm_queryset,
    translation_is_ready,
    unique_workshop_slug,
    unpublish_testimonial,
    unpublish_workshop,
    update_testimonial,
    update_tracker_farm,
)


def get_site_settings():
    settings_object, _created = SiteSettings.objects.get_or_create(key="default")
    return SiteSettings.objects.prefetch_related("links").get(pk=settings_object.pk)


def validated_public_locale(request, default="vi"):
    locale = request.query_params.get("locale", default)
    if locale not in {"en", "vi"}:
        raise ValidationError({"locale": "Unsupported locale."})
    return locale


def site_settings_payload(settings_object):
    return {
        "email": settings_object.email if settings_object.is_email_enabled else "",
        "is_email_enabled": settings_object.is_email_enabled,
        "phone_display": (
            settings_object.phone_display if settings_object.is_phone_enabled else ""
        ),
        "is_phone_enabled": settings_object.is_phone_enabled,
        "links": [
            {
                "id": item.pk,
                "kind": item.kind,
                "label": item.label,
                "url": item.url,
                "position": item.position,
            }
            for item in settings_object.links.all()
            if item.is_enabled and item.kind != "google_maps"
        ],
        "updated_at": settings_object.updated_at.isoformat(),
    }


def admin_site_settings_payload(settings_object):
    data = site_settings_payload(settings_object)
    data.update(
        {
            "email": settings_object.email,
            "phone_display": settings_object.phone_display,
            "version": settings_object.version,
            "links": [
                {
                    "id": item.pk,
                    "kind": item.kind,
                    "label": item.label,
                    "url": item.url,
                    "position": item.position,
                    "is_enabled": item.is_enabled,
                }
                for item in settings_object.links.all()
                if item.kind != "google_maps"
            ],
        }
    )
    return data


class PublicSiteSettingsView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        validated_public_locale(request)
        response = Response({"data": site_settings_payload(get_site_settings())})
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class AdminSessionView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        return Response(
            {
                "data": {
                    "id": str(request.user.pk),
                    "email": request.user.email,
                    "name": request.user.name,
                    "locale": request.user.locale,
                    "roles": list(request.user.groups.values_list("name", flat=True)),
                }
            }
        )


class AdminSiteSettingsView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        obj = get_site_settings()
        return Response({"data": admin_site_settings_payload(obj)})

    @transaction.atomic
    def patch(self, request):
        serializer = SiteSettingsWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        obj = SiteSettings.objects.select_for_update().get(pk="default")
        data = serializer.validated_data
        if data["version"] != obj.version:
            return Response(
                {"errors": [{"code": "version_conflict"}]}, status=status.HTTP_409_CONFLICT
            )
        obj.email = data["email"]
        obj.is_email_enabled = data["is_email_enabled"]
        obj.phone_display = data["phone_display"]
        obj.is_phone_enabled = data["is_phone_enabled"]
        obj.version += 1
        obj.updated_by = request.user
        obj.save()
        obj.links.all().delete()
        ExternalLink.objects.bulk_create(
            [ExternalLink(settings=obj, **item) for item in data["links"]]
        )
        audit(actor=request.user, action="site_settings.update", entity=obj)
        transaction.on_commit(lambda: request_frontend_revalidation(["site-settings"]))
        obj = get_site_settings()
        return Response({"data": admin_site_settings_payload(obj)})


def published_queryset():
    return (
        WorkshopRevision.objects.filter(is_published=True, workshop__archived_at__isnull=True)
        .select_related("workshop")
        .prefetch_related("translations")
    )


class PublicWorkshopListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = PublicWorkshopQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        locale = query.validated_data["locale"]
        period = query.validated_data["period"]
        now = timezone.now()
        queryset = published_queryset()
        if period == "past":
            queryset = queryset.filter(end_at__lte=now).order_by("-start_at")
        else:
            queryset = queryset.filter(end_at__gt=now).order_by("start_at")
        page_size = query.validated_data["page_size"]
        page = query.validated_data["page"]
        total = queryset.count()
        items = queryset[(page - 1) * page_size : page * page_size]
        response = Response(
            {
                "data": [
                    public_workshop_payload(
                        item,
                        locale,
                        include_body=False,
                        effective_at=now,
                    )
                    for item in items
                ],
                "meta": {"page": page, "page_size": page_size, "total": total},
            }
        )
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class PublicWorkshopDetailView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, slug):
        revision = get_object_or_404(published_queryset(), workshop__slug=slug)
        locale = validated_public_locale(request)
        data = public_workshop_payload(revision, locale)
        if data is None:
            raise NotFound()
        response = Response({"data": data})
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class PublicWorkshopPreviewView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, token):
        try:
            revision = read_preview_token(token)
        except (BadSignature, SignatureExpired, WorkshopRevision.DoesNotExist) as error:
            raise NotFound("Preview is invalid or expired.") from error
        locale = validated_public_locale(request, revision.default_locale)
        data = public_workshop_payload(revision, locale)
        if data is None:
            raise NotFound()
        response = Response({"data": data})
        response["Cache-Control"] = "private, no-store"
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response


def published_testimonial_queryset():
    return testimonial_queryset().filter(status=TestimonialStatus.PUBLISHED)


def testimonial_page(queryset, page, page_size):
    total = queryset.count()
    start = (page - 1) * page_size
    return queryset[start : start + page_size], {
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def public_testimonial_response(data):
    response = Response(data)
    response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
    return response


def admin_no_store(response):
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


class PublicTrackerFarmListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = PublicTrackerFarmQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        locale = query.validated_data["locale"]
        farms = (
            tracker_farm_queryset()
            .filter(is_visible=True)
            .filter(Q(farm__is_active=True) | Q(farm__isnull=True))
        )
        response = Response(
            {"data": [public_tracker_farm_payload(farm, locale, request) for farm in farms]}
        )
        response["Cache-Control"] = "public, max-age=60, stale-while-revalidate=300"
        return response


class AdminTrackerFarmListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        data = [admin_tracker_farm_payload(farm, request) for farm in tracker_farm_queryset()]
        return admin_no_store(Response({"data": data}))

    def post(self, request):
        serializer = TrackerFarmWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            farm = create_tracker_farm(actor=request.user, data=serializer.validated_data)
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        return admin_no_store(
            Response(
                {"data": admin_tracker_farm_payload(farm, request)},
                status=status.HTTP_201_CREATED,
            )
        )


class AdminTrackerFarmDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def patch(self, request, farm_id):
        get_object_or_404(TrackerFarm, pk=farm_id)
        serializer = TrackerFarmUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        expected_version = data.pop("version")
        try:
            farm = update_tracker_farm(
                farm_id=farm_id,
                actor=request.user,
                expected_version=expected_version,
                data=data,
            )
        except TrackerFarmVersionConflict:
            return admin_no_store(
                Response(
                    {"errors": [{"code": "version_conflict"}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        return admin_no_store(Response({"data": admin_tracker_farm_payload(farm, request)}))

    def delete(self, request, farm_id):
        get_object_or_404(TrackerFarm, pk=farm_id)
        delete_tracker_farm(farm_id=farm_id, actor=request.user)
        return admin_no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminTrackerFarmReorderView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def patch(self, request):
        serializer = TrackerFarmReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            farms = reorder_tracker_farms(
                actor=request.user,
                items=serializer.validated_data["items"],
            )
        except TrackerFarmVersionConflict:
            return admin_no_store(
                Response(
                    {"errors": [{"code": "version_conflict"}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        return admin_no_store(
            Response({"data": [admin_tracker_farm_payload(farm, request) for farm in farms]})
        )


class PublicTestimonialListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = PublicTestimonialQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        queryset = published_testimonial_queryset().order_by(
            "sort_order", "-published_at", "created_at", "id"
        )
        if data["type"] != "all":
            queryset = queryset.filter(type=data["type"])
        items, meta = testimonial_page(queryset, data["page"], data["page_size"])
        payloads = [public_testimonial_payload(item, request, data["locale"]) for item in items]
        return public_testimonial_response(
            {"data": [item for item in payloads if item is not None], "meta": meta}
        )


class PublicFeaturedTestimonialView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        locale = validated_public_locale(request)
        base = published_testimonial_queryset().order_by(
            "sort_order", "-published_at", "created_at", "id"
        )
        first_featured = base.filter(featured=True).first()
        if first_featured is None:
            items = list(base[:3])
        else:
            items = [first_featured, *list(base.exclude(pk=first_featured.pk)[:2])]
        payloads = [public_testimonial_payload(item, request, locale) for item in items]
        return public_testimonial_response(
            {"data": [item for item in payloads if item is not None]}
        )


class PublicTestimonialDetailView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request, testimonial_id):
        locale = validated_public_locale(request)
        testimonial = get_object_or_404(
            published_testimonial_queryset(),
            pk=testimonial_id,
        )
        data = public_testimonial_payload(
            testimonial,
            request,
            locale,
            include_full_story=True,
        )
        if data is None:
            raise NotFound()
        return public_testimonial_response({"data": data})


def apply_translation_data(revision, translations):
    for translation_data in translations:
        data = dict(translation_data)
        locale = data.pop("locale")
        translation, _created = WorkshopTranslation.objects.update_or_create(
            revision=revision,
            locale=locale,
            defaults=data,
        )
        ready = translation_is_ready(translation)
        if translation.is_ready != ready:
            translation.is_ready = ready
            translation.save(update_fields=["is_ready"])


def sync_workshop_locale_and_slug(revision):
    translations = list(revision.translations.all())
    by_locale = {item.locale: item for item in translations}
    ready_locales = [item.locale for item in translations if item.is_ready]
    titled_locales = [item.locale for item in translations if item.title.strip()]
    candidates = ready_locales or titled_locales
    selected_locale = revision.default_locale
    if selected_locale not in candidates and candidates:
        selected_locale = "vi" if "vi" in candidates else candidates[0]
    if selected_locale != revision.default_locale:
        revision.default_locale = selected_locale
        revision.save(update_fields=["default_locale", "updated_at"])

    source = by_locale.get(selected_locale)
    has_published_revision = revision.workshop.revisions.filter(is_published=True).exists()
    if source and source.title.strip() and not has_published_revision:
        revision.workshop.slug = unique_workshop_slug(
            source.title,
            exclude_workshop_id=revision.workshop_id,
        )
        revision.workshop.save(update_fields=["slug", "updated_at"])


class AdminWorkshopViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]

    def list(self, request):
        queryset = (
            WorkshopRevision.objects.filter(is_draft=True, workshop__archived_at__isnull=True)
            .select_related("workshop")
            .prefetch_related("translations", "workshop__revisions")
            .order_by("start_at")
        )
        return Response({"data": [admin_revision_payload(item) for item in queryset]})

    def create(self, request):
        serializer = WorkshopDraftWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        required = ["start_at", "end_at"]
        missing = [item for item in required if item not in data]
        if missing:
            raise ValidationError({item: "This field is required." for item in missing})
        translations = data.pop("translations", [])
        titled_translations = [item for item in translations if item.get("title", "").strip()]
        if not titled_translations:
            raise ValidationError({"translations": "A workshop title is required."})
        preferred_locale = data.get("default_locale", "vi")
        source = next(
            (item for item in titled_translations if item["locale"] == preferred_locale),
            titled_translations[0],
        )
        data["default_locale"] = source["locale"]
        with transaction.atomic():
            workshop = Workshop.objects.create(
                slug=unique_workshop_slug(source["title"]),
                created_by=request.user,
            )
            revision = WorkshopRevision.objects.create(
                workshop=workshop,
                revision_number=1,
                created_by=request.user,
                **data,
            )
            apply_translation_data(revision, translations)
            sync_workshop_locale_and_slug(revision)
            audit(actor=request.user, action="workshop.create", entity=workshop)
        revision = WorkshopRevision.objects.prefetch_related("translations").get(pk=revision.pk)
        return Response(
            {"data": admin_revision_payload(revision)},
            status=status.HTTP_201_CREATED,
        )

    def retrieve(self, request, pk=None):
        revision = get_object_or_404(
            WorkshopRevision.objects.select_related("workshop").prefetch_related(
                "translations", "workshop__revisions"
            ),
            workshop_id=pk,
            is_draft=True,
        )
        return Response({"data": admin_revision_payload(revision)})

    @transaction.atomic
    def partial_update(self, request, pk=None):
        revision = get_object_or_404(
            WorkshopRevision.objects.select_for_update().select_related("workshop"),
            workshop_id=pk,
            is_draft=True,
        )
        serializer = WorkshopDraftWriteSerializer(
            data=request.data, partial=True, context={"revision": revision}
        )
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        expected_version = data.pop("version", None)
        if expected_version is None or expected_version != revision.version:
            return Response(
                {"errors": [{"code": "version_conflict"}]}, status=status.HTTP_409_CONFLICT
            )
        translations = data.pop("translations", None)
        for field, value in data.items():
            setattr(revision, field, value)
        revision.version += 1
        try:
            revision.full_clean()
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        revision.save()
        if translations is not None:
            apply_translation_data(revision, translations)
            sync_workshop_locale_and_slug(revision)
        audit(actor=request.user, action="workshop.update", entity=revision.workshop)
        revision = WorkshopRevision.objects.prefetch_related("translations").get(pk=revision.pk)
        return Response({"data": admin_revision_payload(revision)})

    @transaction.atomic
    def destroy(self, request, pk=None):
        workshop = get_object_or_404(Workshop.objects.select_for_update(), pk=pk)
        if workshop.revisions.filter(is_published=True).exists():
            unpublish_workshop(workshop, actor=request.user)
            workshop.archived_at = timezone.now()
            workshop.save(update_fields=["archived_at", "updated_at"])
            audit(actor=request.user, action="workshop.archive", entity=workshop)
        else:
            audit(actor=request.user, action="workshop.delete_draft", entity=workshop)
            workshop.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        serializer = WorkshopPublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        revision = get_object_or_404(
            WorkshopRevision,
            pk=serializer.validated_data["revision_id"],
            workshop_id=pk,
        )
        try:
            published, draft = publish_revision(
                revision,
                actor=request.user,
                expected_version=serializer.validated_data["version"],
            )
        except RevisionConflict:
            return Response(
                {"errors": [{"code": "version_conflict"}]},
                status=status.HTTP_409_CONFLICT,
            )
        except (DjangoValidationError, ValueError) as error:
            raise ValidationError({"publish": str(error)}) from error
        draft = WorkshopRevision.objects.prefetch_related("translations").get(pk=draft.pk)
        return Response(
            {
                "data": {
                    "published_revision_id": str(published.pk),
                    "draft": admin_revision_payload(draft),
                }
            }
        )

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        workshop = get_object_or_404(Workshop, pk=pk)
        if not unpublish_workshop(workshop, actor=request.user):
            raise ValidationError({"publish": "Workshop is not published."})
        return Response({"data": {"unpublished": True}})

    @action(detail=True, methods=["post"], url_path="preview-token")
    def preview_token(self, request, pk=None):
        revision = get_object_or_404(WorkshopRevision, workshop_id=pk, is_draft=True)
        return Response(
            {
                "data": {
                    "token": create_preview_token(revision),
                    "expires_in": 600,
                }
            }
        )


class AdminTestimonialViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def list(self, request):
        query = AdminTestimonialQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        queryset = testimonial_queryset().order_by("sort_order", "-updated_at", "id")
        search = data["search"].strip()
        if search:
            queryset = queryset.filter(
                Q(display_name__icontains=search)
                | Q(translations__role__icontains=search)
                | Q(translations__location__icontains=search)
                | Q(translations__quote__icontains=search)
                | Q(translations__full_story__icontains=search)
            ).distinct()
        if data["status"]:
            queryset = queryset.filter(status=data["status"])
        if data["type"]:
            queryset = queryset.filter(type=data["type"])
        items, meta = testimonial_page(queryset, data["page"], data["page_size"])
        return admin_no_store(
            Response(
                {
                    "data": [admin_testimonial_payload(item, request) for item in items],
                    "meta": meta,
                }
            )
        )

    def create(self, request):
        serializer = TestimonialWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            testimonial = create_testimonial(
                actor=request.user,
                data=serializer.validated_data,
            )
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        return admin_no_store(
            Response(
                {"data": admin_testimonial_payload(testimonial, request)},
                status=status.HTTP_201_CREATED,
            )
        )

    def retrieve(self, request, pk=None):
        testimonial = get_object_or_404(testimonial_queryset(), pk=pk)
        return admin_no_store(Response({"data": admin_testimonial_payload(testimonial, request)}))

    def partial_update(self, request, pk=None):
        get_object_or_404(Testimonial, pk=pk)
        serializer = TestimonialUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        expected_version = data.pop("version")
        try:
            testimonial = update_testimonial(
                testimonial_id=pk,
                actor=request.user,
                expected_version=expected_version,
                data=data,
            )
        except TestimonialTransitionConflict as error:
            return admin_no_store(
                Response(
                    {"errors": [{"code": error.code}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        return admin_no_store(Response({"data": admin_testimonial_payload(testimonial, request)}))

    def _transition(self, request, pk, operation):
        get_object_or_404(Testimonial, pk=pk)
        serializer = TestimonialActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            testimonial = operation(
                testimonial_id=pk,
                actor=request.user,
                expected_version=serializer.validated_data["version"],
            )
        except TestimonialTransitionConflict as error:
            return admin_no_store(
                Response(
                    {"errors": [{"code": error.code}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        except DjangoValidationError as error:
            raise ValidationError(error.message_dict) from error
        return admin_no_store(Response({"data": admin_testimonial_payload(testimonial, request)}))

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        return self._transition(request, pk, publish_testimonial)

    @action(detail=True, methods=["post"])
    def hide(self, request, pk=None):
        return self._transition(request, pk, hide_testimonial)

    @action(detail=True, methods=["post"])
    def unpublish(self, request, pk=None):
        return self._transition(request, pk, unpublish_testimonial)

    @action(
        detail=False,
        methods=["post"],
        url_path="media",
        parser_classes=[MultiPartParser, FormParser],
    )
    def media(self, request):
        upload = request.FILES.get("image")
        if upload is None:
            raise ValidationError({"image": "An image is required."})
        try:
            asset = store_testimonial_image(upload, actor=request.user)
        except ValueError as error:
            raise ValidationError({"image": str(error)}) from error
        audit(actor=request.user, action="testimonial.media.upload", entity=asset)
        return admin_no_store(
            Response(
                {"data": media_payload(asset, request)},
                status=status.HTTP_201_CREATED,
            )
        )


class AdminMediaUploadView(APIView):
    permission_classes = [IsWebsiteAdmin]
    parser_classes = [MultiPartParser, FormParser]
    storage_prefix = "workshops"
    audit_action = "media.upload"

    def post(self, request):
        upload = request.FILES.get("image")
        if upload is None:
            raise ValidationError({"image": "An image is required."})
        try:
            asset = store_media_image(
                upload,
                actor=request.user,
                storage_prefix=self.storage_prefix,
            )
        except ValueError as error:
            raise ValidationError({"image": str(error)}) from error
        audit(actor=request.user, action=self.audit_action, entity=asset)
        return Response({"data": media_payload(asset, request)}, status=status.HTTP_201_CREATED)


class AdminTrackerFarmMediaUploadView(AdminMediaUploadView):
    storage_prefix = "farms"
    audit_action = "tracker_farm.media.upload"


class AdminTrackerFarmMediaDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def delete(self, request, media_id):
        try:
            delete_temporary_tracker_farm_media(media_id=media_id, actor=request.user)
        except MediaAsset.DoesNotExist as error:
            raise NotFound("Tracker farm media was not found.") from error
        except TrackerFarmMediaStorageMismatch as error:
            raise ValidationError(
                {"media": "Only media stored for tracker farms can be deleted."}
            ) from error
        except TrackerFarmMediaReferenced:
            return admin_no_store(
                Response(
                    {"errors": [{"code": "media_asset_referenced"}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        return admin_no_store(Response(status=status.HTTP_204_NO_CONTENT))


class AdminDashboardView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        now = timezone.now()
        return Response(
            {
                "data": {
                    "upcoming": published_queryset().filter(end_at__gt=now).count(),
                    "drafts": WorkshopRevision.objects.filter(is_draft=True).count(),
                    "incomplete_translations": WorkshopTranslation.objects.filter(
                        revision__is_draft=True, is_ready=False
                    ).count(),
                    "recent_activity": [
                        {
                            "action": item.action,
                            "entity_type": item.entity_type,
                            "entity_id": item.entity_id,
                            "created_at": item.created_at.isoformat(),
                        }
                        for item in ContentAuditLog.objects.all()[:10]
                    ],
                }
            }
        )
