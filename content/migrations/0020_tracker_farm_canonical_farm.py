import django.db.models.deletion
from django.db import migrations, models


def copy_tracker_farms(apps, schema_editor):
    db = schema_editor.connection.alias
    TrackerFarm = apps.get_model("content", "TrackerFarm")
    TrackerFarmPhoto = apps.get_model("content", "TrackerFarmPhoto")
    Farm = apps.get_model("farms", "Farm")
    FarmPhoto = apps.get_model("farms", "FarmPhoto")
    tracker_owned_ids = set()

    for profile in TrackerFarm.objects.using(db).all().iterator():
        farm, created = Farm.objects.using(db).get_or_create(
            id=profile.pk,
            defaults={
                "name_en": profile.name_en,
                "name_vi": profile.name_vi,
                "location_en": profile.location_en,
                "location_vi": profile.location_vi,
                "description_en": profile.description_en,
                "description_vi": profile.description_vi,
                "image_id": profile.image_id,
                "version": profile.version,
                "tracker_profile_revision": profile.version,
                "legacy_tracker_owned": True,
            },
        )
        if created:
            Farm.objects.using(db).filter(pk=profile.pk).update(
                created_at=profile.created_at,
                updated_at=profile.updated_at,
            )
        if farm.legacy_tracker_owned:
            tracker_owned_ids.add(profile.pk)
            if not created:
                Farm.objects.using(db).filter(pk=profile.pk).update(
                    name_en=profile.name_en,
                    name_vi=profile.name_vi,
                    location_en=profile.location_en,
                    location_vi=profile.location_vi,
                    description_en=profile.description_en,
                    description_vi=profile.description_vi,
                    image_id=profile.image_id,
                    version=profile.version,
                    tracker_profile_revision=max(farm.tracker_profile_revision, profile.version),
                    updated_at=profile.updated_at,
                )
        elif farm.tracker_profile_revision < profile.version:
            Farm.objects.using(db).filter(pk=profile.pk).update(
                tracker_profile_revision=profile.version
            )
        TrackerFarm.objects.using(db).filter(pk=profile.pk).update(farm_id=profile.pk)

    FarmPhoto.objects.using(db).filter(farm_id__in=tracker_owned_ids).delete()
    for photo in TrackerFarmPhoto.objects.using(db).all().iterator():
        if photo.farm_id not in tracker_owned_ids:
            continue
        FarmPhoto.objects.using(db).create(
            id=photo.pk,
            farm_id=photo.farm_id,
            image_id=photo.image_id,
            sort_order=photo.sort_order,
        )
        FarmPhoto.objects.using(db).filter(pk=photo.pk).update(
            created_at=photo.created_at,
            updated_at=photo.updated_at,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("content", "0019_tracker_farm_one_month_signup_count"),
        ("farms", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="trackerfarm",
            name="farm",
            field=models.OneToOneField(
                to="farms.farm",
                related_name="tracker_profile",
                null=True,
                blank=True,
                on_delete=django.db.models.deletion.PROTECT,
            ),
        ),
        migrations.RunPython(copy_tracker_farms, migrations.RunPython.noop),
    ]
