import hashlib
import hmac

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle, UserRateThrottle

from memberships.phones import InvalidPhoneNumber, normalize_vietnam_phone


class CSAPurchaseCreateThrottle(UserRateThrottle):
    scope = "csa_purchase_create"
    rate = "5/minute"


class CSAPurchaseConfirmThrottle(UserRateThrottle):
    scope = "csa_purchase_confirm"
    rate = "20/minute"


class CSATrackerIPThrottle(SimpleRateThrottle):
    scope = "csa_tracker_ip"
    rate = "20/minute"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class CSATrackerCredentialThrottle(SimpleRateThrottle):
    scope = "csa_tracker_credential"
    rate = "5/minute"

    def get_cache_key(self, request, view):
        data = request.data if hasattr(request.data, "get") else {}
        raw_phone = str(data.get("phone", ""))[:64].strip()
        try:
            phone_key = normalize_vietnam_phone(raw_phone)
        except InvalidPhoneNumber:
            phone_key = raw_phone
        credential = "\x00".join(
            (
                str(data.get("reference_code", ""))[:64].strip().upper(),
                phone_key,
            )
        )
        digest = hmac.new(
            settings.SECRET_KEY.encode(),
            credential.encode(),
            hashlib.sha256,
        ).hexdigest()
        return self.cache_format % {"scope": self.scope, "ident": digest}


class CSATrackerSessionThrottle(SimpleRateThrottle):
    scope = "csa_tracker_session"
    rate = "60/minute"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class CSAContractVerificationThrottle(SimpleRateThrottle):
    scope = "csa_contract_verification"
    rate = "20/minute"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
