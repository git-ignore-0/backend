from django.db import IntegrityError, transaction
from django.db.models import (
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    Min,
    OuterRef,
    Prefetch,
    Q,
    Sum,
    Value,
)
from django.db.models.functions import Coalesce
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from commerce.models import Product
from commerce.services import schedule_store_revalidation
from content.models import MediaAsset
from content.services import audit, delete_orphan_media_asset, request_frontend_revalidation
from farms.models import (
    Farm,
    FarmPhoto,
    FarmProduct,
    FarmProductStockBatch,
    LegacyFarmWeeklySupply,
)
from farms.stock import active_stock_batch_q, stock_batch_status


class FarmConflict(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def farm_queryset():
    current_stock = active_stock_batch_q("products__stock_batches") & Q(
        is_active=True,
        products__is_active=True,
    )
    return (
        Farm.objects.select_related("image")
        .prefetch_related(
            Prefetch(
                "photos",
                queryset=FarmPhoto.objects.select_related("image").order_by("sort_order"),
            )
        )
        .annotate(
            _active_product_count=Count(
                "products",
                filter=Q(products__is_active=True),
                distinct=True,
            ),
            in_stock_product_count=Count(
                "products",
                filter=current_stock,
                distinct=True,
            ),
        )
        .annotate(
            out_of_stock_product_count=F("_active_product_count") - F("in_stock_product_count"),
        )
    )


def farm_product_queryset(*, today=None):
    current_date = today or timezone.localdate()
    active_batches = active_stock_batch_q(today=current_date) & Q(
        farm__is_active=True, is_active=True
    )
    return (
        FarmProduct.objects.select_related("farm", "product")
        .prefetch_related("product__translations")
        .annotate(
            available_quantity=Coalesce(
                Sum("stock_batches__quantity_units", filter=active_batches),
                Value(0),
                output_field=IntegerField(),
            ),
            batch_count=Count("stock_batches", distinct=True),
            active_batch_count=Count("stock_batches", filter=active_batches, distinct=True),
            next_expiry_date=Min("stock_batches__end_date", filter=active_batches),
            has_legacy_supply_history=Exists(
                LegacyFarmWeeklySupply.objects.filter(farm_product_id=OuterRef("pk"))
            ),
        )
    )


def _replace_photos(farm, ids):
    locked = list(MediaAsset.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    by_id = {asset.pk: asset for asset in locked}
    missing = [str(asset_id) for asset_id in ids if asset_id not in by_id]
    if missing:
        raise ValidationError({"photo_ids": f"Media assets do not exist: {', '.join(missing)}."})
    old_ids = set(farm.photos.values_list("image_id", flat=True))
    if farm.image_id:
        old_ids.add(farm.image_id)
    invalid = [
        str(asset_id)
        for asset_id in ids
        if asset_id not in old_ids
        and (
            not by_id[asset_id].file.name.startswith("farms/")
            or any(
                not str(variant.get("file") or "").startswith("farms/")
                for variant in by_id[asset_id].variants
            )
        )
    ]
    if invalid:
        raise ValidationError({"photo_ids": f"Choose Farm images: {', '.join(invalid)}."})
    current_ids = list(farm.photos.order_by("sort_order").values_list("image_id", flat=True))
    if current_ids == ids and farm.image_id == (ids[0] if ids else None):
        return
    farm.photos.all().delete()
    FarmPhoto.objects.bulk_create(
        [
            FarmPhoto(farm=farm, image=by_id[asset_id], sort_order=index)
            for index, asset_id in enumerate(ids)
        ]
    )
    farm.image = by_id[ids[0]] if ids else None
    farm.save(update_fields=["image", "updated_at"])
    for asset_id in old_ids - set(ids):
        delete_orphan_media_asset(asset_id=asset_id, storage_prefix="farms")


@transaction.atomic
def create_farm(*, actor, data):
    values = dict(data)
    photo_ids = values.pop("photo_ids", [])
    farm = Farm.objects.create(**values)
    if photo_ids:
        _replace_photos(farm, photo_ids)
    audit(actor=actor, action="farm.create", entity=farm)
    return farm


@transaction.atomic
def update_farm(*, farm_id, actor, expected_version, data):
    from content.models import TrackerFarm, TrackerFarmPhoto

    farm = Farm.objects.select_for_update().get(pk=farm_id)
    store_product_slugs = list(
        Product.objects.filter(farm_offers__farm_id=farm_id).values_list("slug", flat=True)
    )
    profile = TrackerFarm.objects.select_for_update().filter(farm_id=farm_id).first()
    if farm.version != expected_version:
        raise FarmConflict("version_conflict")
    values = dict(data)
    photo_ids = values.pop("photo_ids", None)
    for field, value in values.items():
        setattr(farm, field, value)
    farm.legacy_tracker_owned = False
    farm.version += 1
    farm.save()
    if photo_ids is not None:
        _replace_photos(farm, photo_ids)
    # The legacy tracker admin form still submits shared fields until its UI migration.
    TrackerFarm.objects.filter(farm_id=farm.pk).update(
        name_en=farm.name_en,
        name_vi=farm.name_vi,
        location_en=farm.location_en,
        location_vi=farm.location_vi,
        description_en=farm.description_en,
        description_vi=farm.description_vi,
        image_id=farm.image_id,
    )
    if profile:
        old_profile_media_ids = set(profile.photos.values_list("image_id", flat=True))
        if profile.image_id:
            old_profile_media_ids.add(profile.image_id)
        if photo_ids is not None:
            profile.photos.all().delete()
            TrackerFarmPhoto.objects.bulk_create(
                [
                    TrackerFarmPhoto(farm=profile, image_id=photo.image_id, sort_order=index)
                    for index, photo in enumerate(farm.photos.order_by("sort_order"))
                ]
            )
        profile.version += 1
        profile.image_id = farm.image_id
        profile.save(update_fields=["version", "image", "updated_at"])
        if photo_ids is not None:
            for asset_id in old_profile_media_ids - set(photo_ids):
                delete_orphan_media_asset(asset_id=asset_id, storage_prefix="farms")
        transaction.on_commit(lambda: request_frontend_revalidation(["tracker-farms"]))
    audit(actor=actor, action="farm.update", entity=farm)
    if store_product_slugs:
        schedule_store_revalidation(slugs=store_product_slugs)
    return farm


@transaction.atomic
def delete_farm(*, farm_id, actor):
    from content.models import TrackerFarm

    farm = Farm.objects.select_for_update().get(pk=farm_id)
    if (
        FarmProduct.objects.filter(farm=farm).exists()
        or TrackerFarm.objects.filter(farm=farm).exists()
    ):
        raise FarmConflict("farm_in_use")
    asset_ids = set(farm.photos.values_list("image_id", flat=True))
    if farm.image_id:
        asset_ids.add(farm.image_id)
    audit(actor=actor, action="farm.delete", entity=farm)
    farm.delete()
    for asset_id in asset_ids:
        delete_orphan_media_asset(asset_id=asset_id, storage_prefix="farms")


def _validate_stock_batch_values(*, start_date, end_date, quantity_units, today=None):
    current_date = today or timezone.localdate()
    errors = {}
    if end_date < start_date:
        errors["end_date"] = "End date must not precede start date."
    elif end_date < current_date:
        errors["end_date"] = "An expired stock batch cannot be saved."
    if quantity_units <= 0:
        errors["quantity_units"] = "Quantity must be positive."
    if errors:
        raise ValidationError(errors)


@transaction.atomic
def add_farm_product(*, farm_id, product_id, start_date, end_date, quantity_units, actor):
    farm = Farm.objects.select_for_update().get(pk=farm_id)
    product = Product.objects.select_for_update().filter(pk=product_id).first()
    if product is None:
        raise Product.DoesNotExist
    if not product.is_active:
        raise FarmConflict("product_inactive")
    _validate_stock_batch_values(
        start_date=start_date, end_date=end_date, quantity_units=quantity_units
    )
    try:
        row = FarmProduct.objects.create(farm=farm, product=product)
    except IntegrityError as error:
        raise FarmConflict("product_already_linked") from error
    batch = FarmProductStockBatch.objects.create(
        farm_product=row,
        start_date=start_date,
        end_date=end_date,
        quantity_units=quantity_units,
    )
    audit(
        actor=actor,
        action="farm.product.add",
        entity=row,
        context={"farm_id": str(farm.pk), "stock_batch_id": str(batch.pk)},
    )
    schedule_store_revalidation(slugs=[product.slug])
    return row


@transaction.atomic
def create_stock_batch(*, farm_id, product_id, actor, data):
    product = Product.objects.select_for_update().only("slug").filter(pk=product_id).first()
    if product is None:
        raise FarmConflict("farm_product_changed")
    relation = (
        FarmProduct.objects.select_for_update()
        .filter(farm_id=farm_id, product_id=product_id)
        .first()
    )
    if relation is None:
        raise FarmConflict("farm_product_changed")
    values = dict(data)
    _validate_stock_batch_values(**values)
    batch = FarmProductStockBatch.objects.create(farm_product=relation, **values)
    audit(
        actor=actor,
        action="farm.stock_batch.create",
        entity=batch,
        context={"farm_id": str(farm_id), "product_id": str(product_id)},
    )
    schedule_store_revalidation(slugs=[product.slug])
    return batch


@transaction.atomic
def update_stock_batch(*, farm_id, product_id, batch_id, actor, expected_version, data):
    relation = FarmProduct.objects.filter(farm_id=farm_id, product_id=product_id).first()
    if relation is None:
        raise FarmConflict("farm_product_changed")
    batch = (
        FarmProductStockBatch.objects.select_for_update()
        .select_related("farm_product__product")
        .filter(pk=batch_id, farm_product=relation)
        .first()
    )
    if batch is None:
        raise FarmConflict("stock_batch_changed")
    if batch.version != expected_version:
        raise FarmConflict("version_conflict")
    status = stock_batch_status(batch)
    if status == "cancelled":
        raise FarmConflict("stock_batch_cancelled")
    if status == "expired":
        raise FarmConflict("stock_batch_expired")
    values = dict(data)
    if status == "active" and "start_date" in values and values["start_date"] != batch.start_date:
        raise FarmConflict("stock_batch_start_locked")
    start_date = values.get("start_date", batch.start_date)
    end_date = values.get("end_date", batch.end_date)
    quantity_units = values.get("quantity_units", batch.quantity_units)
    _validate_stock_batch_values(
        start_date=start_date,
        end_date=end_date,
        quantity_units=quantity_units,
    )
    before = {
        "start_date": batch.start_date.isoformat(),
        "end_date": batch.end_date.isoformat(),
        "quantity_units": batch.quantity_units,
    }
    for field, value in values.items():
        setattr(batch, field, value)
    batch.version += 1
    batch.save()
    audit(
        actor=actor,
        action="farm.stock_batch.update",
        entity=batch,
        context={"before": before},
    )
    schedule_store_revalidation(slugs=[batch.farm_product.product.slug])
    return batch


@transaction.atomic
def cancel_stock_batch(*, farm_id, product_id, batch_id, actor, expected_version):
    relation = FarmProduct.objects.filter(farm_id=farm_id, product_id=product_id).first()
    if relation is None:
        raise FarmConflict("farm_product_changed")
    batch = (
        FarmProductStockBatch.objects.select_for_update()
        .select_related("farm_product__product")
        .filter(pk=batch_id, farm_product=relation)
        .first()
    )
    if batch is None:
        raise FarmConflict("stock_batch_changed")
    if batch.version != expected_version:
        raise FarmConflict("version_conflict")
    status = stock_batch_status(batch)
    if status == "cancelled":
        raise FarmConflict("stock_batch_cancelled")
    if status == "expired":
        raise FarmConflict("stock_batch_expired")
    batch.cancelled_at = timezone.now()
    batch.cancelled_by = actor
    batch.version += 1
    batch.save(update_fields=["cancelled_at", "cancelled_by", "version", "updated_at"])
    audit(actor=actor, action="farm.stock_batch.cancel", entity=batch)
    schedule_store_revalidation(slugs=[batch.farm_product.product.slug])
    return batch


@transaction.atomic
def update_farm_product(*, farm_id, product_id, is_active, actor):
    row = (
        FarmProduct.objects.select_for_update()
        .filter(farm_id=farm_id, product_id=product_id)
        .first()
    )
    if row is None:
        raise FarmConflict("farm_product_changed")
    if row.is_active != is_active:
        row.is_active = is_active
        row.save(update_fields=["is_active", "updated_at"])
        audit(
            actor=actor, action="farm.product.update", entity=row, context={"is_active": is_active}
        )
        schedule_store_revalidation(slugs=[row.product.slug])
    return row


@transaction.atomic
def delete_farm_product(*, farm_id, product_id, actor):
    relation = FarmProduct.objects.filter(farm_id=farm_id, product_id=product_id).first()
    if relation is None:
        raise FarmConflict("farm_product_changed")
    if not Product.objects.select_for_update().filter(pk=relation.product_id).exists():
        raise FarmConflict("farm_product_changed")
    row = FarmProduct.objects.select_for_update().filter(pk=relation.pk).first()
    if row is None:
        raise FarmConflict("farm_product_changed")
    if (
        FarmProductStockBatch.objects.filter(farm_product=row).exists()
        or LegacyFarmWeeklySupply.objects.filter(farm_product=row).exists()
    ):
        raise FarmConflict("farm_product_has_supply")
    audit(actor=actor, action="farm.product.delete", entity=row, context={"farm_id": str(farm_id)})
    slug = row.product.slug
    row.delete()
    schedule_store_revalidation(slugs=[slug])


def _schedule_tracker_revalidation():
    transaction.on_commit(lambda: request_frontend_revalidation(["tracker-farms"]))


@transaction.atomic
def create_growth_tracker_profile(*, farm_id, actor, data):
    from content.models import TrackerFarm, TrackerFarmPhoto

    farm = Farm.objects.select_for_update().get(pk=farm_id)
    if TrackerFarm.objects.filter(farm_id=farm.pk).exists():
        raise FarmConflict("growth_tracker_already_enabled")
    values = dict(data)
    max_order = TrackerFarm.objects.aggregate(max_order=Max("sort_order"))["max_order"]
    values.setdefault("sort_order", (max_order if max_order is not None else -1) + 1)
    next_version = farm.tracker_profile_revision + 1
    profile = TrackerFarm.objects.create(
        id=farm.pk,
        farm=farm,
        name_en=farm.name_en,
        name_vi=farm.name_vi,
        location_en=farm.location_en,
        location_vi=farm.location_vi,
        description_en=farm.description_en,
        description_vi=farm.description_vi,
        image_id=farm.image_id,
        version=next_version,
        **values,
    )
    farm.tracker_profile_revision = next_version
    farm.save(update_fields=["tracker_profile_revision"])
    TrackerFarmPhoto.objects.bulk_create(
        [
            TrackerFarmPhoto(farm=profile, image_id=photo.image_id, sort_order=photo.sort_order)
            for photo in farm.photos.order_by("sort_order")
        ]
    )
    audit(actor=actor, action="tracker_farm.enable", entity=profile)
    _schedule_tracker_revalidation()
    return profile


@transaction.atomic
def update_growth_tracker_profile(*, farm_id, actor, expected_version, data):
    from content.models import TrackerFarm

    Farm.objects.select_for_update().get(pk=farm_id)
    profile = TrackerFarm.objects.select_for_update().filter(farm_id=farm_id).first()
    if profile is None:
        raise FarmConflict("growth_tracker_changed")
    if profile.version != expected_version:
        raise FarmConflict("version_conflict")
    for field, value in data.items():
        setattr(profile, field, value)
    profile.version += 1
    profile.save()
    audit(actor=actor, action="tracker_farm.update", entity=profile)
    _schedule_tracker_revalidation()
    return profile


@transaction.atomic
def delete_growth_tracker_profile(*, farm_id, actor):
    from content.models import TrackerFarm

    farm = Farm.objects.select_for_update().get(pk=farm_id)
    profile = TrackerFarm.objects.select_for_update().filter(farm_id=farm_id).first()
    if profile is None:
        raise FarmConflict("growth_tracker_changed")
    farm.tracker_profile_revision = max(farm.tracker_profile_revision, profile.version)
    farm.save(update_fields=["tracker_profile_revision"])
    audit(actor=actor, action="tracker_farm.disable", entity=profile)
    profile.delete()
    _schedule_tracker_revalidation()
