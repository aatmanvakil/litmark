"""BibTeX generation: keys, escaping, unknown metadata, and the written file.

The rule under test throughout is that a missing field stays missing. An entry
may be incomplete; it may not be invented.
"""

from __future__ import annotations

from litmark.bibliography import (
    authors_field,
    build_bibliography,
    citation_key,
    escape_bibtex,
)
from litmark.documents import Document
from litmark.references import RESOLVED, Reference

from conftest import upload, wait_for_extraction


def make_document(document_id: str, **overrides) -> Document:
    fields: dict = {
        "document_id": document_id,
        "sha256": "0" * 64,
        "original_filename": f"{document_id}.pdf",
        "byte_size": 1024,
        "imported_at": "2026-09-22T10:00:00Z",
    }
    fields.update(overrides)
    return Document(**fields)


def make_reference(reference_id: str, document_id: str, page_number: int) -> Reference:
    return Reference(
        reference_id=reference_id,
        document_id=document_id,
        document_sha256="0" * 64,
        page_number=page_number,
        status=RESOLVED,
    )


# ------------------------------------------------------------------- units


def test_citation_key_uses_surname_year_and_title_word():
    document = make_document(
        "doc-001",
        title="Mobility Restrictions and Wage Growth",
        authors="Alice Researcher and Bob Coauthor",
        year=2016,
    )
    assert citation_key(document) == "researcher2016mobility"


def test_citation_key_handles_surname_first_and_accents():
    document = make_document(
        "doc-002", title="Sur les Frictions", authors="Ekström, Åsa", year=2021
    )
    assert citation_key(document) == "ekstrom2021sur"


def test_citation_key_leads_with_the_title_word_when_the_author_is_unknown():
    # Never "2026mobility": a key beginning with a digit reads badly.
    document = make_document("doc-001", title="Mobility Restrictions", year=2026)
    assert citation_key(document) == "mobility2026"


def test_citation_key_falls_back_to_the_document_id_when_metadata_is_unknown():
    document = make_document("doc-003", original_filename="scan.pdf")
    assert citation_key(document) == "doc-003"


def test_duplicate_keys_are_disambiguated():
    documents = [
        make_document("doc-001", title="Mobility One", authors="A. Researcher", year=2016),
        make_document("doc-002", title="Mobility Two", authors="B. Researcher", year=2016),
    ]
    bibliography = build_bibliography(documents, {}, {})
    keys = [entry.key for entry in bibliography.entries]
    assert keys == ["researcher2016mobility", "researcher2016mobilitya"]


def test_authors_field_distinguishes_a_list_from_a_surname_first_name():
    assert authors_field("Alice Researcher, Bob Coauthor") == (
        "Alice Researcher and Bob Coauthor"
    )
    assert authors_field("Researcher, Alice") == "Researcher, Alice"
    assert authors_field("Alice Researcher; Bob Coauthor") == (
        "Alice Researcher and Bob Coauthor"
    )


def test_escaping_covers_bibtex_special_characters():
    escaped = escape_bibtex("Costs & Benefits: 50% {of} a_b ~ c^d \\ e")
    assert r"\&" in escaped
    assert r"\%" in escaped
    assert r"\{of\}" in escaped
    assert r"a\_b" in escaped
    assert r"\textasciitilde{}" in escaped
    assert r"\textasciicircum{}" in escaped
    assert r"\textbackslash{}" in escaped


def test_unknown_metadata_is_omitted_and_reported():
    documents = [make_document("doc-001", title="A Paper Without Metadata")]
    bibliography = build_bibliography(documents, {}, {})
    entry = bibliography.entries[0]
    fields = dict(entry.fields)

    assert "author" not in fields
    assert "year" not in fields
    assert entry.unknown == ["author", "year"]
    assert fields["title"] == "A Paper Without Metadata"
    assert any("No author" in warning for warning in bibliography.warnings)
    assert any("No year" in warning for warning in bibliography.warnings)


def test_entry_renders_as_parseable_bibtex_with_a_protected_title():
    documents = [
        make_document("doc-001", title="Mobility in Denmark", authors="Alice Researcher", year=2016)
    ]
    text = build_bibliography(documents, {}, {}).to_bibtex()

    assert "@misc{researcher2016mobility," in text
    assert "  author = {Alice Researcher}," in text
    # Doubled braces stop a style from lowercasing the proper noun.
    assert "  title = {{Mobility in Denmark}}," in text
    assert "  year = {2016}," in text
    assert "  file = {documents/doc-001/original.pdf}," in text
    assert text.count("@misc") == 1


def test_page_locators_travel_in_cite_commands_not_in_the_entry():
    documents = [make_document("doc-001", title="Mobility", authors="Alice Researcher", year=2016)]
    references = {
        "ref-001": make_reference("ref-001", "doc-001", 12),
        "ref-002": make_reference("ref-002", "doc-001", 3),
    }
    references["ref-001"].page_label = "10"

    bibliography = build_bibliography(documents, references, {})

    assert len(bibliography.entries) == 1
    assert bibliography.entries[0].reference_ids == ["ref-002", "ref-001"]
    commands = {item["reference_id"]: item["cite"] for item in bibliography.citations}
    # The printed label wins over the physical page when the PDF has one.
    assert commands["ref-001"] == "\\cite[p.~10]{researcher2016mobility}"
    assert commands["ref-002"] == "\\cite[p.~3]{researcher2016mobility}"


def test_cited_only_keeps_works_a_note_actually_cites():
    documents = [
        make_document("doc-001", title="Cited Paper", authors="Alice Researcher", year=2016),
        make_document("doc-002", title="Unread Paper", authors="Bob Coauthor", year=2018),
    ]
    references = {
        "ref-001": make_reference("ref-001", "doc-001", 1),
        "ref-002": make_reference("ref-002", "doc-002", 1),
    }
    citations = {"ref-001": [{"kind": "note", "id": "welcome", "title": "Welcome"}]}

    everything = build_bibliography(documents, references, citations)
    narrowed = build_bibliography(documents, references, citations, cited_only=True)

    assert [entry.document_id for entry in everything.entries] == ["doc-001", "doc-002"]
    assert [entry.document_id for entry in narrowed.entries] == ["doc-001"]


def test_a_reference_to_a_deleted_document_is_reported_not_fabricated():
    references = {"ref-004": make_reference("ref-004", "doc-009", 2)}
    bibliography = build_bibliography([], references, {})

    assert bibliography.entries == []
    assert any(
        "ref-004" in warning and "doc-009" in warning for warning in bibliography.warnings
    )


# --------------------------------------------------------------------- api


def test_bibliography_endpoint_reports_entries_and_cite_commands(client, paper_one):
    document_id = upload(client, "paper-one.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    wait_for_extraction(client, document_id)
    created = client.post(
        "/api/references",
        json={
            "document_id": document_id,
            "page_number": 2,
            "quote": "Table 1 reports the baseline estimates.",
        },
    )
    assert created.status_code == 201, created.text
    reference_id = created.json()["reference_id"]

    payload = client.get("/api/bibliography").json()
    entry = payload["entries"][0]

    assert entry["document_id"] == document_id
    # The title came from the PDF's first line; the producer's placeholder
    # /Author ("anonymous") is rejected, so the author stays unknown.
    assert entry["fields"]["title"] == "Mobility Restrictions and Wage Growth"
    assert "author" in entry["unknown_fields"]
    assert reference_id in entry["reference_ids"]
    commands = {item["reference_id"]: item["cite"] for item in payload["citations"]}
    assert commands[reference_id] == f"\\cite[p.~2]{{{entry['key']}}}"
    assert payload["bibtex"].startswith("% Bibliography generated by Litmark")


def test_bibtex_format_downloads_the_file(client, paper_one):
    document_id = upload(client, "paper-one.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    wait_for_extraction(client, document_id)

    response = client.get("/api/bibliography?format=bibtex")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-bibtex")
    assert 'filename="references.bib"' in response.headers["content-disposition"]
    assert "@misc{" in response.text


def test_writing_the_bibliography_is_idempotent(client, services, paper_one):
    document_id = upload(client, "paper-one.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    wait_for_extraction(client, document_id)

    first = client.post("/api/bibliography", json={}).json()
    path = services.workspace.bibliography_file

    assert first["written"] is True
    assert first["entries"] == 1
    assert path.is_file()
    assert "@misc{" in path.read_text("utf-8")

    # Deterministic output: nothing changed, so nothing is rewritten.
    second = client.post("/api/bibliography", json={}).json()
    assert second["written"] is False


def test_a_hand_edited_bibliography_is_snapshotted_before_it_is_replaced(
    client, services, paper_one
):
    document_id = upload(client, "paper-one.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    wait_for_extraction(client, document_id)
    client.post("/api/bibliography", json={})

    path = services.workspace.bibliography_file
    path.write_text("@misc{mine, title = {{Edited by hand}},}\n", encoding="utf-8")
    result = client.post("/api/bibliography", json={}).json()

    assert result["written"] is True
    snapshots = list(services.workspace.iter_history("references.bib"))
    assert snapshots, "the hand-edited file should be recoverable"
    assert "Edited by hand" in snapshots[0].read_text("utf-8")
