"""Core password writes and credential revocation."""

from django.contrib.auth.hashers import check_password
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils.crypto import constant_time_compare
from django.views.debug import SafeExceptionReporterFilter
from oauth2_provider.models import get_access_token_model, get_grant_model, get_refresh_token_model

from identity.models import User


class AccountPasswordReporterFilter(SafeExceptionReporterFilter):
    def get_traceback_frame_variables(self, request, tb_frame):
        # Forms and JSON serializers copy plaintext into framework locals.
        # Hide those locals only on password requests that install this filter.
        return [(key, self.cleansed_substitute) for key in tb_frame.f_locals]


def can_reset_password(user: User) -> bool:
    return user.can_authenticate() and bool(user.email) and user.email_verified


@transaction.atomic
def set_authenticated_password(*, user_id, session_hash, password, current_password=None) -> User:
    user = User.objects.select_for_update().filter(pk=user_id).first()
    if (
        user is None
        or not user.can_access_core()
        or not constant_time_compare(session_hash, user.get_session_auth_hash())
    ):
        raise PermissionDenied
    if current_password is None:
        if user.has_usable_password():
            raise PermissionDenied
    elif not check_password(current_password, user.password):
        raise PermissionDenied
    user.set_password(password)
    user.save(update_fields=["password", "updated_at"])
    return user


@transaction.atomic
def revoke_core_credentials(*, user: User) -> None:
    locked = User.objects.select_for_update().get(pk=user.pk)
    for refresh in (
        get_refresh_token_model()
        .objects.select_for_update()
        .filter(user=locked, revoked__isnull=True)
    ):
        refresh.revoke()
    get_access_token_model().objects.filter(user=locked).delete()
    get_grant_model().objects.filter(user=locked).delete()
