from copy import deepcopy

import pytest
from rest_framework.test import APIClient

from commerce.models import Product
from tests.test_commerce_catalog_api import admin_api as admin_api
from tests.test_commerce_catalog_api import category_payload, product_payload


def document(text="Rau tươi"):
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


@pytest.fixture
def product_record(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    response = admin_api.post(
        "/api/v1/admin/store-products", product_payload(category["id"]), format="json"
    )
    assert response.status_code == 201, response.json()
    return response.json()["data"]


def patch_translation(admin_api, product_record, **fields):
    return admin_api.patch(
        f"/api/v1/admin/store-products/{product_record['id']}",
        {
            "translations": [
                {"locale": "vi", "name": "Rau muống", **fields},
                {"locale": "en", "name": "Water spinach"},
            ],
        },
        format="json",
    )


@pytest.mark.django_db
def test_create_and_read_bilingual_rich_descriptions(admin_api):
    category = admin_api.post(
        "/api/v1/admin/store-categories", category_payload(), format="json"
    ).json()["data"]
    payload = product_payload(category["id"])
    for row in payload["translations"]:
        row["description_document"] = document(row["locale"])
        row["description"] = "Untrusted client fallback"
    response = admin_api.post("/api/v1/admin/store-products", payload, format="json")
    assert response.status_code == 201, response.json()
    item = admin_api.get(f"/api/v1/admin/store-products/{response.json()['data']['id']}").json()[
        "data"
    ]
    for row in item["translations"]:
        assert row["description_document"] == document(row["locale"])
        assert row["description"] == row["locale"]


@pytest.mark.django_db
def test_legacy_products_keep_text_and_null_document(admin_api, product_record):
    legacy = "  <b>Fresh</b>\r\nNext\n\n  "
    response = patch_translation(admin_api, product_record, description=legacy)
    assert response.status_code == 200, response.json()
    row = next(row for row in response.json()["data"]["translations"] if row["locale"] == "vi")
    assert row["description"] == legacy
    assert row["description_document"] is None
    response = admin_api.patch(
        f"/api/v1/admin/store-products/{product_record['id']}",
        {"retail_price_vnd": 45000},
        format="json",
    )
    assert response.status_code == 200
    assert (
        Product.objects.get(pk=product_record["id"]).translations.get(locale="vi").description
        == legacy
    )


@pytest.mark.django_db
def test_partial_updates_keep_rich_text_and_old_client_text_edits_clear_it(
    admin_api, product_record
):
    rich = document("Fresh")
    response = patch_translation(admin_api, product_record, description_document=rich)
    assert response.status_code == 200, response.json()
    for fields in ({"description": "Fresh"}, {"short_description": "New short"}, {}):
        response = patch_translation(admin_api, product_record, **fields)
        assert response.status_code == 200, response.json()
        row = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi")
        assert row.description == "Fresh"
        assert row.description_document == rich
    response = patch_translation(admin_api, product_record, description="Legacy edit")
    assert response.status_code == 200
    row = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi")
    assert row.description == "Legacy edit"
    assert row.description_document is None


@pytest.mark.django_db
def test_null_removes_format_only_and_empty_document_clears_content(admin_api, product_record):
    assert (
        patch_translation(
            admin_api, product_record, description_document=document("Fresh")
        ).status_code
        == 200
    )
    response = patch_translation(admin_api, product_record, description_document=None)
    assert response.status_code == 200
    row = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi")
    assert row.description == "Fresh"
    assert row.description_document is None
    for empty in (
        {"type": "doc", "content": []},
        {"type": "doc", "content": [{"type": "paragraph"}]},
    ):
        response = patch_translation(
            admin_api, product_record, description="Must not reappear", description_document=empty
        )
        assert response.status_code == 200
        row = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi")
        assert row.description == ""
        assert row.description_document == empty


INVALID_DOCUMENTS = [
    "<p>HTML</p>",
    [],
    {},
    {"type": "doc", "content": {}},
    {"type": "doc", "content": [{"type": "image", "attrs": {"src": "https://example.com/a.webp"}}]},
    {"type": "doc", "content": [{"type": "blockquote"}]},
    {"type": "doc", "content": [{"type": "heading", "attrs": {"level": 1}}]},
    {"type": "doc", "content": [{"type": "heading", "attrs": {"level": True}}]},
    {"type": "doc", "content": [{"type": "text", "text": "Wrong parent"}]},
    {"type": "doc", "content": [{"type": "bulletList", "content": [{"type": "paragraph"}]}]},
    {
        "type": "doc",
        "content": [
            {
                "type": "orderedList",
                "attrs": {"start": True},
                "content": [{"type": "listItem", "content": [{"type": "paragraph"}]}],
            }
        ],
    },
    {"type": "doc", "content": [{"type": "paragraph", "attrs": {"onclick": "alert(1)"}}]},
    document(42),
    document("x" * 10001),
    document("😀" * 5001),
    {"type": "doc", "content": [{"type": "paragraph"}] * 2001},
]


@pytest.mark.django_db
@pytest.mark.parametrize("invalid", INVALID_DOCUMENTS)
def test_invalid_document_is_a_400_and_changes_are_atomic(admin_api, product_record, invalid):
    before = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi").description
    response = patch_translation(admin_api, product_record, description_document=invalid)
    assert response.status_code == 400, response.json()
    assert "description_document" in str(response.json())
    assert (
        Product.objects.get(pk=product_record["id"]).translations.get(locale="vi").description
        == before
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,bad",
        "http://example.com",
        "//evil.example",
        "https://[broken",
        "https://example.com\n/a",
    ],
)
def test_unsafe_links_are_rejected(admin_api, product_record, href):
    rich = document()
    rich["content"][0]["content"][0]["marks"] = [{"type": "link", "attrs": {"href": href}}]
    response = patch_translation(admin_api, product_record, description_document=rich)
    assert response.status_code == 400, response.json()


@pytest.mark.django_db
def test_public_detail_has_rich_text_but_list_does_not(admin_api, product_record):
    rich = document("Rich public")
    assert (
        patch_translation(admin_api, product_record, description_document=rich).status_code == 200
    )
    Product.objects.filter(pk=product_record["id"]).update(is_published=True)
    client = APIClient()
    detail = client.get(f"/api/v1/public/store-products/{product_record['slug']}?locale=vi")
    assert detail.status_code == 200, detail.json()
    assert detail.json()["data"]["description_document"] == rich
    assert detail.json()["data"]["description"] == "Rich public"
    rows = client.get("/api/v1/public/store-products?locale=vi").json()["data"]
    assert "description_document" not in rows[0]
    assert "description" not in rows[0]


@pytest.mark.django_db
def test_rich_description_updates_keep_revalidation_on_commit(
    admin_api, product_record, django_capture_on_commit_callbacks
):
    with django_capture_on_commit_callbacks() as callbacks:
        response = patch_translation(admin_api, product_record, description_document=document())
        assert response.status_code == 200, response.json()
    assert callbacks


@pytest.mark.django_db
def test_rich_description_requires_admin(admin_api, product_record, active_user):
    active_user.groups.clear()
    response = patch_translation(admin_api, product_record, description_document=document())
    assert response.status_code == 403
    response = APIClient().patch(
        f"/api/v1/admin/store-products/{product_record['id']}", {"translations": []}, format="json"
    )
    assert response.status_code in {401, 403}


@pytest.mark.django_db
def test_document_size_depth_and_plain_fallback(admin_api, product_record):
    rich = {
        "type": "doc",
        "content": [
            {
                "type": "heading",
                "attrs": {"level": 2},
                "content": [{"type": "text", "text": "Bảo quản"}],
            },
            {
                "type": "bulletList",
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Mát", "marks": [{"type": "bold"}]},
                                    {"type": "hardBreak"},
                                    {
                                        "type": "text",
                                        "text": "Khô",
                                        "marks": [
                                            {"type": "italic"},
                                            {
                                                "type": "link",
                                                "attrs": {
                                                    "href": "https://example.com",
                                                    "target": "_blank",
                                                    "rel": "noopener noreferrer nofollow",
                                                    "class": None,
                                                },
                                            },
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            },
        ],
    }
    response = patch_translation(admin_api, product_record, description_document=rich)
    assert response.status_code == 200, response.json()
    row = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi")
    assert row.description == "Bảo quản\nMát\nKhô"
    assert row.description_document == rich
    deep = {"type": "paragraph"}
    for _ in range(20):
        deep = {
            "type": "bulletList",
            "content": [{"type": "listItem", "content": [{"type": "paragraph"}, deep]}],
        }
    invalid = {"type": "doc", "content": [deep]}
    assert (
        patch_translation(admin_api, product_record, description_document=invalid).status_code
        == 400
    )
    oversized = deepcopy(rich)
    oversized["content"][1]["content"][0]["content"][0]["content"][-1]["marks"][-1]["attrs"][
        "href"
    ] = "https://example.com/" + "x" * 131072
    assert (
        patch_translation(admin_api, product_record, description_document=oversized).status_code
        == 400
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "attrs",
    [
        [],
        "bad",
        {"href": []},
        {"href": "https://example.com", "target": []},
        {"href": "https://example.com", "onclick": "bad"},
        {"href": "https://example.com", "class": {}},
        {"href": "https://example.com", "rel": []},
    ],
)
def test_malformed_link_attributes_are_validation_errors(admin_api, product_record, attrs):
    rich = document()
    rich["content"][0]["content"][0]["marks"] = [{"type": "link", "attrs": attrs}]
    assert (
        patch_translation(admin_api, product_record, description_document=rich).status_code == 400
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid",
    [
        {"type": "doc", "content": [{"type": "paragraph", "content": "bad"}]},
        {"type": "doc", "content": [{"type": "paragraph", "attrs": []}]},
        {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "x", "marks": {}}]}
            ],
        },
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "x", "marks": [{"type": []}]}],
                }
            ],
        },
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": "x",
                            "marks": [{"type": "bold", "attrs": {"style": "color:red"}}],
                        }
                    ],
                }
            ],
        },
        {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {"type": "text", "text": "x", "marks": [{"type": "bold"}, {"type": "bold"}]}
                    ],
                }
            ],
        },
        {
            "type": "doc",
            "content": [{"type": "paragraph", "content": [{"type": "hardBreak", "text": "x"}]}],
        },
        {"type": "doc", "content": [{"type": "doc"}]},
    ],
)
def test_malformed_nodes_never_cause_a_server_error(admin_api, product_record, invalid):
    assert (
        patch_translation(admin_api, product_record, description_document=invalid).status_code
        == 400
    )


@pytest.mark.django_db
def test_partial_translation_without_name_preserves_existing_name(admin_api, product_record):
    response = admin_api.patch(
        f"/api/v1/admin/store-products/{product_record['id']}",
        {
            "translations": [
                {"locale": "vi", "description_document": document("Partial")},
                {"locale": "en"},
            ],
        },
        format="json",
    )
    assert response.status_code == 200, response.json()
    assert [row["name"] for row in response.json()["data"]["translations"]] == [
        "Water spinach",
        "Rau muống",
    ]
    missing_locale = admin_api.patch(
        f"/api/v1/admin/store-products/{product_record['id']}",
        {"translations": [{"description_document": document()}]},
        format="json",
    )
    assert missing_locale.status_code == 400


@pytest.mark.django_db
def test_rejected_document_rolls_back_other_fields_and_languages(admin_api, product_record):
    response = admin_api.patch(
        f"/api/v1/admin/store-products/{product_record['id']}",
        {
            "retail_price_vnd": 99999,
            "translations": [
                {"locale": "vi", "name": "Changed", "description_document": document("Valid")},
                {
                    "locale": "en",
                    "name": "Changed",
                    "description_document": {"type": "doc", "content": [{"type": "image"}]},
                },
            ],
        },
        format="json",
    )
    assert response.status_code == 400
    product = Product.objects.get(pk=product_record["id"])
    assert product.retail_price_vnd == 35000
    assert product.translations.get(locale="vi").name == "Rau muống"
    assert product.translations.get(locale="vi").description_document is None


@pytest.mark.django_db
def test_public_rich_description_uses_same_locale_as_fallback_text(admin_api, product_record):
    response = admin_api.patch(
        f"/api/v1/admin/store-products/{product_record['id']}",
        {
            "translations": [
                {"locale": "vi", "name": "Rau", "description_document": document("Tiếng Việt")}
            ]
        },
        format="json",
    )
    assert response.status_code == 200
    Product.objects.filter(pk=product_record["id"]).update(is_published=True)
    response = APIClient().get(f"/api/v1/public/store-products/{product_record['slug']}?locale=en")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["is_fallback"] is True
    assert data["content_locale"] == "vi"
    assert data["description_document"] == document("Tiếng Việt")
    assert data["description"] == "Tiếng Việt"


@pytest.mark.django_db(transaction=True)
def test_rich_description_migration_preserves_existing_translations():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    latest = executor.loader.graph.leaf_nodes()
    old = ("commerce", "0007_remove_storeorder_commerce_store_order_total_exact_and_more")
    try:
        executor.migrate([old])
        apps = executor.loader.project_state([old]).apps
        category = apps.get_model("commerce", "Category").objects.create(slug="legacy-rich")
        product = apps.get_model("commerce", "Product").objects.create(
            category=category,
            sku="LEGACY-RICH",
            slug="legacy-rich",
            unit_size="1.000",
            unit_label_vi="kg",
            unit_label_en="kg",
            retail_price_vnd=35000,
        )
        legacy = "  <b>Literal</b>\r\nDòng hai\n\n "
        row = apps.get_model("commerce", "ProductTranslation").objects.create(
            product=product,
            locale="vi",
            name="Rau",
            short_description="Ngắn",
            description=legacy,
        )
        executor = MigrationExecutor(connection)
        executor.migrate(latest)
        current_apps = executor.loader.project_state(latest).apps
        updated = current_apps.get_model("commerce", "ProductTranslation").objects.get(pk=row.pk)
        assert updated.description == legacy
        assert updated.short_description == "Ngắn"
        assert updated.description_document is None
    finally:
        MigrationExecutor(connection).migrate(latest)


@pytest.mark.django_db
def test_ten_thousand_utf16_units_are_accepted(admin_api, product_record):
    rich = document("😀" * 5000)
    response = patch_translation(admin_api, product_record, description_document=rich)
    assert response.status_code == 200, response.json()
    assert (
        Product.objects.get(pk=product_record["id"]).translations.get(locale="vi").description
        == "😀" * 5000
    )


@pytest.mark.django_db
@pytest.mark.parametrize("public", [False, True])
def test_product_lists_do_not_read_or_return_rich_documents(admin_api, product_record, public):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    assert (
        patch_translation(admin_api, product_record, description_document=document()).status_code
        == 200
    )
    Product.objects.filter(pk=product_record["id"]).update(is_published=True)
    client = APIClient() if public else admin_api
    url = "/api/v1/public/store-products?locale=vi" if public else "/api/v1/admin/store-products"
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)
    assert response.status_code == 200, response.json()
    row = response.json()["data"][0]
    if public:
        assert "description_document" not in row
    else:
        assert all("description_document" not in translation for translation in row["translations"])
        assert all("description" in translation for translation in row["translations"])
    assert not any(
        '"description_document"' in query["sql"] or "`description_document`" in query["sql"]
        for query in queries
    )


@pytest.mark.django_db
@pytest.mark.parametrize("start", [0, 1, 3, 2_147_483_647])
def test_numbered_list_from_installed_tiptap_round_trips(admin_api, product_record, start):
    # Tiptap 2 OrderedList emits both start and type:null, including after a toolbar toggle.
    rich = {
        "type": "doc",
        "content": [
            {
                "type": "orderedList",
                "attrs": {"start": start, "type": None},
                "content": [
                    {
                        "type": "listItem",
                        "content": [
                            {"type": "paragraph", "content": [{"type": "text", "text": "Giữ mát"}]}
                        ],
                    },
                ],
            }
        ],
    }
    response = patch_translation(admin_api, product_record, description_document=rich)
    assert response.status_code == 200, response.json()
    saved = Product.objects.get(pk=product_record["id"]).translations.get(locale="vi")
    assert saved.description_document == rich
    assert saved.description == "Giữ mát"
    detail = admin_api.get(f"/api/v1/admin/store-products/{product_record['id']}").json()["data"]
    assert (
        next(row for row in detail["translations"] if row["locale"] == "vi")["description_document"]
        == rich
    )
    Product.objects.filter(pk=product_record["id"]).update(is_published=True)
    public = (
        APIClient()
        .get(f"/api/v1/public/store-products/{product_record['slug']}?locale=vi")
        .json()["data"]
    )
    assert public["description_document"] == rich


@pytest.mark.django_db
@pytest.mark.parametrize("list_type", ["a", "I", "invalid", False, 1, [], {}])
def test_numbered_lists_only_accept_the_editor_default_type(admin_api, product_record, list_type):
    rich = {
        "type": "doc",
        "content": [
            {
                "type": "orderedList",
                "attrs": {"start": 1, "type": list_type},
                "content": [{"type": "listItem", "content": [{"type": "paragraph"}]}],
            }
        ],
    }
    response = patch_translation(admin_api, product_record, description_document=rich)
    assert response.status_code == 400


@pytest.mark.django_db
@pytest.mark.parametrize("start", [-1, True, False, 1.5, "0", 2_147_483_648, [], {}])
def test_numbered_list_start_rejects_unsupported_values(admin_api, product_record, start):
    rich = {
        "type": "doc",
        "content": [
            {
                "type": "orderedList",
                "attrs": {"start": start, "type": None},
                "content": [{"type": "listItem", "content": [{"type": "paragraph"}]}],
            }
        ],
    }
    assert (
        patch_translation(admin_api, product_record, description_document=rich).status_code == 400
    )
