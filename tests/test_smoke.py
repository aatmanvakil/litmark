"""A first end-to-end pass: import, extract, search, resolve, cite, save."""

from __future__ import annotations

from conftest import upload, wait_for_extraction


def test_project_state_without_agent(client):
    state = client.get("/api/state").json()
    assert state["project"]["schema_version"] == 1
    assert any(note["note_id"] == "welcome" for note in state["notes"])


def test_import_extract_search_and_cite(client, paper_one):
    result = upload(client, "paper-one.pdf", paper_one)
    document_id = result["imported"][0]["document"]["document_id"]
    assert result["imported"][0]["duplicate"] is False

    document = wait_for_extraction(client, document_id)
    assert document["extraction"]["status"] == "ok"
    assert document["page_count"] == 2

    # The PDF is readable immediately and served as the original bytes.
    pdf = client.get(f"/api/documents/{document_id}/pdf")
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-"

    hits = client.get("/api/search", params={"q": "mobility restriction"}).json()
    assert hits["passages"], hits
    assert hits["passages"][0]["document_id"] == document_id

    resolved = client.post(
        "/api/references/resolve",
        json={
            "document_id": document_id,
            "page_number": 2,
            "quote": "Assumption 2 requires that unobserved ability is orthogonal",
        },
    ).json()
    assert resolved["status"] == "resolved", resolved
    reference_id = resolved["reference_id"]

    note = client.post("/api/notes", json={"title": "Literature note"}).json()
    body = f"# Literature note\n\nIdentification rests on an orthogonality claim.\n[Assumption 2, p. 2](source:{reference_id})\n"
    saved = client.put(
        f"/api/notes/{note['note_id']}",
        json={"text": body, "expected_revision": note["revision"]},
    )
    assert saved.status_code == 200, saved.text
    assert f"source:{reference_id}" in client.get(f"/api/notes/{note['note_id']}").json()["text"]


def test_identical_bytes_do_not_duplicate(client, paper_one):
    first = upload(client, "paper.pdf", paper_one)
    second = upload(client, "paper-copy.pdf", paper_one)
    assert second["imported"][0]["duplicate"] is True
    assert (
        first["imported"][0]["document"]["document_id"]
        == second["imported"][0]["document"]["document_id"]
    )
    assert len(client.get("/api/documents").json()["documents"]) == 1
