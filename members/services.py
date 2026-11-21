from django.core.exceptions import PermissionDenied
from django.db import transaction

from members.models import MemberProfile


@transaction.atomic
def update_member_profile(*, user, data) -> bool:
    locked_user = user.__class__.objects.select_for_update().filter(pk=user.pk).first()
    if locked_user is None or not locked_user.can_access_core():
        raise PermissionDenied
    profile, _created = MemberProfile.objects.select_for_update().get_or_create(user=locked_user)

    name_changed = "name" in data and locked_user.name != data["name"]
    if name_changed:
        locked_user.name = data["name"]
        locked_user.save(update_fields=["name", "updated_at"])

    birth_date_changed = "date_of_birth" in data and profile.date_of_birth != data["date_of_birth"]
    if birth_date_changed:
        profile.date_of_birth = data["date_of_birth"]
        profile.save(update_fields=["date_of_birth", "updated_at"])
    return name_changed or birth_date_changed
