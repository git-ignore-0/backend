from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.storage import default_storage
from rest_framework import serializers

from content.models import (
    ExternalLink,
    MediaAsset,
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
