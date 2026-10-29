from dataclasses import dataclass

from django.db import transaction

from commerce.models import ShippingAddress, ShippingArea, ShippingProvinceRule
from members.administrative_units import province_choices, ward_choices


class UnsupportedShippingArea(Exception):
    pass


def _unit_names(province_code, ward_code):
    provinces_vi = dict(province_choices("vi"))
    provinces_en = dict(province_choices("en"))
    wards_vi = dict(ward_choices(province_code, "vi"))
    wards_en = dict(ward_choices(province_code, "en"))
    return {
        "province_name_vi": provinces_vi.get(province_code, ""),
        "province_name_en": provinces_en.get(province_code, ""),
        "ward_name_vi": wards_vi.get(ward_code, ""),
        "ward_name_en": wards_en.get(ward_code, ""),
    }


def shipping_area_payload(item):
    return {
        "id": str(item.pk),
        "province_code": item.province_code,
        "ward_code": item.ward_code,
        "shipping_fee_vnd": (
            str(item.shipping_fee_vnd) if item.shipping_fee_vnd is not None else None
        ),
        "is_active": item.is_active,
        **_unit_names(item.province_code, item.ward_code),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


@dataclass(frozen=True)
class EffectiveShippingArea:
    shipping_fee_vnd: int


def active_shipping_area(*, province_code, ward_code):
    override = ShippingArea.objects.filter(
        province_code=province_code,
        ward_code=ward_code,
    ).first()
    if override is not None:
        if not override.is_active:
            return None
        if override.shipping_fee_vnd is not None:
            return override
    rule = ShippingProvinceRule.objects.filter(
        province_code=province_code,
    ).first()
    if rule is not None and (rule.is_active or override is not None):
        return EffectiveShippingArea(rule.default_shipping_fee_vnd)
    return None


def shipping_settings_payload(*, province_code, rule=None, overrides=None):
    if rule is None:
        rule = ShippingProvinceRule.objects.filter(province_code=province_code).first()
    if overrides is None:
        overrides = ShippingArea.objects.filter(province_code=province_code)
    province_names_vi = dict(province_choices("vi"))
    province_names_en = dict(province_choices("en"))
    return {
        "province_code": province_code,
        "province_name_vi": province_names_vi.get(province_code, ""),
        "province_name_en": province_names_en.get(province_code, ""),
        "default_shipping_fee_vnd": str(rule.default_shipping_fee_vnd) if rule else "0",
        "is_active": rule.is_active if rule else False,
        "overrides": [shipping_area_payload(item) for item in overrides],
    }


@transaction.atomic
def save_shipping_settings(*, province_code, data):
    rule, _created = ShippingProvinceRule.objects.update_or_create(
        province_code=province_code,
        defaults={
            "default_shipping_fee_vnd": data["default_shipping_fee_vnd"],
            "is_active": data["is_active"],
        },
    )
    requested_codes = {item["ward_code"] for item in data["overrides"]}
    ShippingArea.objects.filter(province_code=province_code).exclude(
        ward_code__in=requested_codes
    ).delete()
    for override in data["overrides"]:
        ShippingArea.objects.update_or_create(
            ward_code=override["ward_code"],
            defaults={
                "province_code": province_code,
                "shipping_fee_vnd": override["shipping_fee_vnd"],
                "is_active": override["is_active"],
            },
        )
    overrides = ShippingArea.objects.filter(province_code=province_code).order_by("ward_code", "id")
    return shipping_settings_payload(
        province_code=province_code,
        rule=rule,
        overrides=overrides,
    )


def public_shipping_area_payloads(locale):
    language = "en" if locale == "en" else "vi"
    province_names = dict(province_choices(language))
    rules = {item.province_code: item for item in ShippingProvinceRule.objects.all()}
    overrides_by_province = {}
    for item in ShippingArea.objects.all().order_by("province_code", "ward_code", "id"):
        overrides_by_province.setdefault(item.province_code, {})[item.ward_code] = item

    result = []
    province_codes = sorted(set(rules) | set(overrides_by_province))
    for province_code in province_codes:
        rule = rules.get(province_code)
        overrides = overrides_by_province.get(province_code, {})
        ward_names = dict(ward_choices(province_code, language))
        wards = []
        for ward_code, ward_name in sorted(ward_names.items()):
            override = overrides.get(ward_code)
            if override is not None and not override.is_active:
                continue
            if override is not None and override.shipping_fee_vnd is not None:
                fee = override.shipping_fee_vnd
            elif rule is not None and (rule.is_active or override is not None):
                fee = rule.default_shipping_fee_vnd
            else:
                continue
            wards.append(
                {
                    "ward_code": ward_code,
                    "ward_name": ward_name,
                    "shipping_fee_vnd": str(fee),
                }
            )
        if wards:
            result.append(
                {
                    "province_code": province_code,
                    "province_name": province_names.get(province_code, ""),
                    "wards": wards,
                }
            )
    return result


def shipping_address_payload(item):
    area = active_shipping_area(province_code=item.province_code, ward_code=item.ward_code)
    return {
        "id": str(item.pk),
        "recipient_name": item.recipient_name,
        "phone_number": item.phone_number,
        "province_code": item.province_code,
        "ward_code": item.ward_code,
        "address": item.address,
        "is_supported": area is not None,
        "shipping_fee_vnd": str(area.shipping_fee_vnd) if area else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
    }


@transaction.atomic
def save_shipping_address(*, user, data):
    if (
        active_shipping_area(province_code=data["province_code"], ward_code=data["ward_code"])
        is None
    ):
        raise UnsupportedShippingArea
    item, _created = ShippingAddress.objects.update_or_create(user=user, defaults=data)
    return item
