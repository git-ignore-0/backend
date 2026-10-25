import hashlib
import hmac
import io
import json
import logging
import time
import uuid
from functools import partial

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.signing import TimestampSigner
from django.db import transaction
from django.db.models import Prefetch
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
    TrackerFarmPhoto,
    Workshop,
    WorkshopRevision,
    WorkshopTranslation,
)
from content.validators import tiptap_has_content, validate_tiptap_document
from farms.models import FarmPhoto

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


class TrackerFarmMediaReferenced(ValueError):
    """A tracker farm media asset is still referenced by persisted content."""


class TrackerFarmMediaStorageMismatch(ValueError):
    """A media asset is outside the tracker farm storage namespace."""


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
    return (
        TrackerFarm.objects.select_related("image", "farm__image")
        .prefetch_related(
            Prefetch(
                "farm__photos",
                queryset=FarmPhoto.objects.select_related("image").order_by(
                    "sort_order", "created_at", "id"
                ),
            ),
        )
        .order_by("sort_order", "created_at", "id")
    )


def _revalidate_tracker_farms():
    transaction.on_commit(lambda: request_frontend_revalidation(["tracker-farms"]))


def _replace_tracker_farm_photos(farm, assets):
    TrackerFarmPhoto.objects.filter(farm=farm).delete()
    TrackerFarmPhoto.objects.bulk_create(
        [
            TrackerFarmPhoto(farm=farm, image=asset, sort_order=index)
            for index, asset in enumerate(assets)
        ]
    )
    farm.image = assets[0] if assets else None


def _sync_canonical_farm(profile):
    """Keep the legacy tracker write endpoint compatible while Farm owns shared data."""
    from farms.models import Farm, FarmPhoto

    farm = Farm.objects.select_for_update().filter(pk=profile.farm_id or profile.pk).first()
    created = farm is None
    if created:
        farm = Farm(id=profile.pk, legacy_tracker_owned=True)
    shared_changed = created
    store_presentation_changed = False
    for field in (
        "name_en",
        "name_vi",
        "location_en",
        "location_vi",
        "description_en",
        "description_vi",
    ):
        value = getattr(profile, field)
        if getattr(farm, field) != value:
            setattr(farm, field, value)
            shared_changed = True
            if field in {"name_en", "name_vi", "location_en", "location_vi"}:
                store_presentation_changed = True
    if farm.image_id != profile.image_id:
        farm.image_id = profile.image_id
        shared_changed = True
    assets = list(profile.photos.order_by("sort_order", "created_at", "id"))
    if not assets and profile.image_id:
        assets = [profile.image]
    new_photo_ids = [asset.image_id if hasattr(asset, "image_id") else asset.pk for asset in assets]
    existing_photo_ids = (
        list(
            FarmPhoto.objects.filter(farm=farm)
            .order_by("sort_order")
            .values_list("image_id", flat=True)
        )
        if not created
        else []
    )
    gallery_changed = existing_photo_ids != new_photo_ids
    if shared_changed or gallery_changed:
        if not created:
            farm.version += 1
        farm.save()
        if created:
            Farm.objects.filter(pk=farm.pk).update(
                created_at=profile.created_at, updated_at=profile.updated_at
            )
        if gallery_changed:
            FarmPhoto.objects.filter(farm=farm).delete()
            FarmPhoto.objects.bulk_create(
                [
                    FarmPhoto(farm=farm, image_id=asset_id, sort_order=index)
                    for index, asset_id in enumerate(new_photo_ids)
                ]
            )
    if profile.farm_id != farm.pk:
        profile.farm = farm
        profile.save(update_fields=["farm"])
    if farm.tracker_profile_revision < profile.version:
        Farm.objects.filter(pk=farm.pk).update(tracker_profile_revision=profile.version)
    if store_presentation_changed and not created:
        from commerce.models import Product
        from commerce.services import schedule_store_revalidation

        linked_slugs = Product.objects.filter(farm_offers__farm=farm).values_list("slug", flat=True)
        schedule_store_revalidation(slugs=linked_slugs)
    return farm


def _tracker_farm_media_ids(farm):
    candidate_ids = set(farm.photos.values_list("image_id", flat=True))
    if farm.image_id:
        candidate_ids.add(farm.image_id)
    return candidate_ids


def _lock_submitted_tracker_farm_media(values, *, additional_ids=()):
    field_name = None
    submitted_assets = []
    if "photo_ids" in values:
        field_name = "photo_ids"
        submitted_assets = values["photo_ids"]
    elif "image_id" in values and values["image_id"] is not None:
        field_name = "image_id"
        submitted_assets = [values["image_id"]]

    submitted_ids = [asset.pk for asset in submitted_assets]
    lock_ids = set(additional_ids) | set(submitted_ids)
    locked_assets = list(
        MediaAsset.objects.select_for_update().filter(pk__in=lock_ids).order_by("pk")
    )
    locked_by_id = {asset.pk: asset for asset in locked_assets}
    missing_ids = [asset_id for asset_id in submitted_ids if asset_id not in locked_by_id]
    if missing_ids:
        if field_name == "image_id":
            raise ValidationError({"image_id": "Media asset does not exist."})
        raise ValidationError(
            {
                "photo_ids": (
                    "Media assets do not exist: "
                    f"{', '.join(str(asset_id) for asset_id in missing_ids)}."
                )
            }
        )

    invalid_ids = [
        asset_id
        for asset_id in submitted_ids
        if asset_id not in additional_ids
        and (
            not locked_by_id[asset_id].file.name.startswith("farms/")
            or any(
                not str(variant.get("file") or "").startswith("farms/")
                for variant in locked_by_id[asset_id].variants
            )
        )
    ]
    if invalid_ids:
        raise ValidationError({field_name: "Choose Farm images."})

    if field_name == "photo_ids":
        values["photo_ids"] = [locked_by_id[asset_id] for asset_id in submitted_ids]
    elif field_name == "image_id":
        values["image_id"] = locked_by_id[submitted_ids[0]]


@transaction.atomic
def create_tracker_farm(*, actor, data):
    farms = list(TrackerFarm.objects.select_for_update().order_by("sort_order", "created_at", "id"))
    values = dict(data)
    _lock_submitted_tracker_farm_media(values)
    gallery_assets = None
    if "photo_ids" in values:
        gallery_assets = values.pop("photo_ids")
    elif "image_id" in values:
        legacy_image = values.pop("image_id")
        gallery_assets = [legacy_image] if legacy_image else []
    if gallery_assets is not None:
        values["image"] = gallery_assets[0] if gallery_assets else None
    if "sort_order" not in values:
        values["sort_order"] = max((farm.sort_order for farm in farms), default=-1) + 1
    farm = TrackerFarm.objects.create(**values)
    if gallery_assets is not None:
        _replace_tracker_farm_photos(farm, gallery_assets)
    _sync_canonical_farm(farm)
    audit(actor=actor, action="tracker_farm.create", entity=farm)
    _revalidate_tracker_farms()
    return farm


@transaction.atomic
def update_tracker_farm(*, farm_id, actor, expected_version, data):
    from farms.models import Farm

    canonical_id = TrackerFarm.objects.only("farm_id").get(pk=farm_id).farm_id
    if canonical_id:
        Farm.objects.select_for_update().get(pk=canonical_id)
    farm = TrackerFarm.objects.select_for_update().get(pk=farm_id)
    if farm.version != expected_version:
        raise TrackerFarmVersionConflict
    values = dict(data)
    if "photo_ids" in values or "image_id" in values:
        _lock_submitted_tracker_farm_media(
            values,
            additional_ids=_tracker_farm_media_ids(farm),
        )
    gallery_assets = None
    previous_candidate_ids = set()
    if "photo_ids" in values:
        gallery_assets = values.pop("photo_ids")
        current_photo_ids = list(
            farm.photos.order_by("sort_order", "created_at", "id").values_list(
                "image_id", flat=True
            )
        )
        submitted_photo_ids = [asset.pk for asset in gallery_assets]
        submitted_legacy_id = submitted_photo_ids[0] if submitted_photo_ids else None
        if current_photo_ids == submitted_photo_ids and farm.image_id == submitted_legacy_id:
            gallery_assets = None
        else:
            previous_candidate_ids = _tracker_farm_media_ids(farm)
    elif "image_id" in values:
        legacy_image = values.pop("image_id")
        if legacy_image is None:
            if farm.image_id or farm.photos.exists():
                gallery_assets = []
                previous_candidate_ids = _tracker_farm_media_ids(farm)
        elif farm.image_id != legacy_image.pk:
            gallery_assets = [legacy_image]
            previous_candidate_ids = _tracker_farm_media_ids(farm)
        elif not farm.photos.exists():
            gallery_assets = [legacy_image]
            previous_candidate_ids = _tracker_farm_media_ids(farm)
    for field, value in values.items():
        setattr(farm, field, value)
    if gallery_assets is not None:
        _replace_tracker_farm_photos(farm, gallery_assets)
    farm.version += 1
    farm.save()
    _sync_canonical_farm(farm)
    if gallery_assets is not None:
        retained_ids = {asset.pk for asset in gallery_assets}
        _cleanup_orphan_tracker_farm_media(
            actor=actor,
            candidate_ids=previous_candidate_ids - retained_ids,
            farm_id=farm.pk,
            reason="gallery_update",
        )
    audit(actor=actor, action="tracker_farm.update", entity=farm)
    _revalidate_tracker_farms()
    return farm


def _media_asset_has_references(asset):
    for relation in asset._meta.related_objects:
        accessor_name = relation.get_accessor_name()
        if not accessor_name or accessor_name == "+":
            continue
        if relation.one_to_one:
            try:
                getattr(asset, accessor_name)
            except relation.related_model.DoesNotExist:
                continue
            return True
        related = getattr(asset, accessor_name)
        if related.exists():
            return True
    return False


def _delete_media_files(file_names):
    for file_name in file_names:
        try:
            default_storage.delete(file_name)
        except Exception:
            logger.warning("Orphan media file cleanup failed for %s", file_name, exc_info=True)


def _media_asset_file_names(asset):
    return {
        file_name
        for file_name in [
            asset.file.name,
            *(variant.get("file") for variant in asset.variants),
        ]
        if file_name
    }


@transaction.atomic
def delete_orphan_media_asset(*, asset_id, storage_prefix):
    """Delete an unreferenced asset belonging to one media namespace."""
    asset = MediaAsset.objects.select_for_update().filter(pk=asset_id).first()
    if asset is None:
        return False
    file_names = _media_asset_file_names(asset)
    prefix = f"{storage_prefix}/"
    if not asset.file.name.startswith(prefix) or any(
        not file_name.startswith(prefix) for file_name in file_names
    ):
        return False
    if _media_asset_has_references(asset):
        return False
    asset.delete()
    transaction.on_commit(partial(_delete_media_files, file_names))
    return True


def _cleanup_orphan_tracker_farm_media(
    *,
    actor,
    candidate_ids,
    reason,
    farm_id=None,
    required_storage_prefix=None,
    reject_referenced=False,
    require_asset=False,
):
    assets = list(
        MediaAsset.objects.select_for_update().filter(pk__in=candidate_ids).order_by("pk")
    )
    if require_asset and not assets:
        raise MediaAsset.DoesNotExist
    for asset in assets:
        file_names = _media_asset_file_names(asset)
        if required_storage_prefix:
            prefix = f"{required_storage_prefix}/"
            if not asset.file.name.startswith(prefix) or any(
                not file_name.startswith(prefix) for file_name in file_names
            ):
                raise TrackerFarmMediaStorageMismatch
        if _media_asset_has_references(asset):
            if reject_referenced:
                raise TrackerFarmMediaReferenced
            continue
        context = {"reason": reason}
        if farm_id is not None:
            context["farm_id"] = str(farm_id)
        audit(
            actor=actor,
            action="tracker_farm.media.orphan_cleanup",
            entity=asset,
            context=context,
        )
        asset.delete()
        transaction.on_commit(partial(_delete_media_files, file_names))


@transaction.atomic
def delete_temporary_tracker_farm_media(*, media_id, actor):
    _cleanup_orphan_tracker_farm_media(
        actor=actor,
        candidate_ids={media_id},
        reason="temporary_upload_cancel",
        required_storage_prefix="farms",
        reject_referenced=True,
        require_asset=True,
    )


@transaction.atomic
def delete_tracker_farm(*, farm_id, actor):
    from farms.models import Farm, FarmPhoto, FarmProduct

    canonical_id = TrackerFarm.objects.only("farm_id").get(pk=farm_id).farm_id
    if canonical_id:
        Farm.objects.select_for_update().get(pk=canonical_id)
    farm = TrackerFarm.objects.select_for_update().get(pk=farm_id)
    candidate_ids = _tracker_farm_media_ids(farm)
    if canonical_id:
        canonical = Farm.objects.select_for_update().get(pk=canonical_id)
        canonical.tracker_profile_revision = max(canonical.tracker_profile_revision, farm.version)
        canonical.save(update_fields=["tracker_profile_revision"])
        candidate_ids.update(canonical.photos.values_list("image_id", flat=True))
        if canonical.image_id:
            candidate_ids.add(canonical.image_id)
    audit(actor=actor, action="tracker_farm.delete", entity=farm)
    farm.photos.all().delete()
    farm.delete()
    if (
        canonical_id
        and canonical.legacy_tracker_owned
        and not FarmProduct.objects.filter(farm_id=canonical_id).exists()
    ):
        FarmPhoto.objects.filter(farm_id=canonical_id).delete()
        canonical.delete()
    _cleanup_orphan_tracker_farm_media(
        actor=actor,
        candidate_ids=candidate_ids,
        farm_id=farm_id,
        reason="farm_delete",
    )
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
    if storage_prefix not in {"farms", "products", "rewards", "testimonials", "workshops"}:
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
