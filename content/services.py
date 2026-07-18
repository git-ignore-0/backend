import hashlib
import hmac
import io
import json
import logging
import time
import uuid

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.signing import TimestampSigner
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from PIL import Image, UnidentifiedImageError

from content.models import (
    ContentAuditLog,
    MediaAsset,
    Workshop,
    WorkshopRevision,
    WorkshopTranslation,
)
from content.validators import tiptap_has_content, validate_tiptap_document

logger = logging.getLogger(__name__)
preview_signer = TimestampSigner(salt="nfv-workshop-preview")


class RevisionConflict(Exception):
    """The requested draft revision is no longer current."""


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


def store_workshop_image(upload, *, actor) -> MediaAsset:
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
            f"workshops/{timezone.now():%Y/%m}/{asset_id}-{width}.webp",
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
