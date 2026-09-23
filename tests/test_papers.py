"""Papers/ holds exactly one canonical PDF per document.

The pointer in metadata.json is the link; document_id stays the identity, so
references, change records and the extraction cache are untouched by any of
this. A pointer that goes stale is recoverable, never fatal.
"""

from __future__ import annotations

from conftest import make_pdf, upload, wait_for_extraction

from litmark.naming import comparable


def _import(client, paper, name="paper.pdf"):
    document_id = upload(client, name, paper)["imported"][0]["document"]["document_id"]
    wait_for_extraction(client, document_id)
    return document_id


# --------------------------------------------------------------- placement


def test_the_canonical_pdf_lives_under_papers(client, services, paper_one):
    document_id = _import(client, paper_one)

    document = services.documents.get(document_id)
    path, status = services.documents.resolve_pdf(document_id)

    assert status == "ok"
    assert path.parent == services.workspace.papers_dir
    assert path.name == document.pdf["canonical_name"]
    assert path.name.endswith(".pdf") and "–" in path.name


def test_the_document_directory_keeps_only_derived_files(client, services, paper_one):
    document_id = _import(client, paper_one)

    names = {p.name for p in services.documents.dir_for(document_id).iterdir()}

    assert "original.pdf" not in names
    assert {"metadata.json", "pages.json"} <= names


def test_the_bytes_are_stored_unchanged(client, services, paper_one):
    document_id = _import(client, paper_one)

    path, _ = services.documents.resolve_pdf(document_id)

    assert path.read_bytes() == paper_one


def test_identical_bytes_still_deduplicate(client, paper_one, services):
    first = _import(client, paper_one, "one.pdf")

    again = upload(client, "one-again.pdf", paper_one)["imported"][0]

    assert again["duplicate"] is True
    assert again["document"]["document_id"] == first
    assert len(list(services.workspace.papers_dir.iterdir())) == 1


def test_two_papers_with_the_same_canonical_name_both_survive(client, services):
    """One must not overwrite the other, even on a case-folding filesystem."""
    first = make_pdf([[(72, 700, "Alpha content one")]])
    second = make_pdf([[(72, 700, "Beta content two")]])

    a = upload(client, "Same.pdf", first)["imported"][0]["document"]["document_id"]
    b = upload(client, "SAME.pdf", second)["imported"][0]["document"]["document_id"]
    client.patch(f"/api/documents/{a}", json={"title": "Same Title"})
    client.patch(f"/api/documents/{b}", json={"title": "Same Title"})

    first_path, _ = services.documents.resolve_pdf(a)
    second_path, _ = services.documents.resolve_pdf(b)

    assert first_path != second_path
    assert comparable(first_path.name) != comparable(second_path.name)
    assert first_path.read_bytes() == first
    assert second_path.read_bytes() == second


# ------------------------------------------------------------------ renames


def test_correcting_metadata_renames_the_file(client, services, paper_one):
    document_id = _import(client, paper_one)
    before, _ = services.documents.resolve_pdf(document_id)

    client.patch(
        f"/api/documents/{document_id}",
        json={"authors": "Alice Researcher", "year": 2016, "title": "Renamed Work"},
    )

    after, status = services.documents.resolve_pdf(document_id)
    assert status == "ok"
    assert after.name == "Researcher (2016) – Renamed Work.pdf"
    assert not before.exists()
    assert after.read_bytes() == paper_one


def test_the_previous_name_is_recoverable_after_a_rename(client, services, paper_one):
    document_id = _import(client, paper_one)
    client.patch(f"/api/documents/{document_id}", json={"title": "New Name"})

    snapshots = list(services.workspace.history_dir.glob("*-rename-*"))

    assert snapshots and snapshots[0].read_bytes() == paper_one


def test_references_survive_a_rename(client, services, paper_one):
    document_id = _import(client, paper_one)
    reference = client.post(
        "/api/references",
        json={"document_id": document_id, "page_number": 1, "allow_page_only": True},
    ).json()

    client.patch(f"/api/documents/{document_id}", json={"title": "Moved"})

    resolved = client.get(f"/api/references/{reference['reference_id']}").json()
    assert resolved["document_id"] == document_id
    assert client.get(f"/api/documents/{document_id}/pdf").status_code == 200


# ------------------------------------------------------- missing and relink


def test_a_renamed_file_with_matching_bytes_is_adopted(client, services, paper_one):
    """The user chose that spelling, so it is kept, not reverted."""
    document_id = _import(client, paper_one)
    path, _ = services.documents.resolve_pdf(document_id)
    renamed = path.with_name("A Name I Chose.pdf")
    path.rename(renamed)

    found, status = services.documents.resolve_pdf(document_id)

    assert status == "relinked"
    assert found == renamed
    assert services.documents.get(document_id).pdf["canonical_name"] == "A Name I Chose.pdf"


def test_a_deleted_file_is_a_recoverable_missing_state(client, services, paper_one):
    document_id = _import(client, paper_one)
    path, _ = services.documents.resolve_pdf(document_id)
    path.unlink()

    found, status = services.documents.resolve_pdf(document_id)

    assert (found, status) == (None, "missing")
    # Everything else about the document survives.
    assert client.get(f"/api/documents/{document_id}").status_code == 200
    assert client.get(f"/api/documents/{document_id}/pages").status_code == 200
    assert client.get(f"/api/documents/{document_id}/pdf").status_code == 404


def test_re_supplying_the_bytes_restores_the_link(client, services, paper_one):
    document_id = _import(client, paper_one)
    path, _ = services.documents.resolve_pdf(document_id)
    path.unlink()

    response = client.post(
        f"/api/documents/{document_id}/pdf",
        files={"file": ("whatever.pdf", paper_one, "application/pdf")},
    )

    assert response.status_code == 200
    assert client.get(f"/api/documents/{document_id}/pdf").status_code == 200


def test_different_bytes_are_refused_as_a_relink(client, services, paper_one, paper_two):
    document_id = _import(client, paper_one)
    services.documents.resolve_pdf(document_id)[0].unlink()

    response = client.post(
        f"/api/documents/{document_id}/pdf",
        files={"file": ("other.pdf", paper_two, "application/pdf")},
    )

    # Swapping the bytes would move existing references onto different content.
    assert response.status_code == 422


# --------------------------------------------------------------- unclaimed


def test_a_loose_pdf_is_listed_but_not_claimed(client, services, paper_two):
    (services.workspace.papers_dir / "Dropped In.pdf").write_bytes(paper_two)

    listed = client.get("/api/papers/unclaimed").json()["unclaimed"]

    assert [item["filename"] for item in listed] == ["Dropped In.pdf"]
    assert client.get("/api/documents").json()["documents"] == []


def test_an_unclaimed_pdf_imports_only_when_asked(client, services, paper_two):
    (services.workspace.papers_dir / "Dropped In.pdf").write_bytes(paper_two)

    imported = client.post(
        "/api/papers/unclaimed/import", json={"path": "Papers/Dropped In.pdf"}
    ).json()

    assert imported["duplicate"] is False
    assert client.get("/api/papers/unclaimed").json()["unclaimed"] == []
    # Exactly one canonical PDF, not the loose file plus a copy.
    assert len(list(services.workspace.papers_dir.iterdir())) == 1


def test_an_already_stored_pdf_is_not_listed_as_unclaimed(client, paper_one):
    _import(client, paper_one)

    assert client.get("/api/papers/unclaimed").json()["unclaimed"] == []


# ------------------------------------------------------------------ deletion


def test_deleting_a_document_also_trashes_its_canonical_pdf(client, services, paper_one):
    document_id = _import(client, paper_one)
    path, _ = services.documents.resolve_pdf(document_id)

    client.delete(f"/api/documents/{document_id}")

    assert not path.exists()
    assert list(services.workspace.papers_dir.iterdir()) == []
    # Recoverable, not destroyed.
    trashed = list(services.workspace.history_dir.glob("deleted-*"))
    assert trashed and any(p.suffix == ".pdf" for p in trashed[0].iterdir())


# -------------------------------------------------------------- bibliography


def test_the_bibliography_file_field_points_at_the_canonical_pdf(client, paper_one):
    document_id = _import(client, paper_one)

    bibtex = client.get("/api/bibliography?format=bibtex").text

    assert "Papers/" in bibtex
    assert "documents/doc-001/original.pdf" not in bibtex


def test_a_second_export_still_rewrites_nothing(client, paper_one):
    _import(client, paper_one)

    first = client.post("/api/bibliography", json={}).json()
    second = client.post("/api/bibliography", json={}).json()

    assert (first["written"], second["written"]) == (True, False)
