import hashlib
import hmac
import io
import json
import logging
import time
import uuid

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.signing import TimestampSigner
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image, UnidentifiedImageError

from content.models import (
    SUPPORTED_LOCALES,
    ContentAuditLog,
    MediaAsset,
    Testimonial,
    TestimonialStatus,
    TestimonialTranslation,
    TestimonialType,
    TrackerFarm,
    Workshop,
    WorkshopRevision,
    WorkshopTranslation,
)
from content.validators import tiptap_has_content, validate_tiptap_document

logger = logging.getLogger(__name__)
preview_signer = TimestampSigner(salt="nfv-workshop-preview")


class RevisionConflict(Exception):
    """The requested draft revision is no longer current."""


class TestimonialTransitionConflict(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class TrackerFarmVersionConflict(ValueError):
    """A tracker farm changed after the administrator loaded it."""


def translation_is_ready(translation: WorkshopTranslation) -> bool:
    return bool(
        translation.title.strip()
        and translation.summary.strip()
        and tiptap_has_content(translation.body)
    )


def unique_workshop_slug(title: str, *, exclude_workshop_id=None) -> str:
    normalized = title.replace("đ", "d").replace("Đ", "D")
    base = slugify(normalized)[:170].strip("-") or "workshop"
    queryset = Workshop.objects.all()
    if exclude_workshop_id:
        queryset = queryset.exclude(pk=exclude_workshop_id)
    candidate = base
    suffix = 2
    while queryset.filter(slug=candidate).exists():
        suffix_text = f"-{suffix}"
        candidate = f"{base[: 180 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def audit(*, actor, action, entity, context=None):
    ContentAuditLog.objects.create(
        actor=actor,
        action=action,
        entity_type=entity._meta.model_name,
        entity_id=str(entity.pk),
        context=context or {},
    )


def clone_revision(source: WorkshopRevision, *, actor) -> WorkshopRevision:
    clone = WorkshopRevision.objects.create(
        workshop=source.workshop,
        revision_number=source.revision_number + 1,
        is_draft=True,
        default_locale=source.default_locale,
        start_at=source.start_at,
        end_at=source.end_at,
        event_timezone=source.event_timezone,
        registration_url=source.registration_url,
        created_by=actor,
    )
    WorkshopTranslation.objects.bulk_create(
        [
            WorkshopTranslation(
                revision=clone,
                locale=item.locale,
                title=item.title,
                summary=item.summary,
                body=item.body,
                is_ready=item.is_ready,
            )
            for item in source.translations.all()
        ]
    )
    return clone


def validate_revision_for_publish(revision: WorkshopRevision):
    revision.full_clean()
    translations = list(revision.translations.filter(is_ready=True))
    if not translations:
        raise ValueError("At least one ready translation is required.")
    if revision.default_locale not in {item.locale for item in translations}:
        raise ValueError("The default locale must have a ready translation.")
    for item in translations:
        item.full_clean()
        missing = [field for field in ("title", "summary") if not getattr(item, field).strip()]
        if not tiptap_has_content(item.body):
            missing.append("body")
        if missing:
            raise ValueError(f"{item.locale} translation is missing: {', '.join(missing)}")
        validate_tiptap_document(item.body)


@transaction.atomic
def publish_revision(revision: WorkshopRevision, *, actor, expected_version: int):
    revision = (
        WorkshopRevision.objects.select_for_update()
        .filter(pk=revision.pk, workshop_id=revision.workshop_id, is_draft=True)
        .first()
    )
    if revision is None or revision.version != expected_version:
        raise RevisionConflict
    validate_revision_for_publish(revision)
    WorkshopRevision.objects.filter(workshop=revision.workshop, is_published=True).update(
        is_published=False
    )
    revision.is_draft = False
    revision.is_published = True
    revision.published_at = timezone.now()
    revision.version += 1
    revision.save(
        update_fields=["is_draft", "is_published", "published_at", "version", "updated_at"]
    )
    new_draft = clone_revision(revision, actor=actor)
    audit(actor=actor, action="workshop.publish", entity=revision.workshop)
    transaction.on_commit(
        lambda: request_frontend_revalidation(["workshops", f"workshop:{revision.workshop.slug}"])
    )
    return revision, new_draft


@transaction.atomic
def unpublish_workshop(workshop, *, actor):
    updated = WorkshopRevision.objects.filter(workshop=workshop, is_published=True).update(
        is_published=False
    )
    if updated:
        audit(actor=actor, action="workshop.unpublish", entity=workshop)
        transaction.on_commit(
            lambda: request_frontend_revalidation(["workshops", f"workshop:{workshop.slug}"])
        )
    return bool(updated)


def create_preview_token(revision: WorkshopRevision) -> str:
    return preview_signer.sign(str(revision.pk))


def read_preview_token(token: str, max_age=600) -> WorkshopRevision:
    revision_id = preview_signer.unsign(token, max_age=max_age)
    return WorkshopRevision.objects.prefetch_related("translations").get(
        pk=revision_id, is_draft=True
    )


def request_frontend_revalidation(tags):
    url = settings.CONTENT_FRONTEND_REVALIDATE_URL
    secret = settings.CONTENT_FRONTEND_REVALIDATE_SECRET
    if not url or len(secret) < 32:
        return False
    body = json.dumps({"tags": sorted(set(tags))}, separators=(",", ":")).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    try:
        response = requests.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-NFV-Timestamp": timestamp,
                "X-NFV-Signature": f"sha256={signature}",
            },
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException:
        logger.warning("Frontend content cache revalidation failed", exc_info=True)
        return False
    return True


def tracker_farm_queryset():
    return TrackerFarm.objects.select_related("image").order_by("sort_order", "created_at", "id")


def _revalidate_tracker_farms():
    transaction.on_commit(lambda: request_frontend_revalidation(["tracker-farms"]))


@transaction.atomic
def create_tracker_farm(*, actor, data):
    farms = list(TrackerFarm.objects.select_for_update().order_by("sort_order", "created_at", "id"))
    values = dict(data)
    if "image_id" in values:
        values["image"] = values.pop("image_id")
    if "sort_order" not in values:
        values["sort_order"] = max((farm.sort_order for farm in farms), default=-1) + 1
    farm = TrackerFarm.objects.create(**values)
    audit(actor=actor, action="tracker_farm.create", entity=farm)
    _revalidate_tracker_farms()
    return farm


@transaction.atomic
def update_tracker_farm(*, farm_id, actor, expected_version, data):
    farm = TrackerFarm.objects.select_for_update().get(pk=farm_id)
    if farm.version != expected_version:
        raise TrackerFarmVersionConflict
    values = dict(data)
    if "image_id" in values:
        values["image"] = values.pop("image_id")
    for field, value in values.items():
        setattr(farm, field, value)
    farm.version += 1
    farm.save()
    audit(actor=actor, action="tracker_farm.update", entity=farm)
    _revalidate_tracker_farms()
    return farm


@transaction.atomic
def delete_tracker_farm(*, farm_id, actor):
    farm = TrackerFarm.objects.select_for_update().get(pk=farm_id)
    audit(actor=actor, action="tracker_farm.delete", entity=farm)
    farm.delete()
    _revalidate_tracker_farms()


@transaction.atomic
def reorder_tracker_farms(*, actor, items):
    farms = list(TrackerFarm.objects.select_for_update().order_by("sort_order", "created_at", "id"))
    by_id = {farm.pk: farm for farm in farms}
    submitted_ids = {item["id"] for item in items}
    if submitted_ids != set(by_id):
        raise ValidationError(
            {"items": "Reorder must include every existing tracker farm exactly once."}
        )
    if any(by_id[item["id"]].version != item["version"] for item in items):
        raise TrackerFarmVersionConflict

    updated_at = timezone.now()
    for item in items:
        farm = by_id[item["id"]]
        farm.sort_order = item["sort_order"]
        farm.version += 1
        farm.updated_at = updated_at
    TrackerFarm.objects.bulk_update(farms, ["sort_order", "version", "updated_at"])
    audit(
        actor=actor,
        action="tracker_farm.reorder",
        entity=farms[0],
        context={"farm_ids": [str(item["id"]) for item in items]},
    )
    _revalidate_tracker_farms()
    return tracker_farm_queryset()


def store_media_image(upload, *, actor, storage_prefix: str) -> MediaAsset:
    if storage_prefix not in {"farms", "rewards", "testimonials", "workshops"}:
        raise ValueError("Unsupported media storage prefix.")
    if upload.size > settings.CONTENT_MAX_UPLOAD_BYTES:
        raise ValueError("Image exceeds the upload size limit.")
    try:
        image = Image.open(upload)
        if image.width * image.height > settings.CONTENT_MAX_IMAGE_PIXELS:
            raise ValueError("Image dimensions are too large.")
        image.verify()
        upload.seek(0)
        image = Image.open(upload)
        if image.width * image.height > settings.CONTENT_MAX_IMAGE_PIXELS:
            raise ValueError("Image dimensions are too large.")
        image.load()
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as error:
        raise ValueError("Unsupported or corrupt image.") from error
    if image.format not in {"JPEG", "PNG", "WEBP"}:
        raise ValueError("Only JPEG, PNG and WebP images are allowed.")

    image = image.convert("RGB")
    asset_id = uuid.uuid4()
    variants = []
    largest_name = None
    largest_dimensions = None
    for target_width in (480, 960, 1600):
        width = min(target_width, image.width)
        if any(item["width"] == width for item in variants):
            continue
        height = round(image.height * width / image.width)
        resized = image.resize((width, height), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        resized.save(output, "WEBP", quality=84, method=6)
        name = default_storage.save(
            f"{storage_prefix}/{timezone.now():%Y/%m}/{asset_id}-{width}.webp",
            ContentFile(output.getvalue()),
        )
        variants.append({"file": name, "width": width, "height": height})
        largest_name = name
        largest_dimensions = (width, height)
        if width == image.width:
            break

    return MediaAsset.objects.create(
        id=asset_id,
        file=largest_name,
        variants=variants,
        width=largest_dimensions[0],
        height=largest_dimensions[1],
        original_name=upload.name[:255],
        created_by=actor,
    )


def store_workshop_image(upload, *, actor) -> MediaAsset:
    return store_media_image(upload, actor=actor, storage_prefix="workshops")


def store_reward_image(upload, *, actor) -> MediaAsset:
    return store_media_image(upload, actor=actor, storage_prefix="rewards")


def store_testimonial_image(upload, *, actor) -> MediaAsset:
    return store_media_image(upload, actor=actor, storage_prefix="testimonials")


def validate_testimonial_data(data, *, partial=False):
    errors = {}
    allowed_fields = {
        "type",
        "display_name",
        "image_id",
        "featured",
        "sort_order",
        "consent_confirmed",
        "translations",
    }
    unknown_fields = sorted(set(data) - allowed_fields)
    if unknown_fields:
        errors.update({field: "This field is not allowed." for field in unknown_fields})
    testimonial_type = data.get("type")
    if not partial and testimonial_type not in TestimonialType.values:
        errors["type"] = "A supported testimonial type is required."
    if testimonial_type is not None and testimonial_type not in TestimonialType.values:
        errors["type"] = "Unsupported testimonial type."
    display_name = data.get("display_name")
    if not partial and not (display_name or "").strip():
        errors["display_name"] = "Display name is required."
    if display_name is not None and not display_name.strip():
        errors["display_name"] = "Display name is required."
    if data.get("sort_order", 0) < 0:
        errors["sort_order"] = "Sort order must be zero or greater."
    image = data.get("image_id")
    if "image_id" in data and image is not None and not isinstance(image, MediaAsset):
        errors["image_id"] = "A valid media asset is required."

    translations = data.get("translations")
    if translations is not None:
        locales = [item.get("locale") for item in translations]
        if len(locales) != len(set(locales)):
            errors["translations"] = "Locale must be unique."
        elif any(locale not in SUPPORTED_LOCALES for locale in locales):
            errors["translations"] = "Unsupported locale."
        translation_fields = {"locale", "role", "location", "quote", "full_story"}
        if any(set(item) - translation_fields for item in translations):
            errors["translations"] = "Translation contains unsupported fields."
    if errors:
        raise ValidationError(errors)


def _save_testimonial_translations(testimonial, translations):
    for translation_data in translations:
        data = dict(translation_data)
        locale = data.pop("locale")
        translation, _created = TestimonialTranslation.objects.update_or_create(
            testimonial=testimonial,
            locale=locale,
            defaults=data,
        )
        translation.full_clean()


def validate_testimonial_for_publish(testimonial):
    testimonial.full_clean()
    if not testimonial.consent_confirmed:
        raise ValidationError({"consent_confirmed": "Consent is required before publishing."})
    translations = {item.locale: item for item in testimonial.translations.all()}
    errors = {}
    for locale in ("vi", "en"):
        translation = translations.get(locale)
        if translation is None:
            errors[locale] = "Translation is required."
            continue
        translation.full_clean()
        missing = [
            field
            for field in ("role", "location", "quote", "full_story")
            if not getattr(translation, field).strip()
        ]
        if missing:
            errors[locale] = f"Translation is missing: {', '.join(missing)}."
    if errors:
        raise ValidationError(
            {"translations": [f"{locale}: {message}" for locale, message in errors.items()]}
        )


def testimonial_queryset():
    return Testimonial.objects.select_related("image", "created_by", "updated_by").prefetch_related(
        "translations"
    )


@transaction.atomic
def create_testimonial(*, actor, data):
    data = dict(data)
    validate_testimonial_data(data)
    translations = data.pop("translations", [])
    image = data.pop("image_id", None)
    testimonial = Testimonial(
        image=image,
        created_by=actor,
        updated_by=actor,
        **data,
    )
    testimonial.full_clean()
    testimonial.save()
    _save_testimonial_translations(testimonial, translations)
    audit(actor=actor, action="testimonial.create", entity=testimonial)
    return testimonial_queryset().get(pk=testimonial.pk)


def _assert_testimonial_version(testimonial, expected_version):
    if testimonial.version != expected_version:
        raise TestimonialTransitionConflict(
            "version_conflict", "Testimonial was changed by another administrator."
        )


@transaction.atomic
def update_testimonial(*, testimonial_id, actor, expected_version, data):
    data = dict(data)
    validate_testimonial_data(data, partial=True)
    testimonial = Testimonial.objects.select_for_update().get(pk=testimonial_id)
    _assert_testimonial_version(testimonial, expected_version)
    translations = data.pop("translations", None)
    if "image_id" in data:
        testimonial.image = data.pop("image_id")
    for field, value in data.items():
        setattr(testimonial, field, value)
    testimonial.updated_by = actor
    testimonial.version += 1
    testimonial.full_clean()
    testimonial.save()
    if translations is not None:
        _save_testimonial_translations(testimonial, translations)
    if testimonial.status == TestimonialStatus.PUBLISHED:
        testimonial = testimonial_queryset().get(pk=testimonial.pk)
        validate_testimonial_for_publish(testimonial)
        transaction.on_commit(
            lambda: request_frontend_revalidation(["testimonials", f"testimonial:{testimonial.pk}"])
        )
    audit(actor=actor, action="testimonial.update", entity=testimonial)
    return testimonial_queryset().get(pk=testimonial.pk)


@transaction.atomic
def publish_testimonial(*, testimonial_id, actor, expected_version):
    testimonial = Testimonial.objects.select_for_update().get(pk=testimonial_id)
    _assert_testimonial_version(testimonial, expected_version)
    if testimonial.status == TestimonialStatus.PUBLISHED:
        raise TestimonialTransitionConflict(
            "testimonial_already_published", "Testimonial is already published."
        )
    testimonial.status = TestimonialStatus.PUBLISHED
    testimonial.published_at = timezone.now()
    testimonial.updated_by = actor
    testimonial.version += 1
    validate_testimonial_for_publish(testimonial)
    testimonial.save(
        update_fields=["status", "published_at", "updated_by", "version", "updated_at"]
    )
    audit(actor=actor, action="testimonial.publish", entity=testimonial)
    transaction.on_commit(
        lambda: request_frontend_revalidation(["testimonials", f"testimonial:{testimonial.pk}"])
    )
    return testimonial_queryset().get(pk=testimonial.pk)


@transaction.atomic
def hide_testimonial(*, testimonial_id, actor, expected_version):
    testimonial = Testimonial.objects.select_for_update().get(pk=testimonial_id)
    _assert_testimonial_version(testimonial, expected_version)
    if testimonial.status == TestimonialStatus.HIDDEN:
        raise TestimonialTransitionConflict(
            "testimonial_already_hidden", "Testimonial is already hidden."
        )
    testimonial.status = TestimonialStatus.HIDDEN
    testimonial.updated_by = actor
    testimonial.version += 1
    testimonial.full_clean()
    testimonial.save(update_fields=["status", "updated_by", "version", "updated_at"])
    audit(actor=actor, action="testimonial.hide", entity=testimonial)
    transaction.on_commit(
        lambda: request_frontend_revalidation(["testimonials", f"testimonial:{testimonial.pk}"])
    )
    return testimonial_queryset().get(pk=testimonial.pk)


@transaction.atomic
def unpublish_testimonial(*, testimonial_id, actor, expected_version):
    testimonial = Testimonial.objects.select_for_update().get(pk=testimonial_id)
    _assert_testimonial_version(testimonial, expected_version)
    if testimonial.status == TestimonialStatus.DRAFT:
        raise TestimonialTransitionConflict(
            "testimonial_already_draft", "Testimonial is already a draft."
        )
    testimonial.status = TestimonialStatus.DRAFT
    testimonial.updated_by = actor
    testimonial.version += 1
    testimonial.full_clean()
    testimonial.save(update_fields=["status", "updated_by", "version", "updated_at"])
    audit(actor=actor, action="testimonial.unpublish", entity=testimonial)
    transaction.on_commit(
        lambda: request_frontend_revalidation(["testimonials", f"testimonial:{testimonial.pk}"])
    )
    return testimonial_queryset().get(pk=testimonial.pk)
