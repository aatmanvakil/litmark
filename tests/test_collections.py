"""Collections classify papers; they never own or copy them.

The registry is an ordinary project file, so it must survive a reopen, be
hand-editable, and degrade quietly when edited badly.
"""

from __future__ import annotations

import json

import pytest
from conftest import upload, wait_for_extraction

from litmark.collections import CollectionStore
from litmark.errors import InvalidInput, NotFound
from litmark.services import Services
from litmark.workspace import Workspace


def _import(client, paper, name="paper.pdf"):
    return upload(client, name, paper)["imported"][0]["document"]["document_id"]


def _create(client, kind="project", name="Minimum wage paper", **extra):
    response = client.post(
        "/api/collections", json={"kind": kind, "name": name, **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_collection_roundtrip_survives_reopen(tmp_path, paper_one):
    workspace = Workspace.initialize(tmp_path / "project")
    services = Services(workspace, backend_name="fake")
    created = services.collections.create(kind="project", name="Spring paper")
    services.db.close()

    reopened = Services(Workspace(tmp_path / "project").open(), backend_name="fake")
    try:
        assert [c.collection_id for c in reopened.collections.list()] == [
            created.collection_id
        ]
        assert reopened.collections.get(created.collection_id).name == "Spring paper"
    finally:
        reopened.db.close()

    # An ordinary, hand-editable project file.
    payload = json.loads((tmp_path / "project" / "collections.json").read_text("utf-8"))
    assert payload["schema_version"] == 1
    assert created.collection_id in payload["collections"]


def test_shared_membership_across_kinds(client, paper_one, paper_two):
    shared = _import(client, paper_one, "one.pdf")
    other = _import(client, paper_two, "two.pdf")

    project = _create(client, "project", "Minimum wage paper", document_ids=[shared])
    topic = _create(client, "topic", "Diff in diff", document_ids=[shared, other])

    listed = {c["collection_id"]: c for c in client.get("/api/collections").json()["collections"]}
    assert listed[project["collection_id"]]["documents"] == [shared]
    assert listed[topic["collection_id"]]["documents"] == [shared, other]
    # One paper, two classifications, one PDF on disk.
    assert len(client.get("/api/documents").json()["documents"]) == 2


def test_a_project_and_a_topic_may_share_a_name(client):
    _create(client, "project", "Causal ID")
    _create(client, "topic", "Causal ID")


def test_duplicate_name_within_kind_rejected(client):
    _create(client, "project", "Causal ID")

    response = client.post("/api/collections", json={"kind": "project", "name": "causal  id"})

    assert response.status_code == 422  # InvalidInput, per errors.py
    assert "already exists" in response.text


def test_kind_cannot_be_changed(client):
    created = _create(client, "project", "Reclassify me")

    response = client.patch(
        f"/api/collections/{created['collection_id']}", json={"kind": "topic"}
    )

    # `kind` is not a field on the update model, so it is ignored, not applied.
    assert response.status_code == 200
    assert response.json()["kind"] == "project"


def test_rename_keeps_membership(client, paper_one):
    document_id = _import(client, paper_one)
    created = _create(client, "project", "Old name", document_ids=[document_id])

    renamed = client.patch(
        f"/api/collections/{created['collection_id']}", json={"name": "New name"}
    ).json()

    assert renamed["name"] == "New name"
    assert renamed["documents"] == [document_id]


def test_delete_collection_keeps_documents(client, services, paper_one):
    document_id = _import(client, paper_one)
    wait_for_extraction(client, document_id)
    created = _create(client, "project", "Temporary", document_ids=[document_id])

    deleted = client.delete(f"/api/collections/{created['collection_id']}").json()

    assert deleted["documents_released"] == 1
    assert deleted["children_reparented"] == 0
    assert client.get(f"/api/documents/{document_id}").status_code == 200
    assert client.get(f"/api/documents/{document_id}/pdf").status_code == 200
    assert services.documents.summary_path(document_id).parent.is_dir()


def test_removing_a_paper_leaves_it_in_its_other_collection(client, paper_one):
    document_id = _import(client, paper_one)
    first = _create(client, "project", "First", document_ids=[document_id])
    second = _create(client, "topic", "Second", document_ids=[document_id])

    client.delete(f"/api/collections/{first['collection_id']}/documents/{document_id}")

    listed = {c["collection_id"]: c for c in client.get("/api/collections").json()["collections"]}
    assert listed[first["collection_id"]]["documents"] == []
    assert listed[second["collection_id"]]["documents"] == [document_id]


def test_delete_document_prunes_membership(client, paper_one, paper_two):
    doomed = _import(client, paper_one, "one.pdf")
    survivor = _import(client, paper_two, "two.pdf")
    first = _create(client, "project", "First", document_ids=[doomed, survivor])
    second = _create(client, "topic", "Second", document_ids=[doomed])

    result = client.delete(f"/api/documents/{doomed}").json()

    assert sorted(result["collections_updated"]) == sorted(
        [first["collection_id"], second["collection_id"]]
    )
    listed = {c["collection_id"]: c for c in client.get("/api/collections").json()["collections"]}
    assert listed[first["collection_id"]]["documents"] == [survivor]
    assert listed[second["collection_id"]]["documents"] == []


def test_assigning_an_unknown_document_is_rejected(client):
    created = _create(client, "project", "Strict")

    response = client.post(
        f"/api/collections/{created['collection_id']}/documents",
        json={"document_ids": ["doc-999"]},
    )

    assert response.status_code == 404


def test_membership_is_idempotent(client, paper_one):
    document_id = _import(client, paper_one)
    created = _create(client, "project", "Idempotent")

    path = f"/api/collections/{created['collection_id']}/documents"
    client.post(path, json={"document_ids": [document_id]})
    twice = client.post(path, json={"document_ids": [document_id]}).json()

    assert twice["documents"] == [document_id]


def test_unfiled_group_is_computed_not_stored(client, paper_one, paper_two):
    filed = _import(client, paper_one, "one.pdf")
    loose = _import(client, paper_two, "two.pdf")
    _create(client, "project", "Filed", document_ids=[filed])

    assert client.get("/api/collections").json()["unfiled"] == [loose]
    assert client.get("/api/state").json()["unfiled"] == [loose]
    # Nothing named "unfiled" is persisted.
    assert "unfiled" not in (
        client.get("/api/collections").json()["collections"][0].keys()
    )


def test_malformed_collections_json_raises_invalid_input(project):
    project.collections_file.write_text("{not json", encoding="utf-8")
    store = CollectionStore(project)

    with pytest.raises(InvalidInput):
        store.list()


def test_membership_naming_a_vanished_document_is_tolerated(project):
    """A hand-edited file must degrade quietly, not crash the sidebar."""
    store = CollectionStore(project)
    created = store.create(kind="topic", name="Hand edited", documents=["doc-404"])

    assert store.get(created.collection_id).documents == ["doc-404"]
    assert store.unfiled([]) == []


def test_unknown_collection_is_a_404(client):
    assert client.get("/api/collections").status_code == 200
    assert client.patch("/api/collections/col-999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/collections/col-999").status_code == 404


def test_store_rejects_an_unknown_kind(project):
    with pytest.raises(InvalidInput):
        CollectionStore(project).create(kind="folder", name="Nope")


def test_store_rejects_a_blank_name(project):
    with pytest.raises(InvalidInput):
        CollectionStore(project).create(kind="project", name="   ")


def test_ids_are_allocated_centrally(project):
    store = CollectionStore(project)

    first = store.create(kind="project", name="One")
    second = store.create(kind="project", name="Two")

    assert first.collection_id == "col-001"
    assert second.collection_id == "col-002"


def test_get_missing_collection_raises(project):
    with pytest.raises(NotFound):
        CollectionStore(project).get("col-001")


# ------------------------------------------------------- collection-scoped search


def test_search_narrows_to_the_active_collection(client, paper_one, paper_two):
    inside = _import(client, paper_one, "one.pdf")
    outside = _import(client, paper_two, "two.pdf")
    for document_id in (inside, outside):
        wait_for_extraction(client, document_id)
    created = _create(client, "project", "Scoped", document_ids=[inside])

    everywhere = client.get("/api/search?q=mobility").json()["passages"]
    scoped = client.get(
        f"/api/search?q=mobility&collection_id={created['collection_id']}"
    ).json()["passages"]

    assert {hit["document_id"] for hit in everywhere} == {inside, outside}
    assert {hit["document_id"] for hit in scoped} == {inside}


def test_search_in_an_empty_collection_finds_nothing(client, paper_one):
    document_id = _import(client, paper_one)
    wait_for_extraction(client, document_id)
    created = _create(client, "project", "Empty")

    scoped = client.get(
        f"/api/search?q=mobility&collection_id={created['collection_id']}"
    ).json()

    # An empty collection means no papers, never every paper.
    assert scoped["passages"] == []


def test_search_intersects_collection_with_explicit_document_filter(
    client, paper_one, paper_two
):
    inside = _import(client, paper_one, "one.pdf")
    outside = _import(client, paper_two, "two.pdf")
    for document_id in (inside, outside):
        wait_for_extraction(client, document_id)
    created = _create(client, "project", "Intersect", document_ids=[inside])

    scoped = client.get(
        f"/api/search?q=mobility&collection_id={created['collection_id']}"
        f"&document_id={outside}"
    ).json()

    # Asking for a non-member while scoped yields nothing, not the non-member.
    assert scoped["passages"] == []
