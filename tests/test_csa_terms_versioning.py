import ast
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from memberships.models import CSATermsClause, CSATermsDocument
from memberships.terms import (
    CSATermsConflict,
    canonical_csa_terms_hash,
    get_csa_terms,
    get_current_csa_terms,
    publish_csa_terms,
)


def _draft(version="CSA_TERMS_TEST"):
    document = CSATermsDocument.objects.create(
        version=version,
        title_vi="Điều khoản thử nghiệm",
        title_en="Test terms",
    )
    CSATermsClause.objects.create(
        document=document,
        key="delivery",
        sort_order=10,
        heading_vi="Giao hàng",
        heading_en="Delivery",
        paragraphs_vi=["Nội dung giao hàng."],
        paragraphs_en=["Delivery details."],
    )
    return document


@pytest.mark.django_db
def test_publishing_draft_archives_previous_current_and_serves_exact_content(active_user):
    previous = CSATermsDocument.objects.get(status=CSATermsDocument.Status.PUBLISHED)
    draft = _draft()

    published = publish_csa_terms(draft, actor=active_user, expected_revision=1)

    previous.refresh_from_db()
    assert previous.status == CSATermsDocument.Status.ARCHIVED
    assert published.status == CSATermsDocument.Status.PUBLISHED
    assert published.revision == 2
    assert published.published_by == active_user
    assert get_current_csa_terms("vi") == {
        "version": "CSA_TERMS_TEST",
        "locale": "vi",
        "content": {
            "title": "Điều khoản thử nghiệm",
            "sections": [
                {
                    "key": "delivery",
                    "heading": "Giao hàng",
                    "paragraphs": ["Nội dung giao hàng."],
                }
            ],
        },
        "hash": canonical_csa_terms_hash(
            {
                "title": "Điều khoản thử nghiệm",
                "sections": [
                    {
                        "key": "delivery",
                        "heading": "Giao hàng",
                        "paragraphs": ["Nội dung giao hàng."],
                    }
                ],
            }
        ),
    }


@pytest.mark.django_db
def test_published_document_and_clauses_are_immutable(active_user):
    published = publish_csa_terms(_draft(), actor=active_user, expected_revision=1)
    published.title_vi = "Đã sửa"
    with pytest.raises(ValidationError, match="csa_terms_immutable"):
        published.save()

    clause = published.clauses.get()
    clause.heading_vi = "Đã sửa"
    with pytest.raises(ValidationError, match="csa_terms_immutable"):
        clause.save()
    with pytest.raises(ValidationError, match="csa_terms_immutable"):
        clause.delete()


@pytest.mark.django_db
def test_publish_allows_clauses_without_optional_headings(active_user):
    draft = _draft()
    draft.clauses.update(heading_vi="", heading_en="")

    published = publish_csa_terms(draft, actor=active_user, expected_revision=1)

    assert published.status == CSATermsDocument.Status.PUBLISHED


@pytest.mark.django_db
def test_publish_rejects_stale_revision_and_incomplete_paragraphs(active_user):
    draft = _draft()
    with pytest.raises(CSATermsConflict, match="csa_terms_revision_conflict"):
        publish_csa_terms(draft, actor=active_user, expected_revision=2)

    draft.clauses.update(paragraphs_en=[])
    with pytest.raises(CSATermsConflict, match="csa_terms_incomplete"):
        publish_csa_terms(draft, actor=active_user, expected_revision=1)


@pytest.mark.django_db
def test_stale_draft_document_cannot_delete_published_terms(active_user):
    draft = _draft()
    stale = CSATermsDocument.objects.get(pk=draft.pk)
    publish_csa_terms(draft, actor=active_user, expected_revision=1)

    with pytest.raises(ValidationError, match="csa_terms_immutable"):
        stale.delete()


@pytest.mark.django_db
def test_stale_draft_clause_cannot_modify_published_terms(active_user):
    draft = _draft()
    stale = CSATermsClause.objects.select_related("document").get(document=draft)
    publish_csa_terms(draft, actor=active_user, expected_revision=1)

    stale.heading_en = "Changed through a stale instance"
    with pytest.raises(ValidationError, match="csa_terms_immutable"):
        stale.save()


@pytest.mark.django_db
def test_seeded_versions_preserve_existing_hashes_and_are_addressable():
    current = get_current_csa_terms("en")
    assert current["version"] == "CSA_TERMS_V2"
    assert current["hash"] == canonical_csa_terms_hash(current["content"])

    archived = get_csa_terms(version="CSA_TERMS_V1", locale="vi")
    assert archived["version"] == "CSA_TERMS_V1"
    assert archived["content"]["sections"][0]["key"] == "parties"


@pytest.mark.django_db
def test_database_rejects_two_published_terms_documents():
    draft = _draft()

    with pytest.raises(IntegrityError), transaction.atomic():
        CSATermsDocument.objects.filter(pk=draft.pk).update(
            status=CSATermsDocument.Status.PUBLISHED,
            published_slot=True,
        )


def test_terms_migration_does_not_import_runtime_terms_module():
    migration = Path(__file__).parents[1] / "memberships/migrations/0030_csa_terms_documents.py"
    tree = ast.parse(migration.read_text(encoding="utf-8"))

    imports = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ]
    assert "memberships.terms" not in imports
