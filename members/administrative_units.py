import json
from functools import cache
from pathlib import Path

DATA_PATH = Path(__file__).with_name("data") / "vietnamese_administrative_units.json"


@cache
def _provinces_by_code():
    with DATA_PATH.open(encoding="utf-8") as data_file:
        provinces = json.load(data_file)
    return {province["Code"]: province for province in provinces}


def _localized_name(unit, language_code):
    return unit["FullNameEn"] if language_code.startswith("en") else unit["FullName"]


def province_choices(language_code):
    return [
        (province["Code"], _localized_name(province, language_code))
        for province in _provinces_by_code().values()
    ]


def ward_choices(province_code, language_code):
    province = _provinces_by_code().get(province_code)
    if province is None:
        return []
    return [(ward["Code"], _localized_name(ward, language_code)) for ward in province["Wards"]]


def province_exists(province_code):
    return province_code in _provinces_by_code()
