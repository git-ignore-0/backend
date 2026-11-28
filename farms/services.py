from django.db import IntegrityError, transaction
from django.db.models import (
    BigIntegerField,
    Count,
    Exists,
    F,
    IntegerField,
    Max,
    OuterRef,
    Prefetch,
    Subquery,
    Sum,
    Value,
)
from django.db.models.deletion import ProtectedError, RestrictedError
from django.db.models.functions import Coalesce, Greatest
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from commerce.models import (
    Product,
    ProductStatus,
    ProductTranslation,
    StoreStockAllocation,
    StoreStockMovement,
)
from commerce.services import schedule_store_revalidation
from content.creation_receipts import (
    CreationIdempotencyConflict,
    claim_creation_receipt,
    creation_fingerprint,
    find_creation_receipt,
)
from content.models import AdminCreationReceipt, ContentAuditLog, MediaAsset
from content.services import audit, delete_orphan_media_asset, request_frontend_revalidation
from farms.models import (
    Farm,
    FarmPhoto,
    FarmProduct,
    FarmProductStockBatch,
    FarmSupplyRequest,
    LegacyFarmWeeklySupply,
)
from farms.stock import (
    batch_net_sold,
    batch_remaining_units,
    current_stock_batch_q,
    stock_batch_status,
)


class FarmConflict(ValueError):
    def __init__(self, code, *, context=None):
        self.code = code
        self.context = context or {}
        super().__init__(code)


def _batch_total(rows, expression):
    totals = rows.order_by().values("farm_product_id").annotate(total=expression).values("total")
    return Coalesce(Subquery(totals, output_field=BigIntegerField()), Value(0))


def _current_source_batches(today):
    return FarmProductStockBatch.objects.filter(
        current_stock_batch_q(today=today), farm_product_id=OuterRef("pk")
    )


def farm_queryset():
    today = timezone.localdate()
    supplying = FarmProduct.objects.filter(farm_id=OuterRef("pk"), removed_at__isnull=True)
    batches_with_stock = (
        _current_source_batches(today)
        .alias(remaining=batch_remaining_units(batch_net_sold()))
        .filter(remaining__gt=0)
    )
    active_count = (
        supplying.order_by().values("farm_id").annotate(total=Count("pk")).values("total")
    )
    in_stock_count = (
        supplying.filter(Exists(batches_with_stock))
        .order_by()
        .values("farm_id")
        .annotate(total=Count("pk"))
        .values("total")
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
            _active_product_count=Coalesce(
                Subquery(active_count, output_field=IntegerField()), Value(0)
            ),
            in_stock_product_count=Coalesce(
                Subquery(in_stock_count, output_field=IntegerField()), Value(0)
            ),
        )
        .annotate(
            out_of_stock_product_count=F("_active_product_count") - F("in_stock_product_count"),
        )
    )


def farm_product_queryset(*, today=None):
    today = today or timezone.localdate()
    all_batches = FarmProductStockBatch.objects.filter(farm_product_id=OuterRef("pk"))
    current_batches = _current_source_batches(today)
    net_sold = batch_net_sold()
    return (
        FarmProduct.objects.select_related("farm", "product")
        .prefetch_related("product__translations")
        .annotate(
            initial_quantity=_batch_total(current_batches, Sum("quantity_units")),
            net_sold_quantity=Greatest(
                Value(0),
                _batch_total(current_batches, Sum(net_sold)),
                output_field=BigIntegerField(),
            ),
            available_quantity=_batch_total(current_batches, Sum(batch_remaining_units(net_sold))),
            batch_count=_batch_total(all_batches, Count("pk")),
            active_batch_count=_batch_total(current_batches, Count("pk")),
            next_expiry_date=Subquery(current_batches.order_by("end_date").values("end_date")[:1]),
            has_legacy_supply_history=Exists(
                LegacyFarmWeeklySupply.objects.filter(farm_product_id=OuterRef("pk"))
            ),
        )
    )


def stock_batch_queryset():
    net_sold = batch_net_sold()
    return FarmProductStockBatch.objects.select_related("farm_product__farm").annotate(
        net_sold_quantity=Greatest(Value(0), net_sold, output_field=BigIntegerField()),
        remaining_quantity=batch_remaining_units(net_sold),
    )


def _farm_media_for_update(ids):
    return MediaAsset.objects.select_for_update().filter(pk__in=ids).order_by("pk")


def _replace_photos(farm, ids, *, tracker_media_ids=()):
    old_ids = set(farm.photos.values_list("image_id", flat=True))
    if farm.image_id:
        old_ids.add(farm.image_id)
    # Match deletion's media lock order before changing shared references.
    media_ids = old_ids.union(ids, tracker_media_ids)
    by_id = {asset.pk: asset for asset in _farm_media_for_update(media_ids)}
    missing = [str(asset_id) for asset_id in ids if asset_id not in by_id]
    if missing:
        raise ValidationError({"photo_ids": f"Media assets do not exist: {', '.join(missing)}."})
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

    # Match Store's Product -> FarmProduct -> Farm -> batch lock order.
    # Lock all products of this farm before taking the farm lock: reservation
    # checks also read other farms supplying the same products.
    if "is_active" in data:
        product_ids = list(
            FarmProduct.objects.filter(farm_id=farm_id).values_list("product_id", flat=True)
        )
        list(Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk"))
        list(
            FarmProduct.objects.select_for_update()
            .filter(product_id__in=product_ids)
            .order_by("pk")
        )
    farm = Farm.objects.select_for_update().get(pk=farm_id)
    if "is_active" in data:
        current_product_ids = set(
            FarmProduct.objects.filter(farm_id=farm_id).values_list("product_id", flat=True)
        )
        if current_product_ids != set(product_ids):
            # A newly attached product was not part of the ordered locks above.
            # Retry from a fresh snapshot instead of locking it after Farm.
            raise FarmConflict("farm_product_changed")
    store_product_slugs = list(
        Product.objects.filter(farm_offers__farm_id=farm_id).values_list("slug", flat=True)
    )
    profile = TrackerFarm.objects.select_for_update().filter(farm_id=farm_id).first()
    if farm.version != expected_version:
        raise FarmConflict("version_conflict")
    values = dict(data)
    if farm.is_active and values.get("is_active") is False:
        from commerce.store_orders import (
            StoreOrderError,
            ensure_farm_deactivation_preserves_reservations,
        )

        try:
            ensure_farm_deactivation_preserves_reservations(farm)
        except StoreOrderError as error:
            raise FarmConflict(error.code) from error
    photo_ids = values.pop("photo_ids", None)
    for field, value in values.items():
        setattr(farm, field, value)
    farm.legacy_tracker_owned = False
    farm.version += 1
    farm.save()
    old_profile_media_ids = set()
    if photo_ids is not None:
        if profile:
            old_profile_media_ids.update(profile.photos.values_list("image_id", flat=True))
            if profile.image_id:
                old_profile_media_ids.add(profile.image_id)
        _replace_photos(farm, photo_ids, tracker_media_ids=old_profile_media_ids)
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


def _farm_delete_product_reference(rows, code):
    names = ProductTranslation.objects.filter(product_id=OuterRef("product_id")).order_by()
    row = (
        rows.order_by("pk")
        .values(
            "product_id",
            "product__sku",
            name_vi=Subquery(names.filter(locale="vi").values("name")[:1]),
            name_en=Subquery(names.filter(locale="en").values("name")[:1]),
        )
        .first()
    )
    if not row:
        return None
    sku = row["product__sku"]
    return {
        "code": code,
        "id": str(row["product_id"]),
        "label": sku,
        "name_vi": row["name_vi"] or row["name_en"] or sku,
        "name_en": row["name_en"] or row["name_vi"] or sku,
    }


def _farm_delete_reservation_reference(farm_id, *, now):
    from commerce.inventory import effective_store_stock_reservations
    from memberships.csa_rounds import effective_stock_hold_rows

    order = (
        effective_store_stock_reservations(now=now)
        .filter(farm_id=farm_id)
        .order_by("pk")
        .values_list("line__order_id", "line__order__reference_code")
        .first()
    )
    if order:
        return {"code": "store_order", "id": str(order[0]), "label": order[1]}
    return _farm_delete_product_reference(
        effective_stock_hold_rows(now=now).filter(farm_id=farm_id), "supplying_product"
    )


def farm_capabilities(farm_id):
    sources = FarmProduct.objects.filter(farm_id=farm_id)
    supplying = _farm_delete_product_reference(
        sources.filter(removed_at__isnull=True), "supplying_product"
    )
    archived = _farm_delete_product_reference(
        sources.filter(
            Exists(LegacyFarmWeeklySupply.objects.filter(farm_product_id=OuterRef("pk")))
        ),
        "legacy_supply",
    )
    order = None
    for model in (StoreStockAllocation, StoreStockMovement):
        row = (
            model.objects.filter(batch__farm_product__farm_id=farm_id)
            .order_by("pk")
            .values("line__order_id", "line__order__reference_code")
            .first()
        )
        if row:
            order = {
                "code": "store_order",
                "id": str(row["line__order_id"]),
                "label": row["line__order__reference_code"],
            }
            break
    reasons = [reason for reason in (supplying, order, archived) if reason]
    if not reasons:
        request = FarmSupplyRequest.objects.filter(farm_id=farm_id).values("id", "code").first()
        if request:
            reasons.append(
                {"code": "farm_supply_request", "id": str(request["id"]), "label": request["code"]}
            )
    if not reasons and (
        reservation := _farm_delete_reservation_reference(farm_id, now=timezone.now())
    ):
        reasons.append(reservation)
    return {"delete": {"allowed": not reasons, "reasons": reasons}}


@transaction.atomic
def delete_farm(*, farm_id, actor):
    from content.models import TrackerFarm

    # Match Store and supply mutations. Add can attach a different Product
    # while we acquire locks, so recheck before touching any dependent rows.
    sources = FarmProduct.objects.filter(farm_id=farm_id)
    product_ids = list(sources.order_by().values_list("product_id", flat=True))
    products = list(
        Product.objects.select_for_update().filter(pk__in=product_ids).only("slug").order_by("pk")
    )
    locked_sources = list(
        sources.select_for_update().filter(product_id__in=product_ids).order_by("pk")
    )
    farm = Farm.objects.select_for_update().get(pk=farm_id)
    if sources.exclude(product_id__in=product_ids).exists():
        raise FarmConflict("farm_product_changed")

    batches = FarmProductStockBatch.objects.filter(
        farm_product_id__in=[source.pk for source in locked_sources]
    )
    list(batches.select_for_update().only("pk").order_by("pk"))
    decision = farm_capabilities(farm_id)["delete"]
    if not decision["allowed"]:
        raise FarmConflict("farm_in_use", context={"reasons": decision["reasons"]})
    tracker = TrackerFarm.objects.select_for_update().filter(farm_id=farm_id).first()
    asset_ids = set()
    for row in (farm, tracker):
        if row:
            asset_ids.update(row.photos.order_by().values_list("image_id", flat=True))
            if row.image_id:
                asset_ids.add(row.image_id)
    try:
        if tracker:
            audit(actor=actor, action="tracker_farm.delete", entity=tracker)
            tracker.delete()
        batches.delete()
        sources.delete()
        audit(actor=actor, action="farm.delete", entity=farm)
        farm.delete()
        for asset_id in sorted(asset_ids):
            delete_orphan_media_asset(asset_id=asset_id, storage_prefix="farms")
    except (ProtectedError, RestrictedError) as error:
        raise FarmConflict("farm_delete_protected") from error
    if tracker:
        _schedule_tracker_revalidation()
    if products:
        schedule_store_revalidation(slugs=[product.slug for product in products])


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


def _batch_created(*, source, actor, data, revalidate=True):
    batch = FarmProductStockBatch.objects.create(
        farm_product=source, supply_generation=source.supply_generation, **data
    )
    audit(
        actor=actor,
        action="farm.stock_batch.create",
        entity=batch,
        context={"farm_id": str(source.farm_id), "product_id": str(source.product_id)},
    )
    if revalidate:
        schedule_store_revalidation(slugs=[source.product.slug])
    return batch


def _locked_supply(farm_id, product_id):
    product = Product.objects.select_for_update().filter(pk=product_id).first()
    if product is None:
        raise FarmConflict("farm_product_changed")
    return _locked_supply_source(farm_id, product)


def _locked_supply_source(farm_id, product):
    source = (
        FarmProduct.objects.select_for_update()
        .filter(farm_id=farm_id, product_id=product.pk)
        .order_by("pk")
        .first()
    )
    if source is None:
        raise FarmConflict("farm_product_changed")
    source.farm = Farm.objects.select_for_update().get(pk=farm_id)
    source.product = product
    return source


def _require_source_revision(source, expected_link_id, expected_version):
    if source.pk != expected_link_id:
        raise FarmConflict("farm_product_identity_changed")
    if source.version != expected_version:
        raise FarmConflict("farm_product_version_conflict")


def farm_product_removal_blocker(source, *, now=None):
    from commerce.inventory import inventory_capacity, inventory_wire_quantity
    from commerce.models import StoreOrderStatus

    decision_at = now or timezone.now()
    orders = list(
        StoreStockAllocation.objects.filter(batch__farm_product=source)
        .exclude(line__order__status__in=[StoreOrderStatus.DELIVERED, StoreOrderStatus.CANCELLED])
        .order_by("line__order__reference_code", "line__order_id")
        .values_list("line__order_id", "line__order__reference_code")
        .distinct()[:4]
    )
    if orders:
        return FarmConflict(
            "farm_product_has_open_allocations",
            context={
                "orders": [
                    {"id": str(order_id), "reference_code": code} for order_id, code in orders[:3]
                ],
                "has_more": len(orders) > 3,
            },
        )
    product_id = source.product_id
    capacity = inventory_capacity([product_id], now=decision_at, exclude_source_id=source.pk)
    assigned = capacity.assigned.get((product_id, source.farm_id), 0)
    required, remaining = (
        (assigned, 0) if assigned else capacity.recovery_shortfalls.get(product_id, (0, 0))
    )
    if not required:
        return None
    return FarmConflict(
        "farm_product_supply_shortfall",
        context={
            "required_units": inventory_wire_quantity(required),
            "remaining_units": inventory_wire_quantity(remaining),
            "shortfall_units": inventory_wire_quantity(required - remaining),
        },
    )


@transaction.atomic
def remove_farm_product(*, farm_id, product_id, expected_link_id, expected_version, actor):
    source = _locked_supply(farm_id, product_id)
    _require_source_revision(source, expected_link_id, expected_version)
    if source.removed_at is not None:
        raise FarmConflict("farm_product_removed")
    decision_at = timezone.now()
    if blocker := farm_product_removal_blocker(source, now=decision_at):
        raise blocker
    source.removed_at = decision_at
    source.removed_by = actor
    source.is_active = False
    source.version += 1
    source.save(update_fields=["removed_at", "removed_by", "is_active", "version", "updated_at"])
    audit(
        actor=actor,
        action="farm.product.remove",
        entity=source,
        context={
            "farm_id": str(source.farm_id),
            "product_id": str(source.product_id),
            "supply_generation": source.supply_generation,
        },
    )
    schedule_store_revalidation(slugs=[source.product.slug])
    return source


@transaction.atomic
def readd_farm_product(
    *,
    farm_id,
    product_id,
    expected_link_id,
    expected_version,
    actor,
    idempotency_key,
    authorize_write=None,
):
    action = AdminCreationReceipt.Action.FARM_PRODUCT_READD
    scope = {"farm_id": str(farm_id), "product_id": str(product_id)}
    fingerprint = creation_fingerprint(
        {**scope, "expected_link_id": expected_link_id, "expected_version": expected_version}
    )
    if receipt := _matching_farm_receipt(actor, action, idempotency_key, fingerprint):
        return receipt, False

    product = Product.objects.select_for_update().filter(pk=product_id).first()
    receipt, created = claim_creation_receipt(
        actor=actor, action=action, key=idempotency_key, fingerprint=fingerprint
    )
    if not created:
        if authorize_write is not None:
            authorize_write()
        return receipt, False
    if product is None:
        raise FarmProduct.DoesNotExist
    try:
        source = _locked_supply_source(farm_id, product)
    except FarmConflict as error:
        # Preserve the Admin re-add route's not-found behavior for missing sources.
        raise FarmProduct.DoesNotExist from error
    if authorize_write is not None:
        authorize_write()
    _require_source_revision(source, expected_link_id, expected_version)
    if source.removed_at is None:
        raise FarmConflict("farm_product_not_removed")
    if source.product.status != ProductStatus.ACTIVE:
        raise FarmConflict("product_not_selling")
    source.supply_generation += 1
    source.version += 1
    source.removed_at = None
    source.removed_by = None
    source.is_active = True
    source.save(
        update_fields=[
            "supply_generation",
            "version",
            "removed_at",
            "removed_by",
            "is_active",
            "updated_at",
        ]
    )
    audit(
        actor=actor,
        action="farm.product.readd",
        entity=source,
        context={**scope, "supply_generation": source.supply_generation},
    )
    receipt.resource_id = source.pk
    receipt.resource_scope = scope
    receipt.save(update_fields=["resource_id", "resource_scope"])
    schedule_store_revalidation(slugs=[product.slug])
    return receipt, True


def _validate_stock_batch_creation(*, source, expected_link_id, expected_version, data, today=None):
    """Check the current source after Product, FarmProduct and Farm are locked."""
    _require_source_revision(source, expected_link_id, expected_version)
    if source.removed_at is not None:
        raise FarmConflict("farm_product_removed")
    if source.product.status != ProductStatus.ACTIVE:
        raise FarmConflict("product_not_selling")
    _validate_stock_batch_values(**data, today=today)


def _write_stock_batch_for_source(*, source, actor, data, revalidate=True):
    source.version += 1
    source.save(update_fields=["version", "updated_at"])
    return _batch_created(
        source=source,
        actor=actor,
        data=data,
        revalidate=revalidate,
    )


@transaction.atomic
def create_stock_batch(*, farm_id, product_id, expected_link_id, expected_version, actor, data):
    source = _locked_supply(farm_id, product_id)
    _validate_stock_batch_creation(
        source=source,
        expected_link_id=expected_link_id,
        expected_version=expected_version,
        data=data,
    )
    return _write_stock_batch_for_source(source=source, actor=actor, data=data)


def _lock_stock_products(product_ids):
    return {
        product.pk: product
        for product in Product.objects.select_for_update().filter(pk__in=product_ids).order_by("pk")
    }


def _lock_farm_stock_sources(farm_id, product_ids):
    return {
        source.product_id: source
        for source in FarmProduct.objects.select_for_update()
        .filter(farm_id=farm_id, product_id__in=product_ids)
        .order_by("pk")
    }


def _matching_farm_receipt(actor, action, key, fingerprint):
    receipt = find_creation_receipt(actor=actor, action=action, key=key)
    if receipt is not None and receipt.request_fingerprint != fingerprint:
        raise CreationIdempotencyConflict
    return receipt


def _reject_existing_link(source, *, index):
    raise FarmConflict(
        "farm_product_removed" if source.removed_at else "product_already_linked",
        context={
            "item_index": index,
            "product_id": str(source.product_id),
            "source_id": str(source.pk),
            "version": source.version,
        },
    )


@transaction.atomic
def link_farm_products(*, farm_id, actor, idempotency_key, product_ids, authorize_write=None):
    action = AdminCreationReceipt.Action.FARM_PRODUCT_LINKS
    fingerprint = creation_fingerprint(
        {"farm_id": farm_id, "product_ids": sorted(product_ids, key=str)}
    )
    if receipt := _matching_farm_receipt(actor, action, idempotency_key, fingerprint):
        return receipt, False

    # Match bulk stock writes: Product -> receipt (shared actor FK) -> source -> Farm.
    products = _lock_stock_products(product_ids)
    receipt, created = claim_creation_receipt(
        actor=actor, action=action, key=idempotency_key, fingerprint=fingerprint
    )
    if not created:
        if authorize_write is not None:
            authorize_write()
        return receipt, False
    existing = _lock_farm_stock_sources(farm_id, product_ids)
    farm = Farm.objects.select_for_update().get(pk=farm_id)
    if authorize_write is not None:
        authorize_write()

    sources = []
    for index, product_id in enumerate(product_ids):
        product = products.get(product_id)
        context = {"item_index": index, "product_id": str(product_id)}
        if product is None:
            raise FarmConflict("not_found", context=context)
        if product.status != ProductStatus.ACTIVE:
            raise FarmConflict("product_inactive", context=context)
        if source := existing.get(product_id):
            _reject_existing_link(source, index=index)
        sources.append(FarmProduct(farm=farm, product=product))

    try:
        with transaction.atomic():
            FarmProduct.objects.bulk_create(sources)
    except IntegrityError:
        # A writer bypassing the Product locks still cannot create a duplicate pair.
        conflicts = {
            source.product_id: source
            for source in FarmProduct.objects.filter(farm=farm, product_id__in=product_ids)
        }
        for index, product_id in enumerate(product_ids):
            if source := conflicts.get(product_id):
                _reject_existing_link(source, index=index)
        raise

    ContentAuditLog.objects.bulk_create(
        [
            ContentAuditLog(
                actor=actor,
                action="farm.product.add",
                entity_type=FarmProduct._meta.model_name,
                entity_id=str(source.pk),
                context={
                    "farm_id": str(farm_id),
                    "product_id": str(source.product_id),
                    "supply_generation": source.supply_generation,
                },
            )
            for source in sources
        ]
    )
    receipt.resource_id = farm.pk
    receipt.created_count = len(sources)
    receipt.save(update_fields=["resource_id", "created_count"])
    schedule_store_revalidation(slugs=[product.slug for product in products.values()])
    return receipt, True


@transaction.atomic
def create_stock_batches_bulk(*, farm_id, actor, idempotency_key, start_date, end_date, items):
    """Commit one intent, or replay its immutable receipt before state/date guards.

    Inputs are typed, schema-validated commands (like single-create). Product
    locks precede receipt insertion's shared actor FK lock, then source/Farm.
    No joined locking query or input-order lock acquisition is used.
    """
    action = AdminCreationReceipt.Action.FARM_STOCK_BATCHES
    fingerprint = creation_fingerprint(
        {
            "farm_id": farm_id,
            "start_date": start_date,
            "end_date": end_date,
            "items": sorted(items, key=lambda item: str(item["product_id"])),
        }
    )
    if receipt := _matching_farm_receipt(actor, action, idempotency_key, fingerprint):
        return receipt, False

    product_ids = [item["product_id"] for item in items]
    products = _lock_stock_products(product_ids)
    # Recheck under the unique key after waiting for Products or another receipt.
    # Nothing acquires Products after claiming this bulk action's receipt.
    receipt, created = claim_creation_receipt(
        actor=actor, action=action, key=idempotency_key, fingerprint=fingerprint
    )
    if not created:
        return receipt, False
    sources = _lock_farm_stock_sources(farm_id, product_ids)
    farm = Farm.objects.select_for_update().get(pk=farm_id)
    today = timezone.localdate()
    validated = []
    for index, item in enumerate(items):
        product_id = item["product_id"]
        source = sources.get(product_id)
        context = {"item_index": index, "product_id": str(product_id)}
        try:
            if source is None or product_id not in products:
                raise FarmConflict("farm_product_changed")
            source.farm = farm
            source.product = products[product_id]
            data = {
                "start_date": start_date,
                "end_date": end_date,
                "quantity_units": item["quantity_units"],
            }
            _validate_stock_batch_creation(
                source=source,
                expected_link_id=item["expected_link_id"],
                expected_version=item["expected_version"],
                data=data,
                today=today,
            )
            validated.append((source, data))
        except FarmConflict as error:
            raise FarmConflict(error.code, context={**error.context, **context}) from error
    for source, data in validated:
        _write_stock_batch_for_source(source=source, actor=actor, data=data, revalidate=False)
    receipt.resource_id = farm.pk
    receipt.created_count = len(items)
    receipt.save(update_fields=["resource_id", "created_count"])
    schedule_store_revalidation(slugs=[product.slug for product in products.values()])
    return receipt, True


def _locked_current_batch(source, batch_id, expected_version):
    if source.removed_at is not None:
        raise FarmConflict("farm_product_removed")
    batch = (
        FarmProductStockBatch.objects.select_for_update()
        .filter(pk=batch_id, farm_product=source)
        .first()
    )
    if batch is None:
        raise FarmConflict("stock_batch_changed")
    if batch.supply_generation != source.supply_generation:
        raise FarmConflict("farm_product_generation_changed")
    if batch.version != expected_version:
        raise FarmConflict("version_conflict")
    batch.farm_product = source
    return batch


@transaction.atomic
def update_stock_batch(*, farm_id, product_id, batch_id, actor, expected_version, data):
    source = _locked_supply(farm_id, product_id)
    batch = _locked_current_batch(source, batch_id, expected_version)
    decision_at = timezone.now()
    today = timezone.localdate(decision_at)
    status = stock_batch_status(batch, today=today)
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
    if source.product.status != ProductStatus.ACTIVE and (
        quantity_units > batch.quantity_units
        or start_date < batch.start_date
        or end_date > batch.end_date
    ):
        raise FarmConflict("product_not_selling")
    _validate_stock_batch_values(
        start_date=start_date,
        end_date=end_date,
        quantity_units=quantity_units,
        today=today,
    )
    from commerce.store_orders import StoreOrderError, ensure_batch_change_preserves_reservations

    try:
        ensure_batch_change_preserves_reservations(
            batch,
            quantity_units=quantity_units,
            start_date=start_date,
            end_date=end_date,
            now=decision_at,
        )
    except StoreOrderError as error:
        raise FarmConflict(error.code) from error
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
    source = _locked_supply(farm_id, product_id)
    batch = _locked_current_batch(source, batch_id, expected_version)
    decision_at = timezone.now()
    status = stock_batch_status(batch, today=timezone.localdate(decision_at))
    if status == "cancelled":
        raise FarmConflict("stock_batch_cancelled")
    if status == "expired":
        raise FarmConflict("stock_batch_expired")
    from commerce.store_orders import StoreOrderError, ensure_batch_change_preserves_reservations

    try:
        ensure_batch_change_preserves_reservations(batch, cancelling=True, now=decision_at)
    except StoreOrderError as error:
        raise FarmConflict(error.code) from error
    batch.cancelled_at = decision_at
    batch.cancelled_by = actor
    batch.version += 1
    batch.save(update_fields=["cancelled_at", "cancelled_by", "version", "updated_at"])
    audit(actor=actor, action="farm.stock_batch.cancel", entity=batch)
    schedule_store_revalidation(slugs=[batch.farm_product.product.slug])
    return batch


def _schedule_tracker_revalidation():
    transaction.on_commit(lambda: request_frontend_revalidation(["tracker-farms"]))


@transaction.atomic
def create_growth_tracker_profile(*, farm_id, actor, data):
    from content.models import TrackerFarm, TrackerFarmPhoto

    farm = Farm.objects.select_for_update().get(pk=farm_id)
    if TrackerFarm.objects.filter(farm_id=farm.pk).exists():
        raise FarmConflict("growth_tracker_already_enabled")
    photos = list(farm.photos.order_by("sort_order"))
    media_ids = {photo.image_id for photo in photos}
    if farm.image_id:
        media_ids.add(farm.image_id)
    # Take the same ordered locks as deletion before the cover FK is written.
    list(_farm_media_for_update(media_ids).only("pk"))
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
            for photo in photos
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
def delete_growth_tracker_profile(*, farm_id, actor, expected_version):
    from content.models import TrackerFarm

    farm = Farm.objects.select_for_update().get(pk=farm_id)
    profile = TrackerFarm.objects.select_for_update().filter(farm_id=farm_id).first()
    if profile is None:
        raise FarmConflict("growth_tracker_changed")
    if profile.version != expected_version:
        raise FarmConflict("version_conflict")
    farm.tracker_profile_revision = max(farm.tracker_profile_revision, profile.version)
    farm.save(update_fields=["tracker_profile_revision"])
    audit(actor=actor, action="tracker_farm.disable", entity=profile)
    profile.delete()
    _schedule_tracker_revalidation()
