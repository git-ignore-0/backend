"""Verify the public BFF's short-lived, purpose-specific client IP assertion."""

import hashlib
import hmac
import ipaddress
import re
import time

from django.conf import settings


def trusted_store_client_ip(request):
    secret = settings.STORE_BFF_SHARED_SECRET
    ip = request.headers.get("X-NFV-Store-Client-IP", "")
    timestamp = request.headers.get("X-NFV-Store-Client-Time", "")
    signature = request.headers.get("X-NFV-Store-Client-Signature", "")
    if not secret or len(secret) < 32 or not timestamp.isascii() or not timestamp.isdecimal():
        return None
    if len(timestamp) > 12 or len(ip) > 45 or not re.fullmatch(r"[0-9a-f]{64}", signature):
        return None
    if abs(time.time() - int(timestamp)) > 60:
        return None
    try:
        address = str(ipaddress.ip_address(ip))
    except ValueError:
        return None
    expected = hmac.new(
        secret.encode(), f"store-guest-lookup:v1\n{timestamp}\n{ip}".encode(), hashlib.sha256
    ).hexdigest()
    return address if hmac.compare_digest(signature, expected) else None
