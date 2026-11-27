"""Private Farm credentials. Only the admin link endpoint decrypts them."""

import base64
import hashlib
import hmac
import re
from secrets import token_urlsafe

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.db import transaction
from django.views.decorators.debug import sensitive_variables
from rest_framework.exceptions import PermissionDenied

from content.services import audit
from farms.models import Farm, FarmFarmerLink
from farms.services import FarmConflict

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


class InvalidFarmerToken(PermissionDenied):
    default_detail = {"errors": [{"code": "invalid_farm_token"}]}


def _token_cipher():
    keys = [settings.SECRET_KEY, *settings.SECRET_KEY_FALLBACKS]
    return MultiFernet(
        [
            Fernet(
                base64.urlsafe_b64encode(
                    hmac.digest(key.encode(), b"nfv:farm-farmer-link:v1", "sha256")
                )
            )
            for key in keys
        ]
    )


@sensitive_variables("token")
def _digest(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


@sensitive_variables("token")
def read_farmer_token(token):
    if not token or not TOKEN_PATTERN.fullmatch(token):
        raise InvalidFarmerToken
    return _digest(token)


def farm_for_token(digest, *, locked_farm_id=None):
    rows = FarmFarmerLink.objects.filter(token_digest=digest)
    if locked_farm_id is None:
        rows = rows.select_related("farm")
    else:
        # Use a current read after the Farm lock, including under MySQL REPEATABLE READ.
        rows = rows.select_for_update().filter(farm_id=locked_farm_id)
    link = rows.first()
    if link is None:
        raise InvalidFarmerToken
    return link.farm if locked_farm_id is None else None


@sensitive_variables("token")
def farmer_link_payload(link):
    try:
        token = _token_cipher().decrypt(bytes(link.encrypted_token)).decode("ascii")
    except InvalidToken as error:
        raise FarmConflict("farm_farmer_link_unreadable") from error
    return {"farm_id": str(link.farm_id), "url": f"/farmer#token={token}"}


@transaction.atomic
@sensitive_variables("token")
def write_farmer_link(*, farm_id, authorize_write, rotate=False):
    farm = Farm.objects.select_for_update().get(pk=farm_id)
    actor = authorize_write()
    link = FarmFarmerLink.objects.select_for_update().filter(farm=farm).first()
    if link is not None and not rotate:
        return link, False
    if link is None and rotate:
        raise FarmFarmerLink.DoesNotExist
    created = link is None
    link = link or FarmFarmerLink(farm=farm)
    token = token_urlsafe(32)
    link.token_digest = _digest(token)
    link.encrypted_token = _token_cipher().encrypt(token.encode("ascii"))
    link.save(force_insert=created, force_update=not created)
    audit(actor=actor, action=f"farm.farmer_link.{'create' if created else 'rotate'}", entity=farm)
    return link, created
