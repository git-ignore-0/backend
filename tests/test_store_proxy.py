import hashlib
import hmac
from time import time

from django.core.cache import cache
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from commerce.store_proxy import trusted_store_client_ip
from commerce.store_views import GuestLookupThrottle

SECRET = "test-store-bff-shared-secret-32-characters"


def request(ip, *, secret=SECRET, timestamp=None, forged=False):
    timestamp = str(int(time()) if timestamp is None else timestamp)
    signature = hmac.new(
        secret.encode(), f"store-guest-lookup:v1\n{timestamp}\n{ip}".encode(), hashlib.sha256
    ).hexdigest()
    return Request(
        APIRequestFactory().post(
            "/api/v1/public/store-order-lookups",
            REMOTE_ADDR="192.0.2.10",
            HTTP_X_NFV_STORE_CLIENT_IP=ip,
            HTTP_X_NFV_STORE_CLIENT_TIME=timestamp,
            HTTP_X_NFV_STORE_CLIENT_SIGNATURE="0" * 64 if forged else signature,
        )
    )


def test_signed_clients_have_independent_lookup_limits(settings):
    settings.STORE_BFF_SHARED_SECRET = SECRET
    cache.clear()
    throttle = GuestLookupThrottle()
    try:
        for _ in range(10):
            assert throttle.allow_request(request("198.51.100.1"), None)
            assert throttle.allow_request(request("198.51.100.2"), None)
        assert not throttle.allow_request(request("198.51.100.1"), None)
        assert not throttle.allow_request(request("198.51.100.2"), None)
        assert throttle.allow_request(request("198.51.100.3"), None)
    finally:
        cache.clear()


def test_invalid_proofs_cannot_override_the_fallback_throttle(settings):
    settings.STORE_BFF_SHARED_SECRET = SECRET
    throttle = GuestLookupThrottle()
    first = request("198.51.100.1", forged=True)
    second = request("198.51.100.2", forged=True)
    second.META["HTTP_X_FORWARDED_FOR"] = "203.0.113.12"
    assert throttle.get_cache_key(first, None) == throttle.get_cache_key(second, None)
    for value in [
        first,
        request("198.51.100.1", secret="wrong-key"),
        request("198.51.100.1", timestamp=int(time()) - 120),
        request("198.51.100.1", timestamp=int(time()) + 120),
        request("198.51.100.1", timestamp="9" * 50),
        request("not-an-ip"),
    ]:
        assert trusted_store_client_ip(value) is None
    non_ascii = request("198.51.100.1")
    non_ascii.META["HTTP_X_NFV_STORE_CLIENT_SIGNATURE"] = "é" * 64
    assert trusted_store_client_ip(non_ascii) is None
    assert trusted_store_client_ip(request("2001:0db8::1")) == "2001:db8::1"
    settings.STORE_BFF_SHARED_SECRET = ""
    assert trusted_store_client_ip(request("198.51.100.1")) is None
