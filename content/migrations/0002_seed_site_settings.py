from django.db import migrations


def seed_content(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    ExternalLink = apps.get_model("content", "ExternalLink")
    SiteSettings = apps.get_model("content", "SiteSettings")
    SiteSettingsTranslation = apps.get_model("content", "SiteSettingsTranslation")

    Group.objects.get_or_create(name="website_admin")
    settings, _created = SiteSettings.objects.get_or_create(
        key="default",
        defaults={
            "email": "naturalfarming@vietnam.com",
            "phone_display": "+84 97 151 91 85",
            "phone_e164": "+84971519185",
        },
    )
    for locale, name in (("vi", "Natural Farming Vietnam"), ("en", "Natural Farming Vietnam")):
        SiteSettingsTranslation.objects.get_or_create(
            settings=settings,
            locale=locale,
            defaults={"organization_name": name, "location_name": "", "address": ""},
        )
    links = [
        ("facebook", "Facebook", "https://www.facebook.com/naturalfarmingvn/", 10),
        ("youtube", "YouTube", "https://www.youtube.com/@naturalfarmingvietnam", 20),
        ("store", "Store", "https://store.farmbrite.com/store/nntn", 30),
        ("forum", "Forum", "https://forum.naturalfarmingvietnam.com/", 40),
    ]
    for kind, label, url, position in links:
        ExternalLink.objects.get_or_create(
            settings=settings,
            kind=kind,
            defaults={"label": label, "url": url, "position": position, "is_enabled": True},
        )


class Migration(migrations.Migration):
    dependencies = [("content", "0001_initial")]
    operations = [migrations.RunPython(seed_content, migrations.RunPython.noop)]
