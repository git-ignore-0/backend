import re

import requests
from django.core.cache import cache

VIETQR_BANKS_URL = "https://api.vietqr.io/v2/banks"
BANKS_CACHE_TTL = 60 * 60 * 24
NEGATIVE_CACHE_TTL = 60
FRESH_BANKS_CACHE_KEY = "memberships:vietqr-banks:fresh:v1"
STALE_BANKS_CACHE_KEY = "memberships:vietqr-banks:stale:v1"
UNAVAILABLE_BANKS_CACHE_KEY = "memberships:vietqr-banks:unavailable:v1"


class BankDirectoryUnavailable(RuntimeError):
    pass


def _normalized_banks(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BankDirectoryUnavailable("VietQR returned an invalid bank directory")
    banks = {}
    for item in payload["data"]:
        if not isinstance(item, dict) or item.get("transferSupported") != 1:
            continue
        bank_bin = str(item.get("bin", "")).strip()
        name = str(item.get("name", "")).strip()
        short_name = str(item.get("shortName", "")).strip()
        code = str(item.get("code", "")).strip().upper()
        if not re.fullmatch(r"\d{6}", bank_bin) or not name or not short_name or not code:
            continue
        banks[bank_bin] = {
            "bin": bank_bin,
            "name": name,
            "short_name": short_name,
            "code": code,
        }
    if not banks:
        raise BankDirectoryUnavailable("VietQR returned no supported banks")
    return sorted(banks.values(), key=lambda item: (item["name"].casefold(), item["bin"]))


def get_transfer_banks():
    fresh = cache.get(FRESH_BANKS_CACHE_KEY)
    if fresh is not None:
        return fresh
    if cache.get(UNAVAILABLE_BANKS_CACHE_KEY):
        stale = cache.get(STALE_BANKS_CACHE_KEY)
        if stale is not None:
            return stale
        raise BankDirectoryUnavailable("The VietQR bank directory is temporarily unavailable")
    try:
        response = requests.get(VIETQR_BANKS_URL, timeout=5)
        response.raise_for_status()
        banks = _normalized_banks(response.json())
    except (requests.RequestException, ValueError, BankDirectoryUnavailable) as error:
        fresh = cache.get(FRESH_BANKS_CACHE_KEY)
        if fresh is not None:
            return fresh
        stale = cache.get(STALE_BANKS_CACHE_KEY)
        if stale is not None:
            return stale
        cache.set(UNAVAILABLE_BANKS_CACHE_KEY, True, timeout=NEGATIVE_CACHE_TTL)
        raise BankDirectoryUnavailable("The VietQR bank directory is unavailable") from error
    cache.set(FRESH_BANKS_CACHE_KEY, banks, timeout=BANKS_CACHE_TTL)
    cache.set(STALE_BANKS_CACHE_KEY, banks, timeout=None)
    cache.delete(UNAVAILABLE_BANKS_CACHE_KEY)
    return banks


def find_transfer_bank(bank_bin):
    normalized = str(bank_bin).strip()
    return next((bank for bank in get_transfer_banks() if bank["bin"] == normalized), None)
