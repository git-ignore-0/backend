import importlib
import io
from datetime import timedelta

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.signing import TimestampSigner
from django.test import override_settings
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient

from content.models import SiteSettings, WorkshopRevision
from content.services import store_workshop_image
from content.validators import validate_tiptap_document


def image_upload(name="banner.png"):
    output = io.BytesIO()
    Image.new("RGB", (1200, 675), "#355b45").save(output, format="PNG")
    return SimpleUploadedFile(name, output.getvalue(), content_type="image/png")


@pytest.fixture
def admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def workshop_payload(*, locale="vi"):
    now = timezone.now() + timedelta(days=3)
    return {
        "default_locale": locale,
        "start_at": now.isoformat(),
        "end_at": (now + timedelta(hours=5)).isoformat(),
        "event_timezone": "Asia/Ho_Chi_Minh",
        "registration_url": "https://forms.example.com/register",
        "translations": [
            {
                "locale": locale,
                "title": "Nền tảng đất sống",
                "summary": "Một ngày thực hành với đất và vi sinh vật.",
                "body": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "image",
                            "attrs": {
                                "src": "https://images.example.com/living-soil.webp",
                                "alt": "Học viên quan sát đất",
                            },
                        },
                        {"type": "paragraph", "content": [{"type": "text", "text": "Nội dung"}]},
                    ],
                },
            }
        ],
    }


def publish_workshop(admin_api, workshop):
    return admin_api.post(
        f"/api/v1/admin/workshops/{workshop['id']}/publish/",
        {"revision_id": workshop["revision_id"], "version": workshop["version"]},
        format="json",
    )


def image_document(source):
    return {
        "type": "doc",
        "content": [{"type": "image", "attrs": {"src": source, "alt": "Workshop"}}],
    }


@override_settings(DEBUG=True)
def test_rich_text_accepts_local_uploaded_image_during_development():
    validate_tiptap_document(
        image_document("http://127.0.0.1:8000/media/workshops/2026/07/image.webp")
    )


@override_settings(DEBUG=True)
def test_rich_text_still_rejects_insecure_external_images():
    with pytest.raises(ValidationError, match="Image URL is not allowed"):
        validate_tiptap_document(image_document("http://untrusted.example/image.webp"))
    with pytest.raises(ValidationError, match="Image URL is not allowed"):
        validate_tiptap_document(image_document("//untrusted.example/image.webp"))


@override_settings(DEBUG=True)
def test_workshop_api_accepts_local_image_and_safe_link(admin_api):
    payload = workshop_payload()
    payload["translations"][0]["body"] = {
        "type": "doc",
        "content": [
            {
                "type": "image",
                "attrs": {
                    "src": "http://127.0.0.1:8000/media/workshops/local.webp",
                    "alt": "Workshop",
                },
            },
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Đăng ký",
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {"href": "http://localhost:3001/workshops/example"},
                            }
                        ],
                    }
                ],
            },
        ],
    }

    response = admin_api.post("/api/v1/admin/workshops/", payload, format="json")

    assert response.status_code == 201, response.json()


@override_settings(DEBUG=True)
def test_rich_text_still_rejects_insecure_external_links():
    document = {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": "Unsafe",
                        "marks": [
                            {
                                "type": "link",
                                "attrs": {"href": "http://untrusted.example/path"},
                            }
                        ],
                    }
                ],
            }
        ],
    }

    with pytest.raises(ValidationError, match="Only HTTPS and email links are allowed"):
        validate_tiptap_document(document)


@pytest.mark.django_db
def test_public_settings_are_seeded(client):
    response = client.get("/api/v1/public/site-settings?locale=en")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["email"] == "naturalfarming@vietnam.com"
    assert {
        "is_location_enabled",
        "location_name",
        "address",
        "organization_name",
        "requested_locale",
        "content_locale",
        "is_fallback",
    }.isdisjoint(data)
    assert {item["kind"] for item in data["links"]} >= {
        "facebook",
        "youtube",
        "store",
        "forum",
    }
    assert "farms" not in {item["kind"] for item in data["links"]}


@pytest.mark.django_db
def test_farms_link_is_available_to_admin_and_hidden_publicly_by_default(client, admin_api):
    admin_data = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    farms = next(item for item in admin_data["links"] if item["kind"] == "farms")

    assert farms["label"] == "See our farms"
    assert farms["url"] == ""
    assert farms["is_enabled"] is False
    public_data = client.get("/api/v1/public/site-settings?locale=en").json()["data"]
    assert "farms" not in {item["kind"] for item in public_data["links"]}


@pytest.mark.django_db
def test_farms_seed_migration_is_idempotent():
    migration = importlib.import_module("content.migrations.0015_add_farms_external_link")
    ExternalLink = django_apps.get_model("content", "ExternalLink")
    ExternalLink.objects.filter(kind="farms").delete()

    migration.add_disabled_farms_link(django_apps, None)
    migration.add_disabled_farms_link(django_apps, None)

    farms_links = ExternalLink.objects.filter(kind="farms")
    assert farms_links.count() == 1
    farms = farms_links.get()
    assert farms.url == ""
    assert farms.is_enabled is False


@pytest.mark.django_db
def test_admin_can_enable_update_and_disable_farms_link(client, admin_api):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    farms = next(item for item in current["links"] if item["kind"] == "farms")
    farms.update(url="https://farms.example.com/visit", is_enabled=True)

    enabled_response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert enabled_response.status_code == 200, enabled_response.json()
    enabled = next(
        item for item in enabled_response.json()["data"]["links"] if item["kind"] == "farms"
    )
    assert enabled["url"] == "https://farms.example.com/visit"
    assert enabled["is_enabled"] is True
    public_links = client.get("/api/v1/public/site-settings?locale=vi").json()["data"]["links"]
    assert next(item for item in public_links if item["kind"] == "farms")["url"] == (
        "https://farms.example.com/visit"
    )

    updated = enabled_response.json()["data"]
    next(item for item in updated["links"] if item["kind"] == "farms")["is_enabled"] = False
    disabled_response = admin_api.patch("/api/v1/admin/site-settings", updated, format="json")

    assert disabled_response.status_code == 200, disabled_response.json()
    saved = next(
        item for item in disabled_response.json()["data"]["links"] if item["kind"] == "farms"
    )
    assert saved["url"] == "https://farms.example.com/visit"
    assert saved["is_enabled"] is False
    public_links = client.get("/api/v1/public/site-settings?locale=en").json()["data"]["links"]
    assert "farms" not in {item["kind"] for item in public_links}

    updated = disabled_response.json()["data"]
    next(item for item in updated["links"] if item["kind"] == "farms")["is_enabled"] = True
    re_enabled_response = admin_api.patch("/api/v1/admin/site-settings", updated, format="json")

    assert re_enabled_response.status_code == 200, re_enabled_response.json()
    public_links = client.get("/api/v1/public/site-settings?locale=vi").json()["data"]["links"]
    assert next(item for item in public_links if item["kind"] == "farms")["url"] == (
        "https://farms.example.com/visit"
    )


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["", "ftp://farms.example.com", "javascript:alert(1)"])
def test_enabled_farms_link_requires_valid_http_url(admin_api, url):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    farms = next(item for item in current["links"] if item["kind"] == "farms")
    farms.update(url=url, is_enabled=True)

    response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert response.status_code == 400
    assert "links" in response.json()


@pytest.mark.django_db
def test_hidden_contact_settings_are_not_exposed_publicly(client):
    site_settings = SiteSettings.objects.get(pk="default")
    site_settings.is_email_enabled = False
    site_settings.is_phone_enabled = False
    site_settings.save()

    data = client.get("/api/v1/public/site-settings?locale=vi").json()["data"]

    assert data["email"] == ""
    assert data["phone_display"] == ""
    assert data["is_email_enabled"] is False
    assert data["is_phone_enabled"] is False


@pytest.mark.django_db
def test_admin_can_toggle_site_setting_visibility_without_losing_values(admin_api):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    assert "translations" not in current
    current.update(
        {
            "is_email_enabled": False,
            "is_phone_enabled": False,
        }
    )

    response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert response.status_code == 200, response.json()
    saved = response.json()["data"]
    assert saved["email"] == "naturalfarming@vietnam.com"
    assert saved["phone_display"] == "+84 97 151 91 85"
    assert saved["is_email_enabled"] is False
    assert saved["is_phone_enabled"] is False


@pytest.mark.django_db
def test_disabled_external_link_can_be_saved_without_url(admin_api):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    current["links"].append(
        {
            "kind": "other",
            "label": "Partner",
            "url": "",
            "position": 2,
            "is_enabled": False,
        }
    )

    response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert response.status_code == 200, response.json()
    saved_link = next(item for item in response.json()["data"]["links"] if item["kind"] == "other")
    assert saved_link["url"] == ""
    assert saved_link["is_enabled"] is False


@pytest.mark.django_db
def test_enabled_external_link_requires_url(admin_api):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    current["links"].append(
        {
            "kind": "other",
            "label": "Partner",
            "url": "",
            "position": 2,
            "is_enabled": True,
        }
    )

    response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert response.status_code == 400
    assert "links" in response.json()


@pytest.mark.django_db
def test_google_maps_is_not_an_supported_site_link(admin_api):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    current["links"].append(
        {
            "kind": "google_maps",
            "label": "Google Maps",
            "url": "https://maps.google.com/example",
            "position": 2,
            "is_enabled": True,
        }
    )

    response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert response.status_code == 400
    assert "links" in response.json()


@pytest.mark.django_db
def test_standard_site_link_kind_cannot_be_duplicated(admin_api):
    current = admin_api.get("/api/v1/admin/site-settings").json()["data"]
    facebook = next(item for item in current["links"] if item["kind"] == "facebook")
    current["links"].append({**facebook, "id": None, "position": 99})

    response = admin_api.patch("/api/v1/admin/site-settings", current, format="json")

    assert response.status_code == 400
    assert "links" in response.json()


@pytest.mark.django_db
def test_admin_api_requires_group_and_scope(access_token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    response = client.get("/api/v1/admin/dashboard")
    assert response.status_code == 403


@pytest.mark.django_db
@pytest.mark.parametrize("query", ["page=abc", "page_size=none", "page=0", "page_size=51"])
def test_public_workshop_pagination_rejects_invalid_values(client, query):
    response = client.get(f"/api/v1/public/workshops?{query}")

    assert response.status_code == 400


@pytest.mark.django_db
def test_public_workshop_detail_rejects_unsupported_locale(admin_api):
    created = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")
    workshop = created.json()["data"]
    assert publish_workshop(admin_api, workshop).status_code == 200

    detail_response = admin_api.get(f"/api/v1/public/workshops/{workshop['slug']}?locale=fr")

    assert detail_response.status_code == 400


@pytest.mark.django_db
def test_workshop_preview_token_and_public_read_are_not_available(admin_api):
    created = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")
    workshop = created.json()["data"]
    legacy_token = TimestampSigner(salt="nfv-workshop-preview").sign(workshop["revision_id"])

    token_response = admin_api.post(f"/api/v1/admin/workshops/{workshop['id']}/preview-token/")
    public_response = admin_api.get(f"/api/v1/public/workshops/preview/{legacy_token}?locale=vi")

    assert token_response.status_code == 404
    assert public_response.status_code == 404


@pytest.mark.django_db
def test_unpublish_hides_workshop_from_public_list_and_detail_but_keeps_draft(admin_api):
    created = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")
    workshop = created.json()["data"]
    assert publish_workshop(admin_api, workshop).status_code == 200

    response = admin_api.post(f"/api/v1/admin/workshops/{workshop['id']}/unpublish/")

    assert response.status_code == 200
    assert response.json()["data"] == {"unpublished": True}
    assert WorkshopRevision.objects.filter(workshop_id=workshop["id"], is_draft=True).count() == 1
    assert not WorkshopRevision.objects.filter(
        workshop_id=workshop["id"], is_published=True
    ).exists()

    public_list = admin_api.get("/api/v1/public/workshops?locale=vi&period=upcoming")
    public_detail = admin_api.get(f"/api/v1/public/workshops/{workshop['slug']}?locale=vi")

    assert public_list.status_code == 200
    assert public_list.json()["data"] == []
    assert public_detail.status_code == 404


@pytest.mark.django_db
def test_workshop_publish_and_locale_fallback(admin_api):
    created = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")
    assert created.status_code == 201, created.json()
    workshop = created.json()["data"]
    workshop_id = workshop["id"]
    assert workshop["slug"] == "nen-tang-dat-song"
    assert workshop["translations"][0]["is_ready"] is True

    published = publish_workshop(admin_api, workshop)
    assert published.status_code == 200, published.json()
    assert WorkshopRevision.objects.filter(workshop_id=workshop_id, is_draft=True).count() == 1
    assert WorkshopRevision.objects.filter(workshop_id=workshop_id, is_published=True).count() == 1

    public = admin_api.get("/api/v1/public/workshops/nen-tang-dat-song?locale=en")
    assert public.status_code == 200
    data = public.json()["data"]
    assert data["content_locale"] == "vi"
    assert data["requested_locale"] == "en"
    assert data["is_fallback"] is True
    assert data["available_locales"] == ["vi"]
    assert "cover" not in data
    assert {
        "location_name",
        "address",
        "map_url",
        "instructor",
        "fee",
    }.isdisjoint(data)
    assert data["body"]["content"][0] == {
        "type": "image",
        "attrs": {
            "src": "https://images.example.com/living-soil.webp",
            "alt": "Học viên quan sát đất",
        },
    }
    assert "banner" not in data

    retried = publish_workshop(admin_api, workshop)
    assert retried.status_code == 409
    assert WorkshopRevision.objects.filter(workshop_id=workshop_id, is_draft=True).count() == 1
    assert WorkshopRevision.objects.filter(workshop_id=workshop_id, is_published=True).count() == 1


@pytest.mark.django_db
def test_duplicate_workshop_titles_receive_unique_automatic_slugs(admin_api):
    first = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")
    second = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["data"]["slug"] == "nen-tang-dat-song"
    assert second.json()["data"]["slug"] == "nen-tang-dat-song-2"


@pytest.mark.django_db
def test_only_complete_language_becomes_default_and_can_publish(admin_api):
    payload = workshop_payload()
    payload["default_locale"] = "vi"
    payload["translations"] = [
        {
            "locale": "vi",
            "title": "",
            "summary": "",
            "body": {"type": "doc", "content": [{"type": "paragraph"}]},
        },
        {
            "locale": "en",
            "title": "Living soil",
            "summary": "A practical workshop.",
            "body": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Details"}]}
                ],
            },
        },
    ]

    created = admin_api.post("/api/v1/admin/workshops/", payload, format="json")

    assert created.status_code == 201, created.json()
    workshop = created.json()["data"]
    assert workshop["default_locale"] == "en"
    assert workshop["slug"] == "living-soil"
    assert publish_workshop(admin_api, workshop).status_code == 200


@pytest.mark.django_db
def test_draft_slug_follows_title_but_freezes_after_publish(admin_api):
    created = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json").json()[
        "data"
    ]
    translations = created["translations"]
    translations[0]["title"] = "Workshop mới"
    updated = admin_api.patch(
        f"/api/v1/admin/workshops/{created['id']}/",
        {"version": created["version"], "translations": translations},
        format="json",
    ).json()["data"]
    assert updated["slug"] == "workshop-moi"

    published = publish_workshop(admin_api, updated)
    draft = published.json()["data"]["draft"]
    draft["translations"][0]["title"] = "Tên sau xuất bản"
    renamed = admin_api.patch(
        f"/api/v1/admin/workshops/{draft['id']}/",
        {"version": draft["version"], "translations": draft["translations"]},
        format="json",
    )
    assert renamed.status_code == 200
    assert renamed.json()["data"]["slug"] == "workshop-moi"


@pytest.mark.django_db
def test_draft_update_rejects_stale_version(admin_api):
    created = admin_api.post("/api/v1/admin/workshops/", workshop_payload(), format="json")
    workshop_id = created.json()["data"]["id"]
    response = admin_api.patch(
        f"/api/v1/admin/workshops/{workshop_id}/",
        {"version": 999, "registration_url": "https://example.com/register"},
        format="json",
    )
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "version_conflict"


@pytest.mark.parametrize(
    ("start_offset", "end_offset", "expected_status"),
    [
        (timedelta(hours=1), timedelta(hours=2), "upcoming"),
        (timedelta(0), timedelta(hours=1), "ongoing"),
        (timedelta(hours=-1), timedelta(hours=1), "ongoing"),
        (timedelta(hours=-2), timedelta(0), "completed"),
        (timedelta(hours=-2), timedelta(hours=-1), "completed"),
    ],
)
def test_workshop_status_is_derived_from_schedule(start_offset, end_offset, expected_status):
    now = timezone.now()
    revision = WorkshopRevision(
        start_at=now + start_offset,
        end_at=now + end_offset,
    )

    assert revision.effective_status_at(now) == expected_status


@pytest.mark.django_db
def test_ongoing_status_is_exposed_by_public_api(admin_api):
    payload = workshop_payload()
    payload["translations"][0]["title"] = "Ongoing workshop"
    payload["start_at"] = (timezone.now() - timedelta(hours=1)).isoformat()
    payload["end_at"] = (timezone.now() + timedelta(hours=1)).isoformat()
    created = admin_api.post("/api/v1/admin/workshops/", payload, format="json")
    workshop = created.json()["data"]
    assert publish_workshop(admin_api, workshop).status_code == 200
    response = admin_api.get("/api/v1/public/workshops/ongoing-workshop?locale=vi")
    upcoming = admin_api.get("/api/v1/public/workshops?locale=vi&period=upcoming")

    assert response.json()["data"]["status"] == "ongoing"
    assert [item["slug"] for item in upcoming.json()["data"]] == ["ongoing-workshop"]


@pytest.mark.django_db
def test_completed_status_is_exposed_by_public_api(admin_api):
    payload = workshop_payload()
    payload["translations"][0]["title"] = "Past workshop"
    payload["start_at"] = (timezone.now() - timedelta(days=2)).isoformat()
    payload["end_at"] = (timezone.now() - timedelta(days=1)).isoformat()
    created = admin_api.post("/api/v1/admin/workshops/", payload, format="json")
    workshop = created.json()["data"]
    assert publish_workshop(admin_api, workshop).status_code == 200
    response = admin_api.get("/api/v1/public/workshops/past-workshop?locale=vi")
    past = admin_api.get("/api/v1/public/workshops?locale=vi&period=past")

    assert response.json()["data"]["status"] == "completed"
    assert [item["slug"] for item in past.json()["data"]] == ["past-workshop"]


@pytest.mark.django_db
def test_publish_rejects_empty_rich_text(admin_api):
    payload = workshop_payload()
    payload["translations"][0]["title"] = "Empty content"
    payload["translations"][0]["body"] = {
        "type": "doc",
        "content": [{"type": "paragraph"}],
    }
    created = admin_api.post("/api/v1/admin/workshops/", payload, format="json")
    response = publish_workshop(admin_api, created.json()["data"])
    assert response.status_code == 400
    assert "translation" in str(response.json())


@pytest.mark.django_db
def test_workshop_rejects_unknown_iana_timezone(admin_api):
    now = timezone.now() + timedelta(days=3)
    payload = workshop_payload()
    payload.update(
        {
            "start_at": now.isoformat(),
            "end_at": (now + timedelta(hours=2)).isoformat(),
            "event_timezone": "Mars/Olympus_Mons",
        }
    )
    response = admin_api.post("/api/v1/admin/workshops/", payload, format="json")
    assert response.status_code == 400
    assert "event_timezone" in response.json()


def test_public_workshop_uses_null_when_registration_url_is_missing(admin_api):
    payload = workshop_payload()
    payload["registration_url"] = ""
    created = admin_api.post("/api/v1/admin/workshops/", payload, format="json")
    workshop = created.json()["data"]
    assert publish_workshop(admin_api, workshop).status_code == 200

    response = admin_api.get(f"/api/v1/public/workshops/{workshop['slug']}?locale=vi")

    assert response.status_code == 200
    assert response.json()["data"]["registration_url"] is None


def test_oversized_image_is_rejected_before_decoding(monkeypatch, settings):
    class OversizedImage:
        width = 5000
        height = 5000
        format = "PNG"
        loaded = False

        def verify(self):
            return None

        def load(self):
            self.loaded = True

    image = OversizedImage()
    monkeypatch.setattr("content.services.Image.open", lambda _upload: image)
    settings.CONTENT_MAX_IMAGE_PIXELS = 16_000_000

    with pytest.raises(ValueError, match="dimensions are too large"):
        store_workshop_image(image_upload("oversized.png"), actor=None)

    assert image.loaded is False
