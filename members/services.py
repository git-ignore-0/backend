from django.db import transaction

from members.models import MemberProfile


@transaction.atomic
def update_member_profile(*, user, data):
    locked_user = user.__class__.objects.select_for_update().get(pk=user.pk)
    profile, _created = MemberProfile.objects.select_for_update().get_or_create(user=locked_user)

    if "name" in data and locked_user.name != data["name"]:
        locked_user.name = data["name"]
        locked_user.save(update_fields=["name", "updated_at"])

    profile_fields = {
        "phone_number",
        "date_of_birth",
        "province_code",
        "ward_code",
        "address",
    }
    changed_fields = []
    for field in profile_fields.intersection(data):
        value = data[field]
        if getattr(profile, field) != value:
            setattr(profile, field, value)
            changed_fields.append(field)
    if changed_fields:
        profile.save(update_fields=[*changed_fields, "updated_at"])
    return locked_user, profile
