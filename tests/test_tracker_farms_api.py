import importlib
import io
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, migrations, transaction
from PIL import Image
from rest_framework.test import APIClient

from content.models import MediaAsset, TrackerFarm


def farm_payload(**overrides):
    payload = {
        "name_en": "Green Valley Farm",
        "name_vi": "Nông trại Thung Lũng Xanh",
        "location_en": "Da Lat, Vietnam",
        "location_vi": "Đà Lạt, Việt Nam",
        "signup_count": 14,
        "is_visible": True,
    }
    payload.update(overrides)
    return payload


def create_farm(**overrides):
    return TrackerFarm.objects.create(**farm_payload(**overrides))


def image_upload():
    output = io.BytesIO()
    Image.new("RGB", (24, 16), "green").save(output, "PNG")
    return SimpleUploadedFile("farm.png", output.getvalue(), content_type="image/png")


def create_media_asset(**overrides):
    values = {
        "file": f"farms/test-{uuid4()}.webp",
        "variants": [],
        "width": 1200,
        "height": 900,
        "original_name": "farm.webp",
    }
    values.update(overrides)
    return MediaAsset.objects.create(**values)


@pytest.fixture
def tracker_admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


@pytest.mark.django_db
def test_tracker_farm_model_defaults_and_database_constraints():
    farm = TrackerFarm.objects.create(
        name_en="Farm",
        name_vi="Nông trại",
        location_en="Location",
        location_vi="Địa điểm",
    )

    assert farm.signup_count == 0
    assert farm.is_visible is True
    assert farm.sort_order == 0
    assert farm.version == 1
    assert farm.image is None
    assert farm.description_en == ""
    assert farm.description_vi == ""
    assert farm.created_at is not None
    assert farm.updated_at is not None

    with pytest.raises(IntegrityError), transaction.atomic():
        TrackerFarm.objects.filter(pk=farm.pk).update(signup_count=-1)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrackerFarm.objects.filter(pk=farm.pk).update(sort_order=-1)


@pytest.mark.django_db
def test_admin_create_update_and_permanent_delete(tracker_admin_api):
    image = create_media_asset()
    first = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(
            name_en="  First Farm  ",
            location_en="  First Place  ",
            description_en="  A family farm  ",
            description_vi="  Nông trại gia đình  ",
            image_id=str(image.pk),
        ),
        format="json",
    )
    second = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(name_en="Second Farm"),
        format="json",
    )

    assert first.status_code == 201, first.json()
    assert second.status_code == 201, second.json()
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert first_data["name_en"] == "First Farm"
    assert first_data["location_en"] == "First Place"
    assert first_data["description_en"] == "A family farm"
    assert first_data["description_vi"] == "Nông trại gia đình"
    assert first_data["image"]["id"] == str(image.pk)
    assert first_data["sort_order"] == 0
    assert second_data["sort_order"] == 1

    updated = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{first_data['id']}",
        {
            "version": first_data["version"],
            "signup_count": 27,
            "is_visible": False,
            "name_vi": "  Nông trại đầu tiên  ",
            "description_en": None,
            "description_vi": "  Mô tả mới  ",
            "image_id": None,
        },
        format="json",
    )
    assert updated.status_code == 200, updated.json()
    assert updated.json()["data"]["signup_count"] == 27
    assert updated.json()["data"]["is_visible"] is False
    assert updated.json()["data"]["name_vi"] == "Nông trại đầu tiên"
    assert updated.json()["data"]["description_en"] == ""
    assert updated.json()["data"]["description_vi"] == "Mô tả mới"
    assert updated.json()["data"]["image"] is None
    assert updated.json()["data"]["version"] == first_data["version"] + 1

    deleted = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{first_data['id']}")
    assert deleted.status_code == 204
    assert not TrackerFarm.objects.filter(pk=first_data["id"]).exists()
    assert TrackerFarm.objects.filter(pk=second_data["id"]).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("signup_count", [-1, "not-an-integer", 1.5])
def test_signup_count_rejects_negative_and_invalid_values(tracker_admin_api, signup_count):
    response = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(signup_count=signup_count),
        format="json",
    )

    assert response.status_code == 400
    assert TrackerFarm.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["name_en", "name_vi", "location_en", "location_vi"])
@pytest.mark.parametrize("value", [None, "   "])
def test_required_bilingual_fields_are_trimmed_and_required(tracker_admin_api, field, value):
    payload = farm_payload()
    if value is None:
        payload.pop(field)
    else:
        payload[field] = value

    response = tracker_admin_api.post("/api/v1/admin/tracker-farms", payload, format="json")

    assert response.status_code == 400
    assert TrackerFarm.objects.count() == 0


@pytest.mark.django_db
def test_public_tracker_farms_localize_en_and_vi_and_hide_internal_fields(client):
    image = create_media_asset()
    farm = create_farm(
        sort_order=4,
        signup_count=14,
        image=image,
        description_en="English description",
        description_vi="Mô tả tiếng Việt",
    )

    english = client.get("/api/v1/public/tracker-farms?locale=en")
    vietnamese = client.get("/api/v1/public/tracker-farms?locale=vi")

    assert english.status_code == 200
    assert english.json()["data"] == [
        {
            "id": str(farm.pk),
            "name": "Green Valley Farm",
            "location": "Da Lat, Vietnam",
            "description": "English description",
            "image": {
                "id": str(image.pk),
                "url": f"http://testserver/media/{image.file.name}",
                "width": 1200,
                "height": 900,
                "alt": "Green Valley Farm",
            },
            "signup_count": 14,
            "sort_order": 4,
        }
    ]
    assert vietnamese.status_code == 200
    assert vietnamese.json()["data"][0] == {
        "id": str(farm.pk),
        "name": "Nông trại Thung Lũng Xanh",
        "location": "Đà Lạt, Việt Nam",
        "description": "Mô tả tiếng Việt",
        "image": {
            "id": str(image.pk),
            "url": f"http://testserver/media/{image.file.name}",
            "width": 1200,
            "height": 900,
            "alt": "Nông trại Thung Lũng Xanh",
        },
        "signup_count": 14,
        "sort_order": 4,
    }
    assert "is_visible" not in english.json()["data"][0]
    assert "version" not in english.json()["data"][0]
    assert client.get("/api/v1/public/tracker-farms?locale=fr").status_code == 400


@pytest.mark.django_db
def test_tracker_farm_allows_missing_image_and_empty_descriptions(client, tracker_admin_api):
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(description_en=None, description_vi=None),
        format="json",
    )

    assert created.status_code == 201, created.json()
    assert created.json()["data"]["image"] is None
    assert created.json()["data"]["description_en"] == ""
    assert created.json()["data"]["description_vi"] == ""

    public = client.get("/api/v1/public/tracker-farms?locale=en")
    assert public.json()["data"][0]["image"] is None
    assert public.json()["data"][0]["description"] == ""


@pytest.mark.django_db
def test_tracker_farm_rejects_an_unknown_image_id(tracker_admin_api):
    response = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(image_id=str(uuid4())),
        format="json",
    )

    assert response.status_code == 400
    assert "image_id" in response.json()


@pytest.mark.django_db
def test_tracker_farm_media_upload_uses_the_farms_prefix(tracker_admin_api):
    response = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms/media",
        {"image": image_upload()},
        format="multipart",
    )

    assert response.status_code == 201, response.json()
    asset = MediaAsset.objects.get(pk=response.json()["data"]["id"])
    assert asset.file.name.startswith("farms/")


@pytest.mark.django_db
def test_hidden_farms_are_omitted_publicly_but_retained_for_admin(client, tracker_admin_api):
    visible = create_farm(name_en="Visible", sort_order=1)
    hidden = create_farm(name_en="Hidden", is_visible=False, sort_order=0)

    public = client.get("/api/v1/public/tracker-farms?locale=en")
    admin = tracker_admin_api.get("/api/v1/admin/tracker-farms")

    assert [item["id"] for item in public.json()["data"]] == [str(visible.pk)]
    assert [item["id"] for item in admin.json()["data"]] == [
        str(hidden.pk),
        str(visible.pk),
    ]


@pytest.mark.django_db
def test_public_tracker_farms_returns_an_empty_list(client):
    response = client.get("/api/v1/public/tracker-farms?locale=en")

    assert response.status_code == 200
    assert response.json() == {"data": []}


@pytest.mark.django_db
def test_tracker_farm_admin_api_enforces_website_admin_permission(access_token):
    anonymous = APIClient()
    bearer = APIClient()
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")

    assert anonymous.get("/api/v1/admin/tracker-farms").status_code in {401, 403}
    assert bearer.get("/api/v1/admin/tracker-farms").status_code == 403
    assert (
        bearer.post("/api/v1/admin/tracker-farms", farm_payload(), format="json").status_code == 403
    )
    assert (
        bearer.post(
            "/api/v1/admin/tracker-farms/media",
            {"image": image_upload()},
            format="multipart",
        ).status_code
        == 403
    )


def reorder_item(farm, sort_order, *, version=None):
    return {
        "id": str(farm.pk),
        "sort_order": sort_order,
        "version": farm.version if version is None else version,
    }


@pytest.mark.django_db
def test_reorder_succeeds_and_increments_versions(tracker_admin_api):
    first = create_farm(name_en="First", sort_order=0)
    second = create_farm(name_en="Second", sort_order=1)
    third = create_farm(name_en="Third", sort_order=2)

    response = tracker_admin_api.patch(
        "/api/v1/admin/tracker-farms/reorder",
        {
            "items": [
                reorder_item(third, 0),
                reorder_item(first, 10),
                reorder_item(second, 20),
            ]
        },
        format="json",
    )

    assert response.status_code == 200, response.json()
    assert [item["id"] for item in response.json()["data"]] == [
        str(third.pk),
        str(first.pk),
        str(second.pk),
    ]
    assert list(TrackerFarm.objects.values_list("sort_order", flat=True)) == [0, 10, 20]
    assert set(TrackerFarm.objects.values_list("version", flat=True)) == {2}


@pytest.mark.django_db
def test_reorder_rejects_duplicate_missing_and_unknown_ids(tracker_admin_api):
    first = create_farm(name_en="First", sort_order=0)
    second = create_farm(name_en="Second", sort_order=1)

    duplicate = tracker_admin_api.patch(
        "/api/v1/admin/tracker-farms/reorder",
        {"items": [reorder_item(first, 0), reorder_item(first, 1)]},
        format="json",
    )
    missing = tracker_admin_api.patch(
        "/api/v1/admin/tracker-farms/reorder",
        {"items": [reorder_item(first, 0)]},
        format="json",
    )
    unknown = tracker_admin_api.patch(
        "/api/v1/admin/tracker-farms/reorder",
        {
            "items": [
                reorder_item(first, 0),
                {"id": str(uuid4()), "sort_order": 1, "version": 1},
            ]
        },
        format="json",
    )

    assert duplicate.status_code == 400
    assert missing.status_code == 400
    assert unknown.status_code == 400
    assert list(TrackerFarm.objects.values_list("pk", "sort_order", "version")) == [
        (first.pk, 0, 1),
        (second.pk, 1, 1),
    ]


@pytest.mark.django_db
def test_reorder_is_atomic_and_returns_conflict_for_a_stale_version(tracker_admin_api):
    first = create_farm(name_en="First", sort_order=0)
    second = create_farm(name_en="Second", sort_order=1)

    response = tracker_admin_api.patch(
        "/api/v1/admin/tracker-farms/reorder",
        {
            "items": [
                reorder_item(second, 0),
                reorder_item(first, 1, version=first.version + 1),
            ]
        },
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "version_conflict"
    assert list(TrackerFarm.objects.values_list("pk", "sort_order", "version")) == [
        (first.pk, 0, 1),
        (second.pk, 1, 1),
    ]


@pytest.mark.django_db
def test_patch_rejects_missing_and_stale_versions(tracker_admin_api):
    farm = create_farm()

    missing = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{farm.pk}",
        {"signup_count": 20},
        format="json",
    )
    stale = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{farm.pk}",
        {"version": farm.version + 1, "signup_count": 20},
        format="json",
    )

    assert missing.status_code == 400
    assert stale.status_code == 409
    farm.refresh_from_db()
    assert farm.signup_count == 14
    assert farm.version == 1


@pytest.mark.django_db
def test_mutations_revalidate_tracker_farms_after_commit(
    tracker_admin_api, django_capture_on_commit_callbacks
):
    with patch("content.services.request_frontend_revalidation") as revalidate:
        with django_capture_on_commit_callbacks(execute=True):
            created = tracker_admin_api.post(
                "/api/v1/admin/tracker-farms", farm_payload(), format="json"
            ).json()["data"]
        with django_capture_on_commit_callbacks(execute=True):
            updated = tracker_admin_api.patch(
                f"/api/v1/admin/tracker-farms/{created['id']}",
                {"version": created["version"], "signup_count": 22},
                format="json",
            ).json()["data"]
        farm = TrackerFarm.objects.get(pk=created["id"])
        with django_capture_on_commit_callbacks(execute=True):
            tracker_admin_api.patch(
                "/api/v1/admin/tracker-farms/reorder",
                {"items": [reorder_item(farm, 3, version=updated["version"])]},
                format="json",
            )
        with django_capture_on_commit_callbacks(execute=True):
            tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{created['id']}")

    assert revalidate.call_count == 4
    revalidate.assert_called_with(["tracker-farms"])


@pytest.mark.django_db
def test_tracker_farm_migration_creates_no_sample_records():
    migration = importlib.import_module("content.migrations.0017_tracker_farm_content")

    assert not any(
        isinstance(operation, migrations.RunPython) for operation in migration.Migration.operations
    )
    assert TrackerFarm.objects.count() == 0
