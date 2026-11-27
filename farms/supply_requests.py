from uuid import uuid4

from django.db import transaction
from django.db.models import prefetch_related_objects
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from commerce.services import schedule_store_revalidation
from content.creation_receipts import creation_fingerprint
from content.services import audit
from farms.farmer_links import InvalidFarmerToken, farm_for_token
from farms.models import Farm, FarmSupplyRequest, FarmSupplyRequestItem
from farms.services import (
    FarmConflict,
    _lock_farm_stock_sources,
    _lock_stock_products,
    _validate_stock_batch_creation,
    _write_stock_batch_for_source,
)

Status = FarmSupplyRequest.Status


def supply_product_payload(product):
    names = {item.locale: item.name for item in product.translations.all()}
    return {
        "product_id": str(product.pk),
        "sku": product.sku,
        "name_vi": names.get("vi") or names.get("en") or product.sku,
        "name_en": names.get("en") or names.get("vi") or product.sku,
        "unit_size": str(product.unit_size),
        "unit_label_vi": product.unit_label_vi,
        "unit_label_en": product.unit_label_en,
    }


def _locked_items(*, farm, products, sources, items, start_date, end_date):
    today = timezone.localdate()
    validated = []
    for position, item in enumerate(items):
        product_id = item["product_id"]
        source = sources.get(product_id)
        context = {"item_index": position, "product_id": str(product_id)}
        try:
            if source is None or product_id not in products:
                raise FarmConflict("farm_product_changed")
            if source.pk != item["expected_link_id"]:
                raise FarmConflict("farm_product_identity_changed")
            if source.supply_generation != item["expected_generation"]:
                raise FarmConflict("farm_product_generation_changed")
            if source.version < item.get("submitted_version", source.version):
                raise FarmConflict("farm_product_version_conflict")
            if item["quantity_units"] > 2_147_483_647:
                raise ValidationError({"quantity_units": "Quantity must not exceed 2147483647."})
            source.farm = farm
            source.product = products[product_id]
            data = {
                "start_date": start_date,
                "end_date": end_date,
                "quantity_units": item["quantity_units"],
            }
            # The locked version can have advanced from an unrelated batch addition.
            _validate_stock_batch_creation(
                source=source,
                expected_link_id=item["expected_link_id"],
                expected_version=source.version,
                data=data,
                today=today,
            )
            validated.append((source, data))
        except FarmConflict as error:
            raise FarmConflict(error.code, context={**error.context, **context}) from error
        except ValidationError as error:
            if set(error.detail) <= {"start_date", "end_date"}:
                raise
            raise ValidationError({"items": {str(position): error.detail}}) from error
    return validated


@transaction.atomic
def submit_supply_request(*, farm_id, digest, idempotency_key, start_date, end_date, items):
    fingerprint = creation_fingerprint(
        {
            "start_date": start_date,
            "end_date": end_date,
            "items": sorted(items, key=lambda item: str(item["product_id"])),
        }
    )
    product_ids = [item["product_id"] for item in items]
    products = _lock_stock_products(product_ids)
    sources = _lock_farm_stock_sources(farm_id, product_ids)
    try:
        farm = Farm.objects.select_for_update().get(pk=farm_id)
    except Farm.DoesNotExist as error:
        raise InvalidFarmerToken from error
    farm_for_token(digest, locked_farm_id=farm.pk)
    previous = (
        FarmSupplyRequest.objects.select_for_update()
        .filter(farm=farm, submission_key=idempotency_key)
        .first()
    )
    if previous is not None:
        if previous.submission_fingerprint != fingerprint:
            raise FarmConflict("idempotency_conflict")
        return previous, False
    if not farm.is_active:
        raise FarmConflict("farm_inactive")
    validated = _locked_items(
        farm=farm,
        products=products,
        sources=sources,
        items=items,
        start_date=start_date,
        end_date=end_date,
    )
    request_id = uuid4()
    supply_request = FarmSupplyRequest.objects.create(
        pk=request_id,
        farm=farm,
        code=f"FSR-{request_id.hex.upper()}",
        start_date=start_date,
        end_date=end_date,
        submission_key=idempotency_key,
        submission_fingerprint=fingerprint,
    )
    prefetch_related_objects(list(products.values()), "translations")
    FarmSupplyRequestItem.objects.bulk_create(
        [
            FarmSupplyRequestItem(
                request=supply_request,
                source=source,
                position=position,
                product_snapshot=supply_product_payload(source.product),
                source_generation=source.supply_generation,
                source_version=source.version,
                quantity_units=data["quantity_units"],
            )
            for position, (source, data) in enumerate(validated)
        ]
    )
    audit(
        actor=None,
        action="farm.supply_request.submit",
        entity=supply_request,
        context={"farm_id": str(farm.pk)},
    )
    return supply_request, True


def _is_decision_replay(entry, *, outcome, idempotency_key, reason):
    if entry.status == Status.PENDING:
        return False
    if entry.decision_key != idempotency_key:
        raise FarmConflict("farm_supply_request_processed", context={"status": entry.status})
    if entry.status != outcome or entry.rejection_reason != reason:
        raise FarmConflict("idempotency_conflict")
    return True


@transaction.atomic
def decide_supply_request(*, entry, authorize_write, idempotency_key, outcome, reason=""):
    if _is_decision_replay(entry, outcome=outcome, idempotency_key=idempotency_key, reason=reason):
        return entry, True
    if outcome == Status.APPROVED:
        original = list(entry.items.select_related("source"))
        items = [
            {
                "product_id": item.source.product_id,
                "expected_link_id": item.source_id,
                "expected_generation": item.source_generation,
                "submitted_version": item.source_version,
                "quantity_units": item.quantity_units,
            }
            for item in original
        ]
        product_ids = [item["product_id"] for item in items]
        products = _lock_stock_products(product_ids)
        sources = _lock_farm_stock_sources(entry.farm_id, product_ids)

    farm = Farm.objects.select_for_update().get(pk=entry.farm_id)
    entry = FarmSupplyRequest.objects.select_for_update().get(pk=entry.pk)
    actor = authorize_write()
    if _is_decision_replay(entry, outcome=outcome, idempotency_key=idempotency_key, reason=reason):
        return entry, True
    if outcome == Status.APPROVED:
        if not farm.is_active:
            raise FarmConflict("farm_inactive")
        validated = _locked_items(
            farm=farm,
            products=products,
            sources=sources,
            items=items,
            start_date=entry.start_date,
            end_date=entry.end_date,
        )
        for item, (source, data) in zip(original, validated, strict=True):
            item.approved_batch = _write_stock_batch_for_source(
                source=source, actor=actor, data=data, revalidate=False
            )
        FarmSupplyRequestItem.objects.bulk_update(original, ["approved_batch"])
        schedule_store_revalidation(slugs=[product.slug for product in products.values()])

    entry.status = outcome
    entry.processed_at = timezone.now()
    entry.processed_by = actor
    entry.decision_key = idempotency_key
    entry.rejection_reason = reason
    entry.save(
        update_fields=["status", "processed_at", "processed_by", "decision_key", "rejection_reason"]
    )
    audit(
        actor=actor,
        action=f"farm.supply_request.{outcome}",
        entity=entry,
        context={"farm_id": str(farm.pk)},
    )
    return entry, False
