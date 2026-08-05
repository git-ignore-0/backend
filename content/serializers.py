from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from rest_framework import serializers

from content.models import (
    ExternalLink,
    MediaAsset,
    TestimonialStatus,
    TestimonialType,
)
from content.validators import validate_tiptap_document


def media_payload(asset: MediaAsset, request):
    def absolute(path):
        url = default_storage.url(path)
        return request.build_absolute_uri(url)

    return {
        "id": str(asset.pk),
        "url": absolute(asset.file.name),
        "width": asset.width,
        "height": asset.height,
        "variants": [{**item, "url": absolute(item["file"])} for item in asset.variants],
    }


def choose_translation(revision, locale):
    translations = [item for item in revision.translations.all() if item.is_ready]
    by_locale = {item.locale: item for item in translations}
    selected = by_locale.get(locale) or by_locale.get(revision.default_locale)
    if selected is None and translations:
        selected = translations[0]
    return selected, sorted(by_locale)


def public_workshop_payload(revision, locale, include_body=True, effective_at=None):
    translation, available_locales = choose_translation(revision, locale)
    if translation is None:
        return None
    result = {
        "id": str(revision.workshop_id),
        "slug": revision.workshop.slug,
        "requested_locale": locale,
        "content_locale": translation.locale,
        "available_locales": available_locales,
        "is_fallback": translation.locale != locale,
        "default_locale": revision.default_locale,
        "start_at": revision.start_at.isoformat(),
        "end_at": revision.end_at.isoformat(),
        "event_timezone": revision.event_timezone,
        "status": (
            revision.effective_status
            if effective_at is None
            else revision.effective_status_at(effective_at)
        ),
        "registration_url": revision.registration_url or None,
        "title": translation.title,
        "summary": translation.summary,
    }
    if include_body:
        result["body"] = translation.body
    return result


class ExternalLinkSerializer(serializers.ModelSerializer):
    url = serializers.URLField(max_length=500, allow_blank=True)

    class Meta:
        model = ExternalLink
        fields = ["id", "kind", "label", "url", "position", "is_enabled"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        if attrs.get("is_enabled") and not attrs.get("url"):
            raise serializers.ValidationError({"url": "URL is required when the link is enabled."})
        return attrs


class SiteSettingsWriteSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1)
    email = serializers.EmailField(allow_blank=True)
    is_email_enabled = serializers.BooleanField()
    phone_display = serializers.CharField(max_length=50, allow_blank=True)
    is_phone_enabled = serializers.BooleanField()
    links = ExternalLinkSerializer(many=True)

    def validate_links(self, value):
        standard_kinds = [item["kind"] for item in value if item["kind"] != "other"]
        if len(standard_kinds) != len(set(standard_kinds)):
            raise serializers.ValidationError("Each standard link type can only appear once.")
        return value


class WorkshopTranslationWriteSerializer(serializers.Serializer):
    locale = serializers.ChoiceField(choices=["en", "vi"])
    title = serializers.CharField(max_length=240, allow_blank=True, required=False)
    summary = serializers.CharField(max_length=800, allow_blank=True, required=False)
    body = serializers.JSONField(required=False)

    def validate_body(self, value):
        try:
            validate_tiptap_document(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(
                error.message_dict.get("body", error.messages)
            ) from error
        return value


class WorkshopDraftWriteSerializer(serializers.Serializer):
    version = serializers.IntegerField(min_value=1, required=False)
    default_locale = serializers.ChoiceField(choices=["en", "vi"], required=False)
    start_at = serializers.DateTimeField(required=False)
    end_at = serializers.DateTimeField(required=False)
    event_timezone = serializers.CharField(max_length=64, required=False)
    registration_url = serializers.URLField(max_length=500, allow_blank=True, required=False)
    translations = WorkshopTranslationWriteSerializer(many=True, required=False)

    def validate_event_timezone(self, value):
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise serializers.ValidationError("Unknown IANA timezone.") from error
        return value

    def validate(self, data):
        start = data.get("start_at") or getattr(self.context.get("revision"), "start_at", None)
        end = data.get("end_at") or getattr(self.context.get("revision"), "end_at", None)
        if start and end and end <= start:
            raise serializers.ValidationError({"end_at": "End time must be after start time."})
        translations = data.get("translations")
        if translations is not None:
            locales = [item["locale"] for item in translations]
            if len(locales) != len(set(locales)):
                raise serializers.ValidationError({"translations": "Locale must be unique."})
        return data


class WorkshopPublishSerializer(serializers.Serializer):
    revision_id = serializers.UUIDField()
    version = serializers.IntegerField(min_value=1)


class PublicWorkshopQuerySerializer(serializers.Serializer):
    locale = serializers.ChoiceField(choices=["en", "vi"], default="vi")
    period = serializers.ChoiceField(choices=["upcoming", "past"], default="upcoming")
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=12)
    page = serializers.IntegerField(min_value=1, default=1)


class TestimonialStrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = sorted(set(data.keys()) - set(self.fields))
            if unknown:
                raise serializers.ValidationError(
                    {field: "This field is not allowed." for field in unknown}
                )
        return super().to_internal_value(data)


class TestimonialTranslationWriteSerializer(TestimonialStrictSerializer):
    locale = serializers.ChoiceField(choices=["vi", "en"])
    role = serializers.CharField(
        max_length=240,
        allow_blank=True,
        required=False,
        default="",
        trim_whitespace=True,
    )
    location = serializers.CharField(
        max_length=240,
        allow_blank=True,
        required=False,
        default="",
        trim_whitespace=True,
    )
    quote = serializers.CharField(
        max_length=800,
        allow_blank=True,
        required=False,
        default="",
        trim_whitespace=True,
    )
    full_story = serializers.CharField(
        max_length=20_000,
        allow_blank=True,
        required=False,
        default="",
        trim_whitespace=True,
    )


class TestimonialWriteSerializer(TestimonialStrictSerializer):
    type = serializers.ChoiceField(choices=TestimonialType.choices)
    display_name = serializers.CharField(max_length=240, trim_whitespace=True)
    image_id = serializers.UUIDField(required=False, allow_null=True)
    featured = serializers.BooleanField(required=False, default=False)
    sort_order = serializers.IntegerField(min_value=0, required=False, default=0)
    consent_confirmed = serializers.BooleanField(required=False, default=False)
    translations = TestimonialTranslationWriteSerializer(many=True, required=False, default=list)

    def validate_display_name(self, value):
        if not value:
            raise serializers.ValidationError("Display name is required.")
        return value

    def validate_image_id(self, value):
        if value is None:
            return None
        try:
            return MediaAsset.objects.get(pk=value)
        except MediaAsset.DoesNotExist as error:
            raise serializers.ValidationError("Media asset does not exist.") from error

    def validate_translations(self, value):
        locales = [item["locale"] for item in value]
        if len(locales) != len(set(locales)):
            raise serializers.ValidationError("Locale must be unique.")
        return value


class TestimonialUpdateSerializer(TestimonialWriteSerializer):
    version = serializers.IntegerField(min_value=1, required=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if "version" not in attrs:
            raise serializers.ValidationError({"version": "This field is required."})
        return attrs


class TestimonialActionSerializer(TestimonialStrictSerializer):
    version = serializers.IntegerField(min_value=1)


class AdminTestimonialQuerySerializer(serializers.Serializer):
    search = serializers.CharField(max_length=240, allow_blank=True, default="")
    status = serializers.ChoiceField(
        choices=TestimonialStatus.choices,
        allow_blank=True,
        default="",
    )
    type = serializers.ChoiceField(
        choices=TestimonialType.choices,
        allow_blank=True,
        default="",
    )
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=20)
    page = serializers.IntegerField(min_value=1, max_value=10_000, default=1)


class PublicTestimonialQuerySerializer(serializers.Serializer):
    locale = serializers.ChoiceField(choices=["vi", "en"], default="vi")
    type = serializers.ChoiceField(
        choices=["all", *TestimonialType.values],
        default="all",
    )
    page_size = serializers.IntegerField(min_value=1, max_value=50, default=20)
    page = serializers.IntegerField(min_value=1, max_value=10_000, default=1)


def admin_translation_payload(item):
    return {
        "locale": item.locale,
        "title": item.title,
        "summary": item.summary,
        "body": item.body,
        "is_ready": item.is_ready,
    }


def admin_revision_payload(revision):
    return {
        "id": str(revision.workshop_id),
        "revision_id": str(revision.pk),
        "revision_number": revision.revision_number,
        "slug": revision.workshop.slug,
        "version": revision.version,
        "default_locale": revision.default_locale,
        "start_at": revision.start_at.isoformat(),
        "end_at": revision.end_at.isoformat(),
        "event_timezone": revision.event_timezone,
        "effective_status": revision.effective_status,
        "registration_url": revision.registration_url,
        "has_published_revision": revision.workshop.revisions.filter(is_published=True).exists(),
        "translations": [admin_translation_payload(item) for item in revision.translations.all()],
        "updated_at": revision.updated_at.isoformat(),
    }


def testimonial_translation_is_complete(translation) -> bool:
    return all(
        getattr(translation, field).strip() for field in ("role", "location", "quote", "full_story")
    )


def choose_testimonial_translation(testimonial, locale):
    translations = [
        item for item in testimonial.translations.all() if testimonial_translation_is_complete(item)
    ]
    by_locale = {item.locale: item for item in translations}
    selected = by_locale.get(locale) or by_locale.get("vi") or by_locale.get("en")
    return selected, sorted(by_locale)


def public_testimonial_payload(testimonial, request, locale, *, include_full_story=False):
    translation, available_locales = choose_testimonial_translation(testimonial, locale)
    if translation is None:
        return None
    data = {
        "id": str(testimonial.pk),
        "type": testimonial.type,
        "display_name": testimonial.display_name,
        "image": media_payload(testimonial.image, request) if testimonial.image else None,
        "requested_locale": locale,
        "content_locale": translation.locale,
        "available_locales": available_locales,
        "is_fallback": translation.locale != locale,
        "role": translation.role,
        "location": translation.location,
        "quote": translation.quote,
        "published_at": testimonial.published_at.isoformat(),
    }
    if include_full_story:
        data["full_story"] = translation.full_story
    return data


def admin_testimonial_payload(testimonial, request):
    def actor_payload(actor):
        if actor is None:
            return None
        return {"id": str(actor.pk), "name": actor.name, "email": actor.email}

    return {
        "id": str(testimonial.pk),
        "type": testimonial.type,
        "status": testimonial.status,
        "display_name": testimonial.display_name,
        "image": media_payload(testimonial.image, request) if testimonial.image else None,
        "featured": testimonial.featured,
        "sort_order": testimonial.sort_order,
        "published_at": (
            testimonial.published_at.isoformat() if testimonial.published_at else None
        ),
        "consent_confirmed": testimonial.consent_confirmed,
        "version": testimonial.version,
        "translations": [
            {
                "locale": item.locale,
                "role": item.role,
                "location": item.location,
                "quote": item.quote,
                "full_story": item.full_story,
                "is_complete": testimonial_translation_is_complete(item),
            }
            for item in testimonial.translations.all()
        ],
        "created_by": actor_payload(testimonial.created_by),
        "updated_by": actor_payload(testimonial.updated_by),
        "created_at": testimonial.created_at.isoformat(),
        "updated_at": testimonial.updated_at.isoformat(),
    }
