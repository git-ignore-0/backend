import pytest
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from memberships.models import CSATermsDocument

BASE = "/api/v1/admin/csa-terms"


@pytest.fixture
def terms_admin_client(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def _payload(version="CSA_TERMS_V3"):
    return {
        "version": version,
        "title_vi": "Điều khoản V3",
        "title_en": "Terms V3",
        "clauses": [
            {
                "key": "delivery",
                "sort_order": 10,
                "heading_vi": "Giao hàng",
                "heading_en": "Delivery",
                "paragraphs_vi": ["Nội dung giao hàng."],
                "paragraphs_en": ["Delivery details."],
            }
        ],
    }


@pytest.mark.django_db
def test_admin_can_create_edit_clone_publish_and_delete_drafts(terms_admin_client):
    created = terms_admin_client.post(BASE, _payload(), format="json")
    assert created.status_code == 201, created.json()
    draft = created.json()["data"]
    assert draft["status"] == "draft"
    assert draft["revision"] == 1

    detail_url = f"{BASE}/{draft['id']}"
    changed = terms_admin_client.patch(
        detail_url, {"title_vi": "Điều khoản V3 sửa", "revision": 1}, format="json"
    )
    assert changed.status_code == 200, changed.json()
    assert changed.json()["data"]["revision"] == 2

    cloned = terms_admin_client.post(
        f"{detail_url}/clone", {"version": "CSA_TERMS_V4"}, format="json"
    )
    assert cloned.status_code == 201, cloned.json()
    clone = cloned.json()["data"]
    assert clone["version"] == "CSA_TERMS_V4"
    assert clone["clauses"][0]["key"] == "delivery"
    assert terms_admin_client.delete(f"{BASE}/{clone['id']}").status_code == 204

    published = terms_admin_client.post(f"{detail_url}/publish", {"revision": 2}, format="json")
    assert published.status_code == 200, published.json()
    assert published.json()["data"]["status"] == "published"
    immutable = terms_admin_client.patch(detail_url, {"title_vi": "Không được"}, format="json")
    assert immutable.json() == {"errors": [{"code": "csa_terms_immutable"}]}
    assert terms_admin_client.delete(detail_url).status_code == 409


@pytest.mark.django_db
def test_admin_terms_permissions_and_revision_conflicts(
    terms_admin_client, active_user, access_token
):
    assert APIClient().get(BASE).status_code == 401
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    non_admin = APIClient()
    non_admin.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    active_user.groups.clear()
    assert non_admin.get(BASE).status_code == 403

    active_user.groups.add(Group.objects.get(name="website_admin"))
    draft = terms_admin_client.post(BASE, _payload(), format="json").json()["data"]
    response = terms_admin_client.patch(
        f"{BASE}/{draft['id']}", {"title_en": "Changed", "revision": 99}, format="json"
    )
    assert response.status_code == 409
    assert response.json() == {"errors": [{"code": "csa_terms_revision_conflict"}]}


@pytest.mark.django_db
def test_public_current_terms_returns_published_content_and_hash():
    response = APIClient().get("/api/v1/public/csa-terms?locale=vi")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["version"] == "CSA_TERMS_V2"
    assert len(data["hash"]) == 64
    assert data["content"]["title"]
    assert data["content"]["sections"]
    assert CSATermsDocument.objects.get(version=data["version"]).status == "published"
