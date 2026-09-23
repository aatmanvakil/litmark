"""Correcting a document's metadata.

Without this route the canonical name is stuck at whatever the PDF's /Info
dictionary claimed, and a publication year the file never stated could never
be supplied at all.
"""

from __future__ import annotations

from conftest import upload, wait_for_extraction

from litmark.naming import YEAR_CONFIRMED, YEAR_FROM_CREATIONDATE


def _import(client, paper):
    document_id = upload(client, "paper.pdf", paper)["imported"][0]["document"][
        "document_id"
    ]
    wait_for_extraction(client, document_id)
    return document_id


def test_a_year_read_from_the_pdf_is_marked_as_such(client, services, paper_one):
    document_id = _import(client, paper_one)

    document = services.documents.get(document_id)

    if document.year is not None:
        assert document.year_source == YEAR_FROM_CREATIONDATE
        assert "(n.d.)" in document.canonical_name


def test_confirming_a_year_puts_it_in_the_canonical_name(client, services, paper_one):
    document_id = _import(client, paper_one)

    updated = client.patch(f"/api/documents/{document_id}", json={"year": 2016}).json()

    assert updated["year"] == 2016
    assert updated["year_source"] == YEAR_CONFIRMED
    assert "(2016)" in updated["canonical_name"]
    assert "n.d." not in updated["canonical_name"]


def test_correcting_authors_changes_the_canonical_name(client, paper_one):
    document_id = _import(client, paper_one)

    updated = client.patch(
        f"/api/documents/{document_id}",
        json={"authors": "Alice Researcher and Bob Coauthor", "title": "Real Title"},
    ).json()

    assert updated["canonical_name"].startswith("Researcher, Coauthor (n.d.)")
    assert "Real Title" in updated["canonical_name"]


def test_four_authors_collapse_to_et_al(client, paper_one):
    document_id = _import(client, paper_one)

    updated = client.patch(
        f"/api/documents/{document_id}",
        json={"authors": "A One and B Two and C Three and D Four"},
    ).json()

    assert updated["canonical_name"].startswith("One et al. (n.d.)")


def test_an_absent_field_is_left_alone(client, paper_one):
    document_id = _import(client, paper_one)
    client.patch(f"/api/documents/{document_id}", json={"title": "Kept"})

    updated = client.patch(f"/api/documents/{document_id}", json={"year": 2020}).json()

    assert updated["title"] == "Kept"


def test_clear_unsets_a_field(client, paper_one):
    document_id = _import(client, paper_one)
    client.patch(f"/api/documents/{document_id}", json={"year": 2020})

    updated = client.patch(f"/api/documents/{document_id}", json={"clear": ["year"]}).json()

    assert updated["year"] is None
    assert updated["year_source"] is None
    assert "(n.d.)" in updated["canonical_name"]


def test_an_implausible_year_is_rejected(client, paper_one):
    document_id = _import(client, paper_one)

    response = client.patch(f"/api/documents/{document_id}", json={"year": 12})

    assert response.status_code == 422


def test_patching_an_unknown_document_is_a_404(client):
    assert client.patch("/api/documents/doc-999", json={"year": 2016}).status_code == 404


def test_the_correction_survives_a_reopen(client, services, paper_one):
    document_id = _import(client, paper_one)
    client.patch(
        f"/api/documents/{document_id}", json={"year": 2016, "title": "Persisted"}
    )

    reread = services.documents.get(document_id)

    assert (reread.year, reread.year_source, reread.title) == (
        2016,
        YEAR_CONFIRMED,
        "Persisted",
    )


def test_a_correction_reaches_the_bibliography(client, paper_one):
    document_id = _import(client, paper_one)
    client.patch(
        f"/api/documents/{document_id}",
        json={"year": 2016, "authors": "Alice Researcher"},
    )

    entries = client.get("/api/bibliography").json()["entries"]

    assert entries[0]["key"].startswith("researcher2016")
