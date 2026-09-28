import importlib
import io
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from PIL import Image
from rest_framework.test import APIClient

from content import models as content_models
from content.models import (
    TRACKER_SIGNUP_COUNT_MAX,
    ContentAuditLog,
    MediaAsset,
    TrackerFarm,
    TrackerFarmPhoto,
)
from content.serializers import TrackerFarmUpdateSerializer, TrackerFarmWriteSerializer
from referrals.models import Reward


def farm_payload(**overrides):
    payload = {
        "name_en": "Green Valley Farm",
        "name_vi": "Nông trại Thung Lũng Xanh",
        "location_en": "Da Lat, Vietnam",
        "location_vi": "Đà Lạt, Việt Nam",
        "signup_count": 14,
        "one_month_signup_count": 4,
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


def create_stored_media_asset(prefix, storage_prefix="farms"):
    original_file = default_storage.save(
        f"{storage_prefix}/{prefix}-original.webp",
        ContentFile(b"original"),
    )
    variant_file = default_storage.save(
        f"{storage_prefix}/{prefix}-480.webp",
        ContentFile(b"variant"),
    )
    asset = create_media_asset(
        file=original_file,
        variants=[{"file": variant_file, "width": 480, "height": 360}],
    )
    return asset, {original_file, variant_file}


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
    assert farm.one_month_signup_count == 0
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
        TrackerFarm.objects.filter(pk=farm.pk).update(one_month_signup_count=-1)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrackerFarm.objects.filter(pk=farm.pk).update(sort_order=-1)


@pytest.mark.django_db
def test_tracker_farm_photo_database_constraints():
    farm = create_farm()
    first = create_media_asset()
    second = create_media_asset()
    TrackerFarmPhoto.objects.create(farm=farm, image=first, sort_order=0)

    with pytest.raises(IntegrityError), transaction.atomic():
        TrackerFarmPhoto.objects.create(farm=farm, image=second, sort_order=0)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrackerFarmPhoto.objects.create(farm=farm, image=first, sort_order=1)
    with pytest.raises(IntegrityError), transaction.atomic():
        TrackerFarmPhoto.objects.create(farm=farm, image=second, sort_order=-1)


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
    assert [item["id"] for item in first_data["photos"]] == [str(image.pk)]
    assert first_data["signup_count"] == 14
    assert first_data["one_month_signup_count"] == 4
    assert first_data["sort_order"] == 0
    assert second_data["sort_order"] == 1

    updated = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{first_data['id']}",
        {
            "version": first_data["version"],
            "signup_count": 27,
            "one_month_signup_count": 8,
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
    assert updated.json()["data"]["one_month_signup_count"] == 8
    assert updated.json()["data"]["is_visible"] is False
    assert updated.json()["data"]["name_vi"] == "Nông trại đầu tiên"
    assert updated.json()["data"]["description_en"] == ""
    assert updated.json()["data"]["description_vi"] == "Mô tả mới"
    assert updated.json()["data"]["image"] is None
    assert updated.json()["data"]["photos"] == []
    assert updated.json()["data"]["version"] == first_data["version"] + 1

    deleted = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{first_data['id']}")
    assert deleted.status_code == 204
    assert not TrackerFarm.objects.filter(pk=first_data["id"]).exists()
    assert TrackerFarm.objects.filter(pk=second_data["id"]).exists()


@pytest.mark.django_db
def test_farm_delete_cleans_exclusive_gallery_assets_and_files(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    asset, file_names = create_stored_media_asset("exclusive")
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(asset.pk)]),
        format="json",
    ).json()["data"]
    assert all(default_storage.exists(file_name) for file_name in file_names)

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{created['id']}")

    assert response.status_code == 204
    assert not TrackerFarm.objects.filter(pk=created["id"]).exists()
    assert not TrackerFarmPhoto.objects.filter(farm_id=created["id"]).exists()
    assert not MediaAsset.objects.filter(pk=asset.pk).exists()
    assert all(not default_storage.exists(file_name) for file_name in file_names)
    assert ContentAuditLog.objects.filter(
        action="tracker_farm.delete",
        entity_id=created["id"],
    ).exists()
    assert ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(asset.pk),
        context__farm_id=created["id"],
        context__reason="farm_delete",
    ).exists()


@pytest.mark.django_db
def test_farm_delete_preserves_shared_gallery_asset_until_last_reference_is_removed(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    asset, file_names = create_stored_media_asset("shared")
    first = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(name_en="First", photo_ids=[str(asset.pk)]),
        format="json",
    ).json()["data"]
    second = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(name_en="Second", photo_ids=[str(asset.pk)]),
        format="json",
    ).json()["data"]

    with django_capture_on_commit_callbacks(execute=True):
        first_delete = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{first['id']}")

    assert first_delete.status_code == 204
    assert MediaAsset.objects.filter(pk=asset.pk).exists()
    assert all(default_storage.exists(file_name) for file_name in file_names)
    assert not ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(asset.pk),
    ).exists()

    with django_capture_on_commit_callbacks(execute=True):
        second_delete = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{second['id']}")

    assert second_delete.status_code == 204
    assert not MediaAsset.objects.filter(pk=asset.pk).exists()
    assert all(not default_storage.exists(file_name) for file_name in file_names)
    assert (
        ContentAuditLog.objects.filter(
            action="tracker_farm.media.orphan_cleanup",
            entity_id=str(asset.pk),
        ).count()
        == 1
    )


@pytest.mark.django_db
def test_farm_delete_preserves_assets_referenced_by_testimonials_and_rewards(
    tracker_admin_api,
    django_capture_on_commit_callbacks,
):
    testimonial_asset = create_media_asset(original_name="testimonial.webp")
    reward_asset = create_media_asset(original_name="reward.webp")
    content_models.Testimonial.objects.create(
        type=content_models.TestimonialType.FARMER,
        status=content_models.TestimonialStatus.DRAFT,
        display_name="Shared farmer",
        image=testimonial_asset,
    )
    Reward.objects.create(point_cost=10, image=reward_asset)
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(testimonial_asset.pk), str(reward_asset.pk)]),
        format="json",
    ).json()["data"]

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/{created['id']}")

    assert response.status_code == 204
    assert MediaAsset.objects.filter(pk=testimonial_asset.pk).exists()
    assert MediaAsset.objects.filter(pk=reward_asset.pk).exists()
    assert not ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id__in=[str(testimonial_asset.pk), str(reward_asset.pk)],
    ).exists()


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
@pytest.mark.parametrize("one_month_signup_count", [-1, "not-an-integer", 1.5])
def test_one_month_signup_count_rejects_negative_and_invalid_values(
    tracker_admin_api, one_month_signup_count
):
    response = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(one_month_signup_count=one_month_signup_count),
        format="json",
    )

    assert response.status_code == 400
    assert TrackerFarm.objects.count() == 0


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["signup_count", "one_month_signup_count"])
@pytest.mark.parametrize("value", [0, TRACKER_SIGNUP_COUNT_MAX])
def test_signup_counts_accept_mysql_unsigned_integer_boundaries(tracker_admin_api, field, value):
    response = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(**{field: value}),
        format="json",
    )

    assert response.status_code == 201, response.json()
    assert response.json()["data"][field] == value


@pytest.mark.django_db
@pytest.mark.parametrize("field", ["signup_count", "one_month_signup_count"])
def test_signup_counts_reject_values_above_mysql_unsigned_integer_limit(tracker_admin_api, field):
    response = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(**{field: TRACKER_SIGNUP_COUNT_MAX + 1}),
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
        one_month_signup_count=4,
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
                "variants": [],
            },
            "images": [
                {
                    "id": str(image.pk),
                    "url": f"http://testserver/media/{image.file.name}",
                    "width": 1200,
                    "height": 900,
                    "alt": "Green Valley Farm",
                    "variants": [],
                }
            ],
            "signup_count": 14,
            "one_month_signup_count": 4,
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
            "variants": [],
        },
        "images": [
            {
                "id": str(image.pk),
                "url": f"http://testserver/media/{image.file.name}",
                "width": 1200,
                "height": 900,
                "alt": "Nông trại Thung Lũng Xanh",
                "variants": [],
            }
        ],
        "signup_count": 14,
        "one_month_signup_count": 4,
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
    assert created.json()["data"]["photos"] == []
    assert created.json()["data"]["description_en"] == ""
    assert created.json()["data"]["description_vi"] == ""

    public = client.get("/api/v1/public/tracker-farms?locale=en")
    assert public.json()["data"][0]["image"] is None
    assert public.json()["data"][0]["images"] == []
    assert public.json()["data"][0]["description"] == ""


@pytest.mark.django_db
def test_admin_and_public_gallery_payloads_are_ordered_localized_and_query_bounded(
    client,
    tracker_admin_api,
    django_assert_num_queries,
):
    first = create_media_asset(
        original_name="first.webp",
        variants=[{"file": "farms/first-480.webp", "width": 480, "height": 360}],
    )
    second = create_media_asset(original_name="second.webp")
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(
            description_en="English description",
            description_vi="Mô tả tiếng Việt",
            photo_ids=[str(second.pk), str(first.pk)],
        ),
        format="json",
    )
    assert created.status_code == 201, created.json()

    admin = tracker_admin_api.get("/api/v1/admin/tracker-farms")
    assert admin.status_code == 200
    assert [item["id"] for item in admin.json()["data"][0]["photos"]] == [
        str(second.pk),
        str(first.pk),
    ]
    assert admin.json()["data"][0]["photos"][1]["variants"] == [
        {
            "file": "farms/first-480.webp",
            "url": "http://testserver/media/farms/first-480.webp",
            "width": 480,
            "height": 360,
        }
    ]

    with django_assert_num_queries(2):
        english = client.get("/api/v1/public/tracker-farms?locale=en")
    vietnamese = client.get("/api/v1/public/tracker-farms?locale=vi")

    assert english.status_code == 200
    assert vietnamese.status_code == 200
    english_data = english.json()["data"][0]
    vietnamese_data = vietnamese.json()["data"][0]
    assert [item["id"] for item in english_data["images"]] == [
        str(second.pk),
        str(first.pk),
    ]
    assert english_data["image"] == english_data["images"][0]
    assert english_data["images"][0]["alt"] == "Green Valley Farm"
    assert english_data["images"][1]["variants"] == [
        {
            "url": "http://testserver/media/farms/first-480.webp",
            "width": 480,
            "height": 360,
        }
    ]
    assert vietnamese_data["image"] == vietnamese_data["images"][0]
    assert [item["alt"] for item in vietnamese_data["images"]] == [
        "Nông trại Thung Lũng Xanh",
        "Nông trại Thung Lũng Xanh",
    ]
    assert vietnamese_data["description"] == "Mô tả tiếng Việt"


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
def test_photo_ids_race_returns_field_error_without_partial_create(tracker_admin_api):
    asset = create_media_asset()
    original_is_valid = TrackerFarmWriteSerializer.is_valid

    def validate_then_remove_asset(serializer, *args, **kwargs):
        result = original_is_valid(serializer, *args, **kwargs)
        MediaAsset.objects.filter(pk=asset.pk).delete()
        return result

    with patch.object(TrackerFarmWriteSerializer, "is_valid", validate_then_remove_asset):
        response = tracker_admin_api.post(
            "/api/v1/admin/tracker-farms",
            farm_payload(photo_ids=[str(asset.pk)]),
            format="json",
        )

    assert response.status_code == 400
    assert response.json() == {"photo_ids": [f"Media assets do not exist: {asset.pk}."]}
    assert TrackerFarm.objects.count() == 0
    assert TrackerFarmPhoto.objects.count() == 0


@pytest.mark.django_db
def test_legacy_image_id_race_returns_field_error_without_partial_update(tracker_admin_api):
    farm = create_farm(name_en="Original name")
    asset = create_media_asset()
    original_is_valid = TrackerFarmUpdateSerializer.is_valid

    def validate_then_remove_asset(serializer, *args, **kwargs):
        result = original_is_valid(serializer, *args, **kwargs)
        MediaAsset.objects.filter(pk=asset.pk).delete()
        return result

    with patch.object(TrackerFarmUpdateSerializer, "is_valid", validate_then_remove_asset):
        response = tracker_admin_api.patch(
            f"/api/v1/admin/tracker-farms/{farm.pk}",
            {
                "version": farm.version,
                "name_en": "Should roll back",
                "image_id": str(asset.pk),
            },
            format="json",
        )

    assert response.status_code == 400
    assert response.json() == {"image_id": ["Media asset does not exist."]}
    farm.refresh_from_db()
    assert farm.name_en == "Original name"
    assert farm.version == 1
    assert farm.image_id is None
    assert not farm.photos.exists()


@pytest.mark.django_db
def test_admin_create_and_update_ordered_tracker_farm_gallery(tracker_admin_api):
    first = create_media_asset(original_name="first.webp")
    second = create_media_asset(original_name="second.webp")
    third = create_media_asset(original_name="third.webp")

    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(second.pk), str(first.pk), str(third.pk)]),
        format="json",
    )

    assert created.status_code == 201, created.json()
    created_data = created.json()["data"]
    assert [item["id"] for item in created_data["photos"]] == [
        str(second.pk),
        str(first.pk),
        str(third.pk),
    ]
    assert created_data["image"]["id"] == str(second.pk)
    assert created_data["version"] == 1
    farm = TrackerFarm.objects.get(pk=created_data["id"])
    assert list(farm.photos.values_list("image_id", "sort_order")) == [
        (second.pk, 0),
        (first.pk, 1),
        (third.pk, 2),
    ]

    updated = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{farm.pk}",
        {
            "version": created_data["version"],
            "photo_ids": [str(third.pk), str(first.pk)],
        },
        format="json",
    )

    assert updated.status_code == 200, updated.json()
    updated_data = updated.json()["data"]
    assert [item["id"] for item in updated_data["photos"]] == [
        str(third.pk),
        str(first.pk),
    ]
    assert updated_data["image"]["id"] == str(third.pk)
    assert updated_data["version"] == 2
    farm.refresh_from_db()
    assert farm.image_id == third.pk
    assert list(farm.photos.values_list("image_id", "sort_order")) == [
        (third.pk, 0),
        (first.pk, 1),
    ]

    cleared = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{farm.pk}",
        {"version": updated_data["version"], "photo_ids": []},
        format="json",
    )
    assert cleared.status_code == 200, cleared.json()
    assert cleared.json()["data"]["photos"] == []
    assert cleared.json()["data"]["image"] is None
    assert cleared.json()["data"]["version"] == 3


@pytest.mark.django_db
def test_gallery_update_cleans_removed_exclusive_asset_and_files(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    removed_asset, removed_files = create_stored_media_asset("gallery-removed")
    retained_asset, retained_files = create_stored_media_asset("gallery-retained")
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(removed_asset.pk), str(retained_asset.pk)]),
        format="json",
    ).json()["data"]

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.patch(
            f"/api/v1/admin/tracker-farms/{created['id']}",
            {
                "version": created["version"],
                "photo_ids": [str(retained_asset.pk)],
            },
            format="json",
        )

    assert response.status_code == 200, response.json()
    assert not MediaAsset.objects.filter(pk=removed_asset.pk).exists()
    assert all(not default_storage.exists(file_name) for file_name in removed_files)
    assert MediaAsset.objects.filter(pk=retained_asset.pk).exists()
    assert all(default_storage.exists(file_name) for file_name in retained_files)
    assert ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(removed_asset.pk),
        context__farm_id=created["id"],
        context__reason="gallery_update",
    ).exists()


@pytest.mark.django_db
def test_legacy_gallery_replacement_cleans_old_exclusive_asset(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    old_asset, old_files = create_stored_media_asset("legacy-old")
    new_asset, new_files = create_stored_media_asset("legacy-new")
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(image_id=str(old_asset.pk)),
        format="json",
    ).json()["data"]

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.patch(
            f"/api/v1/admin/tracker-farms/{created['id']}",
            {"version": created["version"], "image_id": str(new_asset.pk)},
            format="json",
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["image"]["id"] == str(new_asset.pk)
    assert not MediaAsset.objects.filter(pk=old_asset.pk).exists()
    assert all(not default_storage.exists(file_name) for file_name in old_files)
    assert MediaAsset.objects.filter(pk=new_asset.pk).exists()
    assert all(default_storage.exists(file_name) for file_name in new_files)
    assert ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(old_asset.pk),
        context__reason="gallery_update",
    ).exists()


@pytest.mark.django_db
def test_gallery_update_retains_asset_shared_with_another_farm(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    shared_asset, shared_files = create_stored_media_asset("gallery-shared")
    replacement = create_media_asset(original_name="replacement.webp")
    first = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(name_en="First", photo_ids=[str(shared_asset.pk)]),
        format="json",
    ).json()["data"]
    second = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(name_en="Second", photo_ids=[str(shared_asset.pk)]),
        format="json",
    ).json()["data"]

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.patch(
            f"/api/v1/admin/tracker-farms/{first['id']}",
            {"version": first["version"], "photo_ids": [str(replacement.pk)]},
            format="json",
        )

    assert response.status_code == 200, response.json()
    assert MediaAsset.objects.filter(pk=shared_asset.pk).exists()
    assert all(default_storage.exists(file_name) for file_name in shared_files)
    assert TrackerFarmPhoto.objects.filter(farm_id=second["id"], image_id=shared_asset.pk).exists()
    assert not ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(shared_asset.pk),
    ).exists()


@pytest.mark.django_db
def test_unchanged_photo_ids_do_not_replace_relations_or_run_cleanup(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    first, first_files = create_stored_media_asset("unchanged-first")
    second, second_files = create_stored_media_asset("unchanged-second")
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(first.pk), str(second.pk)]),
        format="json",
    ).json()["data"]
    relation_ids = list(
        TrackerFarmPhoto.objects.filter(farm_id=created["id"])
        .order_by("sort_order")
        .values_list("pk", flat=True)
    )

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.patch(
            f"/api/v1/admin/tracker-farms/{created['id']}",
            {
                "version": created["version"],
                "photo_ids": [str(first.pk), str(second.pk)],
            },
            format="json",
        )

    assert response.status_code == 200, response.json()
    assert (
        list(
            TrackerFarmPhoto.objects.filter(farm_id=created["id"])
            .order_by("sort_order")
            .values_list("pk", flat=True)
        )
        == relation_ids
    )
    assert MediaAsset.objects.filter(pk__in=[first.pk, second.pk]).count() == 2
    assert all(default_storage.exists(file_name) for file_name in first_files | second_files)
    assert not ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        context__farm_id=created["id"],
        context__reason="gallery_update",
    ).exists()


@pytest.mark.django_db
def test_tracker_farm_gallery_rejects_limit_duplicates_unknown_and_legacy_mix(
    tracker_admin_api,
):
    assets = [create_media_asset() for _index in range(13)]
    unknown_id = uuid4()

    too_many = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(asset.pk) for asset in assets]),
        format="json",
    )
    duplicate = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(assets[0].pk), str(assets[0].pk)]),
        format="json",
    )
    unknown = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(assets[0].pk), str(unknown_id)]),
        format="json",
    )
    mixed = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(image_id=str(assets[0].pk), photo_ids=[str(assets[1].pk)]),
        format="json",
    )

    assert too_many.status_code == 400
    assert "photo_ids" in too_many.json()
    assert duplicate.status_code == 400
    assert "unique" in str(duplicate.json()["photo_ids"]).lower()
    assert unknown.status_code == 400
    assert str(unknown_id) in str(unknown.json()["photo_ids"])
    assert mixed.status_code == 400
    assert "photo_ids" in mixed.json()
    assert TrackerFarm.objects.count() == 0


@pytest.mark.django_db
def test_legacy_image_id_create_update_and_null_gallery_compatibility(tracker_admin_api):
    first = create_media_asset(original_name="first.webp")
    second = create_media_asset(original_name="second.webp")
    third = create_media_asset(original_name="third.webp")
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(image_id=str(first.pk)),
        format="json",
    )

    assert created.status_code == 201, created.json()
    data = created.json()["data"]
    assert [item["id"] for item in data["photos"]] == [str(first.pk)]

    gallery = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{data['id']}",
        {
            "version": data["version"],
            "photo_ids": [str(first.pk), str(second.pk), str(third.pk)],
        },
        format="json",
    ).json()["data"]
    unchanged_legacy = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{data['id']}",
        {"version": gallery["version"], "image_id": str(first.pk)},
        format="json",
    )

    assert unchanged_legacy.status_code == 200, unchanged_legacy.json()
    unchanged_data = unchanged_legacy.json()["data"]
    assert [item["id"] for item in unchanged_data["photos"]] == [
        str(first.pk),
        str(second.pk),
        str(third.pk),
    ]

    changed_legacy = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{data['id']}",
        {"version": unchanged_data["version"], "image_id": str(second.pk)},
        format="json",
    )
    assert changed_legacy.status_code == 200, changed_legacy.json()
    changed_data = changed_legacy.json()["data"]
    assert [item["id"] for item in changed_data["photos"]] == [str(second.pk)]
    assert changed_data["image"]["id"] == str(second.pk)

    removed = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{data['id']}",
        {"version": changed_data["version"], "image_id": None},
        format="json",
    )
    assert removed.status_code == 200, removed.json()
    assert removed.json()["data"]["photos"] == []
    assert removed.json()["data"]["image"] is None


@pytest.mark.django_db
def test_gallery_stale_version_conflict_is_atomic(tracker_admin_api):
    first = create_media_asset()
    second = create_media_asset()
    created = tracker_admin_api.post(
        "/api/v1/admin/tracker-farms",
        farm_payload(photo_ids=[str(first.pk)]),
        format="json",
    ).json()["data"]

    response = tracker_admin_api.patch(
        f"/api/v1/admin/tracker-farms/{created['id']}",
        {"version": created["version"] + 1, "photo_ids": [str(second.pk)]},
        format="json",
    )

    assert response.status_code == 409
    farm = TrackerFarm.objects.get(pk=created["id"])
    assert farm.version == created["version"]
    assert farm.image_id == first.pk
    assert list(farm.photos.values_list("image_id", flat=True)) == [first.pk]


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
def test_temporary_tracker_farm_media_cleanup_deletes_unreferenced_asset_and_files(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    asset, file_names = create_stored_media_asset("temporary-cleanup")

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/media/{asset.pk}")

    assert response.status_code == 204
    assert not MediaAsset.objects.filter(pk=asset.pk).exists()
    assert all(not default_storage.exists(file_name) for file_name in file_names)
    assert ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(asset.pk),
        context__reason="temporary_upload_cancel",
    ).exists()


@pytest.mark.django_db
def test_temporary_tracker_farm_media_cleanup_retains_referenced_asset(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    asset, file_names = create_stored_media_asset("temporary-referenced")
    farm = create_farm(image=asset)
    TrackerFarmPhoto.objects.create(farm=farm, image=asset, sort_order=0)

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/media/{asset.pk}")

    assert response.status_code == 409
    assert response.json() == {"errors": [{"code": "media_asset_referenced"}]}
    assert MediaAsset.objects.filter(pk=asset.pk).exists()
    assert all(default_storage.exists(file_name) for file_name in file_names)
    assert not ContentAuditLog.objects.filter(
        action="tracker_farm.media.orphan_cleanup",
        entity_id=str(asset.pk),
        context__reason="temporary_upload_cancel",
    ).exists()


@pytest.mark.django_db
def test_temporary_tracker_farm_media_cleanup_rejects_non_farm_asset(
    tracker_admin_api,
    settings,
    tmp_path,
    django_capture_on_commit_callbacks,
):
    settings.MEDIA_ROOT = tmp_path
    asset, file_names = create_stored_media_asset("not-a-farm", "rewards")

    with django_capture_on_commit_callbacks(execute=True):
        response = tracker_admin_api.delete(f"/api/v1/admin/tracker-farms/media/{asset.pk}")

    assert response.status_code == 400
    assert "media" in response.json()
    assert MediaAsset.objects.filter(pk=asset.pk).exists()
    assert all(default_storage.exists(file_name) for file_name in file_names)


@pytest.mark.django_db
def test_temporary_tracker_farm_media_cleanup_requires_website_admin(access_token):
    bearer = APIClient()
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    asset = create_media_asset()

    response = bearer.delete(f"/api/v1/admin/tracker-farms/media/{asset.pk}")

    assert response.status_code == 403
    assert MediaAsset.objects.filter(pk=asset.pk).exists()


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
def test_tracker_farm_photo_migration_backfills_legacy_image_at_zero_when_empty():
    migration = importlib.import_module("content.migrations.0018_tracker_farm_photos")
    image = create_media_asset()
    farm = create_farm(image=image)

    migration.backfill_tracker_farm_photos(django_apps, None)
    migration.backfill_tracker_farm_photos(django_apps, None)

    assert list(farm.photos.values_list("image_id", "sort_order")) == [(image.pk, 0)]


@pytest.mark.django_db
def test_tracker_farm_photo_migration_ignores_an_existing_legacy_image_row():
    migration = importlib.import_module("content.migrations.0018_tracker_farm_photos")
    image = create_media_asset()
    farm = create_farm(image=image)
    existing = TrackerFarmPhoto.objects.create(farm=farm, image=image, sort_order=4)

    migration.backfill_tracker_farm_photos(django_apps, None)

    assert list(farm.photos.values_list("id", "image_id", "sort_order")) == [
        (existing.pk, image.pk, 4)
    ]


@pytest.mark.django_db
def test_tracker_farm_photo_migration_uses_next_free_order_for_legacy_image():
    migration = importlib.import_module("content.migrations.0018_tracker_farm_photos")
    legacy_image = create_media_asset()
    first_existing_image = create_media_asset()
    second_existing_image = create_media_asset()
    farm = create_farm(image=legacy_image)
    TrackerFarmPhoto.objects.create(farm=farm, image=first_existing_image, sort_order=0)
    TrackerFarmPhoto.objects.create(farm=farm, image=second_existing_image, sort_order=2)

    migration.backfill_tracker_farm_photos(django_apps, None)
    migration.backfill_tracker_farm_photos(django_apps, None)

    assert list(farm.photos.values_list("image_id", "sort_order")) == [
        (first_existing_image.pk, 0),
        (legacy_image.pk, 1),
        (second_existing_image.pk, 2),
    ]


@pytest.mark.django_db
def test_tracker_farm_photo_migration_creates_no_sample_records():
    migration = importlib.import_module("content.migrations.0018_tracker_farm_photos")

    migration.backfill_tracker_farm_photos(django_apps, None)

    assert TrackerFarm.objects.count() == 0
    assert TrackerFarmPhoto.objects.count() == 0
