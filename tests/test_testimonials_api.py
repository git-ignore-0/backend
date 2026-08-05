import io

import pytest
from django.contrib.auth.models import Group
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from content.models import (
    Testimonial as ContentTestimonial,
)
from content.models import (
    TestimonialStatus as ContentTestimonialStatus,
)
from content.models import (
    TestimonialTranslation as ContentTestimonialTranslation,
)


@pytest.fixture
def testimonial_admin_api(active_user, access_token):
    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def sample_testimonial_payload(
    *,
    display_name="Nguyễn Văn An",
    testimonial_type="farmer",
    sort_order=0,
    featured=False,
    consent_confirmed=True,
):
    return {
        "type": testimonial_type,
        "display_name": display_name,
        "featured": featured,
        "sort_order": sort_order,
        "consent_confirmed": consent_confirmed,
        "translations": [
            {
                "locale": "vi",
                "role": "Nông dân",
                "location": "Đồng Nai",
                "quote": f"Câu chuyện của {display_name}",
                "full_story": "Một câu chuyện đầy đủ bằng tiếng Việt.",
            },
            {
                "locale": "en",
                "role": "Farmer",
                "location": "Dong Nai",
                "quote": f"The story of {display_name}",
                "full_story": "A complete story in English.",
            },
        ],
    }


def create_testimonial(api, **overrides):
    payload = sample_testimonial_payload(**overrides)
    response = api.post("/api/v1/admin/testimonials/", payload, format="json")
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def publish_testimonial(api, testimonial_id):
    return api.post(
        f"/api/v1/admin/testimonials/{testimonial_id}/publish/",
        {"version": ContentTestimonial.objects.get(pk=testimonial_id).version},
        format="json",
    )


def action_testimonial(api, testimonial_id, action, *, version=None):
    if version is None:
        version = ContentTestimonial.objects.get(pk=testimonial_id).version
    return api.post(
        f"/api/v1/admin/testimonials/{testimonial_id}/{action}/",
        {"version": version},
        format="json",
    )


@pytest.mark.django_db
def test_public_only_exposes_published_summary_and_published_detail(testimonial_admin_api):
    published = create_testimonial(testimonial_admin_api, display_name="Published")
    draft = create_testimonial(testimonial_admin_api, display_name="Draft")
    hidden = create_testimonial(testimonial_admin_api, display_name="Hidden")
    assert publish_testimonial(testimonial_admin_api, published["id"]).status_code == 200
    assert publish_testimonial(testimonial_admin_api, hidden["id"]).status_code == 200
    assert action_testimonial(testimonial_admin_api, hidden["id"], "hide").status_code == 200

    response = testimonial_admin_api.get("/api/v1/public/testimonials?locale=vi")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["data"]] == [published["id"]]
    summary = response.json()["data"][0]
    assert "full_story" not in summary
    assert {
        "status",
        "featured",
        "sort_order",
        "consent_confirmed",
        "translations",
        "created_by",
        "updated_by",
        "created_at",
        "updated_at",
        "version",
    }.isdisjoint(summary)

    detail = testimonial_admin_api.get(f"/api/v1/public/testimonials/{published['id']}?locale=vi")
    assert detail.status_code == 200
    assert detail.json()["data"]["full_story"] == "Một câu chuyện đầy đủ bằng tiếng Việt."
    assert (
        testimonial_admin_api.get(
            f"/api/v1/public/testimonials/{draft['id']}?locale=vi"
        ).status_code
        == 404
    )
    assert (
        testimonial_admin_api.get(
            f"/api/v1/public/testimonials/{hidden['id']}?locale=vi"
        ).status_code
        == 404
    )


@pytest.mark.django_db
@pytest.mark.parametrize("case", ["no_consent", "missing_en", "incomplete_vi"])
def test_publish_requires_consent_and_complete_vi_en(testimonial_admin_api, case):
    payload = sample_testimonial_payload()
    if case == "no_consent":
        payload["consent_confirmed"] = False
    elif case == "missing_en":
        payload["translations"] = payload["translations"][:1]
    else:
        payload["translations"][0]["full_story"] = ""
    created = testimonial_admin_api.post("/api/v1/admin/testimonials/", payload, format="json")
    assert created.status_code == 201, created.json()

    response = publish_testimonial(testimonial_admin_api, created.json()["data"]["id"])

    assert response.status_code == 400
    assert ContentTestimonial.objects.get(pk=created.json()["data"]["id"]).status == "draft"


def _create_published_set(api, records):
    for name, order, featured in records:
        item = create_testimonial(
            api,
            display_name=name,
            sort_order=order,
            featured=featured,
        )
        assert publish_testimonial(api, item["id"]).status_code == 200


@pytest.mark.django_db
def test_featured_endpoint_places_one_featured_first_and_fills_three(testimonial_admin_api):
    _create_published_set(
        testimonial_admin_api,
        [("Regular first", 1, False), ("Featured", 20, True), ("Regular last", 30, False)],
    )

    response = testimonial_admin_api.get("/api/v1/public/testimonials/featured?locale=en")

    assert response.status_code == 200
    assert [item["display_name"] for item in response.json()["data"]] == [
        "Featured",
        "Regular first",
        "Regular last",
    ]


@pytest.mark.django_db
def test_featured_endpoint_uses_first_featured_then_public_order_without_duplicates(
    testimonial_admin_api,
):
    _create_published_set(
        testimonial_admin_api,
        [
            ("Regular first", 1, False),
            ("Featured first", 10, True),
            ("Featured second", 20, True),
            ("Regular last", 30, False),
        ],
    )

    data = testimonial_admin_api.get("/api/v1/public/testimonials/featured?locale=vi").json()[
        "data"
    ]

    assert [item["display_name"] for item in data] == [
        "Featured first",
        "Regular first",
        "Featured second",
    ]
    assert len({item["id"] for item in data}) == 3


@pytest.mark.django_db
def test_featured_endpoint_without_featured_returns_first_three_published(testimonial_admin_api):
    _create_published_set(
        testimonial_admin_api,
        [
            ("Third", 30, False),
            ("First", 10, False),
            ("Fourth", 40, False),
            ("Second", 20, False),
        ],
    )

    data = testimonial_admin_api.get("/api/v1/public/testimonials/featured?locale=vi").json()[
        "data"
    ]

    assert [item["display_name"] for item in data] == ["First", "Second", "Third"]


@pytest.mark.django_db
def test_featured_endpoint_without_published_returns_empty(testimonial_admin_api):
    create_testimonial(testimonial_admin_api, display_name="Draft", featured=True)

    response = testimonial_admin_api.get("/api/v1/public/testimonials/featured?locale=vi")

    assert response.status_code == 200
    assert response.json() == {"data": []}


@pytest.mark.django_db
def test_public_locale_uses_vi_then_en_fallback_convention(testimonial_admin_api):
    created = create_testimonial(testimonial_admin_api)
    assert publish_testimonial(testimonial_admin_api, created["id"]).status_code == 200
    ContentTestimonialTranslation.objects.filter(testimonial_id=created["id"], locale="en").update(
        quote=""
    )

    response = testimonial_admin_api.get(f"/api/v1/public/testimonials/{created['id']}?locale=en")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["requested_locale"] == "en"
    assert data["content_locale"] == "vi"
    assert data["available_locales"] == ["vi"]
    assert data["is_fallback"] is True
    assert testimonial_admin_api.get("/api/v1/public/testimonials?locale=fr").status_code == 400


@pytest.mark.django_db
def test_admin_search_filters_and_bounded_pagination(testimonial_admin_api):
    farmer = create_testimonial(
        testimonial_admin_api,
        display_name="Farmer Result",
        testimonial_type="farmer",
    )
    customer = create_testimonial(
        testimonial_admin_api,
        display_name="Customer Result",
        testimonial_type="customer",
    )
    assert publish_testimonial(testimonial_admin_api, customer["id"]).status_code == 200

    searched = testimonial_admin_api.get(
        "/api/v1/admin/testimonials/?search=N%C3%B4ng%20d%C3%A2n&type=farmer&status=draft&page=1&page_size=1"
    )

    assert searched.status_code == 200
    assert [item["id"] for item in searched.json()["data"]] == [farmer["id"]]
    assert searched.json()["data"][0]["version"] == 1
    assert searched.json()["meta"] == {"page": 1, "page_size": 1, "total": 1}
    assert (
        testimonial_admin_api.get("/api/v1/admin/testimonials/?status=invalid").status_code == 400
    )
    assert testimonial_admin_api.get("/api/v1/admin/testimonials/?page=0").status_code == 400
    assert testimonial_admin_api.get("/api/v1/public/testimonials?page_size=51").status_code == 400


@pytest.mark.django_db
def test_admin_testimonial_requires_scope_and_group(access_token, active_user):
    anonymous = APIClient()
    assert anonymous.get("/api/v1/admin/testimonials/").status_code == 401

    bearer = APIClient()
    bearer.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    assert bearer.get("/api/v1/admin/testimonials/").status_code == 403

    group, _created = Group.objects.get_or_create(name="website_admin")
    active_user.groups.add(group)
    assert bearer.get("/api/v1/admin/testimonials/").status_code == 403

    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    assert bearer.get("/api/v1/admin/testimonials/").status_code == 200


@pytest.mark.django_db
def test_create_update_and_actions_reject_invalid_or_unsafe_changes(testimonial_admin_api):
    payload = sample_testimonial_payload()
    payload["status"] = "published"
    assert (
        testimonial_admin_api.post(
            "/api/v1/admin/testimonials/", payload, format="json"
        ).status_code
        == 400
    )

    duplicate = sample_testimonial_payload()
    duplicate["translations"].append({**duplicate["translations"][0]})
    assert (
        testimonial_admin_api.post(
            "/api/v1/admin/testimonials/", duplicate, format="json"
        ).status_code
        == 400
    )

    invalid_image = sample_testimonial_payload()
    invalid_image["image_id"] = "4bedc2fd-c5b4-41d8-a28b-ef07903992f8"
    assert (
        testimonial_admin_api.post(
            "/api/v1/admin/testimonials/", invalid_image, format="json"
        ).status_code
        == 400
    )

    created = create_testimonial(testimonial_admin_api)
    assert publish_testimonial(testimonial_admin_api, created["id"]).status_code == 200
    assert publish_testimonial(testimonial_admin_api, created["id"]).status_code == 409
    assert (
        testimonial_admin_api.patch(
            f"/api/v1/admin/testimonials/{created['id']}/",
            {
                "consent_confirmed": False,
                "version": ContentTestimonial.objects.get(pk=created["id"]).version,
            },
            format="json",
        ).status_code
        == 400
    )
    assert (
        testimonial_admin_api.post(
            f"/api/v1/admin/testimonials/{created['id']}/hide/",
            {
                "unexpected": True,
                "version": ContentTestimonial.objects.get(pk=created["id"]).version,
            },
            format="json",
        ).status_code
        == 400
    )
    assert (
        testimonial_admin_api.delete(f"/api/v1/admin/testimonials/{created['id']}/").status_code
        == 405
    )

    hidden = action_testimonial(testimonial_admin_api, created["id"], "hide")
    assert hidden.status_code == 200
    assert hidden.json()["data"]["status"] == ContentTestimonialStatus.HIDDEN
    assert action_testimonial(testimonial_admin_api, created["id"], "hide").status_code == 409
    draft = action_testimonial(testimonial_admin_api, created["id"], "unpublish")
    assert draft.status_code == 200
    assert draft.json()["data"]["status"] == ContentTestimonialStatus.DRAFT
    assert action_testimonial(testimonial_admin_api, created["id"], "unpublish").status_code == 409


@pytest.mark.django_db
def test_testimonial_media_uses_existing_validated_image_pipeline(testimonial_admin_api):
    output = io.BytesIO()
    Image.new("RGB", (640, 480), "#355b45").save(output, format="PNG")
    upload = SimpleUploadedFile(
        "portrait.png",
        output.getvalue(),
        content_type="image/png",
    )

    uploaded = testimonial_admin_api.post(
        "/api/v1/admin/testimonials/media/",
        {"image": upload},
        format="multipart",
    )

    assert uploaded.status_code == 201, uploaded.json()
    payload = sample_testimonial_payload()
    payload["image_id"] = uploaded.json()["data"]["id"]
    created = testimonial_admin_api.post("/api/v1/admin/testimonials/", payload, format="json")
    assert created.status_code == 201, created.json()
    assert created.json()["data"]["image"]["id"] == uploaded.json()["data"]["id"]


@pytest.mark.django_db
def test_public_testimonial_list_query_count_is_bounded(
    testimonial_admin_api, django_assert_num_queries
):
    for index in range(4):
        created = create_testimonial(
            testimonial_admin_api,
            display_name=f"Published {index}",
            sort_order=index,
        )
        assert publish_testimonial(testimonial_admin_api, created["id"]).status_code == 200

    public = APIClient()
    with django_assert_num_queries(3):
        response = public.get("/api/v1/public/testimonials?locale=vi")

    assert response.status_code == 200
    assert len(response.json()["data"]) == 4


@pytest.mark.django_db
def test_full_story_rejects_more_than_twenty_thousand_characters(testimonial_admin_api):
    payload = sample_testimonial_payload()
    payload["translations"][0]["full_story"] = "x" * 20_001

    response = testimonial_admin_api.post("/api/v1/admin/testimonials/", payload, format="json")

    assert response.status_code == 400
    assert ContentTestimonial.objects.count() == 0


@pytest.mark.django_db
def test_patch_requires_version_and_rejects_stale_write_without_changes(testimonial_admin_api):
    created = create_testimonial(testimonial_admin_api)
    testimonial_id = created["id"]
    assert created["version"] == 1

    missing = testimonial_admin_api.patch(
        f"/api/v1/admin/testimonials/{testimonial_id}/",
        {"display_name": "Missing version"},
        format="json",
    )
    stale = testimonial_admin_api.patch(
        f"/api/v1/admin/testimonials/{testimonial_id}/",
        {"display_name": "Stale write", "version": 2},
        format="json",
    )

    testimonial = ContentTestimonial.objects.get(pk=testimonial_id)
    assert missing.status_code == 400
    assert stale.status_code == 409
    assert stale.json()["errors"][0]["code"] == "version_conflict"
    assert testimonial.display_name == created["display_name"]
    assert testimonial.version == 1


@pytest.mark.django_db
def test_two_sequential_patches_with_same_version_only_apply_first(testimonial_admin_api):
    created = create_testimonial(testimonial_admin_api)
    url = f"/api/v1/admin/testimonials/{created['id']}/"

    first = testimonial_admin_api.patch(
        url,
        {"display_name": "First admin", "version": created["version"]},
        format="json",
    )
    second = testimonial_admin_api.patch(
        url,
        {"display_name": "Second admin", "version": created["version"]},
        format="json",
    )

    assert first.status_code == 200
    assert first.json()["data"]["version"] == 2
    assert second.status_code == 409
    testimonial = ContentTestimonial.objects.get(pk=created["id"])
    assert testimonial.display_name == "First admin"
    assert testimonial.version == 2


@pytest.mark.django_db
@pytest.mark.parametrize("action", ["publish", "hide", "unpublish"])
def test_actions_reject_stale_version_without_state_change(testimonial_admin_api, action):
    created = create_testimonial(testimonial_admin_api)
    if action == "unpublish":
        published = publish_testimonial(testimonial_admin_api, created["id"])
        assert published.status_code == 200
    before = ContentTestimonial.objects.get(pk=created["id"])
    before_status = before.status
    before_version = before.version

    response = action_testimonial(
        testimonial_admin_api,
        created["id"],
        action,
        version=before_version + 1,
    )

    after = ContentTestimonial.objects.get(pk=created["id"])
    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "version_conflict"
    assert after.status == before_status
    assert after.version == before_version


@pytest.mark.django_db
def test_each_action_increments_version_and_old_version_cannot_mutate_again(testimonial_admin_api):
    created = create_testimonial(testimonial_admin_api)
    initial_version = created["version"]

    published = action_testimonial(
        testimonial_admin_api, created["id"], "publish", version=initial_version
    )
    stale_hide = action_testimonial(
        testimonial_admin_api, created["id"], "hide", version=initial_version
    )
    hidden = action_testimonial(
        testimonial_admin_api,
        created["id"],
        "hide",
        version=published.json()["data"]["version"],
    )
    draft = action_testimonial(
        testimonial_admin_api,
        created["id"],
        "unpublish",
        version=hidden.json()["data"]["version"],
    )

    assert published.status_code == 200
    assert published.json()["data"]["version"] == initial_version + 1
    assert stale_hide.status_code == 409
    assert hidden.status_code == 200
    assert hidden.json()["data"]["version"] == initial_version + 2
    assert draft.status_code == 200
    assert draft.json()["data"]["version"] == initial_version + 3
