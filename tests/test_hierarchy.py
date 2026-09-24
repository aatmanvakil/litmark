"""Nesting rules for collections.

The rule is about projects, not about kinds matching: a project is always a
root, a topic may sit under anything. Project -> Topic is therefore allowed
and is the case the whole feature exists for.
"""

from __future__ import annotations

import json

import pytest

from litmark.collections import MAX_DEPTH, UNSET, CollectionStore
from litmark.errors import InvalidInput, NotFound


@pytest.fixture
def store(project):
    return CollectionStore(project)


# ------------------------------------------------------ the permitted matrix


def test_a_project_may_parent_a_topic(store):
    """The target command: a subtopic under International Macro."""
    parent = store.create(kind="project", name="International Macro")

    child = store.create(
        kind="topic", name="Dominant Currency", parent_id=parent.collection_id
    )

    assert child.parent_id == parent.collection_id
    assert store.path_of(child.collection_id) == "International Macro / Dominant Currency"


def test_a_topic_may_parent_a_topic(store):
    outer = store.create(kind="topic", name="Open Economy")

    inner = store.create(kind="topic", name="Pass-through", parent_id=outer.collection_id)

    assert inner.parent_id == outer.collection_id


def test_a_project_may_be_a_root(store):
    assert store.create(kind="project", name="Root Project").parent_id is None


def test_a_topic_may_be_a_root(store):
    assert store.create(kind="topic", name="Loose Topic").parent_id is None


# ------------------------------------------------------- the forbidden cases


def test_a_project_may_not_be_parented_by_a_project(store):
    parent = store.create(kind="project", name="Outer")

    with pytest.raises(InvalidInput) as caught:
        store.create(kind="project", name="Inner", parent_id=parent.collection_id)

    assert "top-level" in caught.value.message


def test_a_project_may_not_be_parented_by_a_topic(store):
    topic = store.create(kind="topic", name="A Topic")

    with pytest.raises(InvalidInput):
        store.create(kind="project", name="Inner", parent_id=topic.collection_id)


def test_an_existing_project_cannot_be_moved_under_anything(store):
    topic = store.create(kind="topic", name="A Topic")
    a_project = store.create(kind="project", name="Stays Root")

    with pytest.raises(InvalidInput):
        store.update(a_project.collection_id, parent_id=topic.collection_id)


def test_an_unknown_parent_is_a_not_found(store):
    with pytest.raises(NotFound):
        store.create(kind="topic", name="Orphan", parent_id="col-999")


# ---------------------------------------------------------- cycles and depth


def test_a_collection_cannot_be_its_own_parent(store):
    topic = store.create(kind="topic", name="Self")

    with pytest.raises(InvalidInput):
        store.update(topic.collection_id, parent_id=topic.collection_id)


def test_a_collection_cannot_move_under_its_own_descendant(store):
    outer = store.create(kind="topic", name="Outer")
    inner = store.create(kind="topic", name="Inner", parent_id=outer.collection_id)

    with pytest.raises(InvalidInput) as caught:
        store.update(outer.collection_id, parent_id=inner.collection_id)

    assert "subtopics" in caught.value.message


def test_depth_is_capped(store):
    parent = store.create(kind="project", name="Level 1")
    current = parent.collection_id
    for level in range(2, MAX_DEPTH + 1):
        current = store.create(
            kind="topic", name=f"Level {level}", parent_id=current
        ).collection_id

    with pytest.raises(InvalidInput) as caught:
        store.create(kind="topic", name="Too Deep", parent_id=current)

    assert str(MAX_DEPTH) in caught.value.message


def test_a_hand_written_cycle_loads_without_hanging(project):
    """The file is documented as hand-editable, so it must degrade."""
    project.collections_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collections": {
                    "col-001": {"kind": "topic", "name": "A", "parent_id": "col-002"},
                    "col-002": {"kind": "topic", "name": "B", "parent_id": "col-001"},
                },
            }
        ),
        encoding="utf-8",
    )
    store = CollectionStore(project)

    loaded = store.all()

    assert set(loaded) == {"col-001", "col-002"}
    # One edge of the cycle is dropped, so the walk terminates.
    assert store.descendant_ids("col-001")
    assert store.path_of("col-001")


def test_a_hand_written_parent_on_a_project_is_dropped(project):
    project.collections_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collections": {
                    "col-001": {"kind": "topic", "name": "T"},
                    "col-002": {"kind": "project", "name": "P", "parent_id": "col-001"},
                },
            }
        ),
        encoding="utf-8",
    )

    assert CollectionStore(project).all()["col-002"].parent_id is None


def test_a_hand_written_unknown_parent_is_dropped(project):
    project.collections_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "collections": {
                    "col-001": {"kind": "topic", "name": "T", "parent_id": "col-404"}
                },
            }
        ),
        encoding="utf-8",
    )

    assert CollectionStore(project).all()["col-001"].parent_id is None


# --------------------------------------------------------------- the subtree


def test_descendants_include_self_first(store):
    root = store.create(kind="project", name="Root")
    mid = store.create(kind="topic", name="Mid", parent_id=root.collection_id)
    leaf = store.create(kind="topic", name="Leaf", parent_id=mid.collection_id)

    found = store.descendant_ids(root.collection_id)

    assert found[0] == root.collection_id
    assert set(found) == {root.collection_id, mid.collection_id, leaf.collection_id}


def test_a_sibling_subtree_is_not_included(store):
    root = store.create(kind="project", name="Root")
    mine = store.create(kind="topic", name="Mine", parent_id=root.collection_id)
    theirs = store.create(kind="topic", name="Theirs", parent_id=root.collection_id)

    assert theirs.collection_id not in store.descendant_ids(mine.collection_id)


def test_scope_documents_gather_the_whole_subtree(store):
    root = store.create(kind="project", name="Root", documents=["doc-001"])
    mid = store.create(
        kind="topic", name="Mid", parent_id=root.collection_id, documents=["doc-002"]
    )
    store.create(
        kind="topic", name="Leaf", parent_id=mid.collection_id, documents=["doc-003"]
    )

    assert store.scope_document_ids(root.collection_id) == ["doc-001", "doc-002", "doc-003"]
    assert store.scope_document_ids(mid.collection_id) == ["doc-002", "doc-003"]


def test_a_document_in_two_places_is_gathered_once(store):
    root = store.create(kind="project", name="Root", documents=["doc-001"])
    store.create(
        kind="topic", name="Mid", parent_id=root.collection_id, documents=["doc-001"]
    )

    assert store.scope_document_ids(root.collection_id) == ["doc-001"]


# -------------------------------------------------------------------- delete


def test_deleting_a_parent_splices_children_up(store):
    root = store.create(kind="project", name="Root")
    mid = store.create(kind="topic", name="Mid", parent_id=root.collection_id)
    leaf = store.create(kind="topic", name="Leaf", parent_id=mid.collection_id)

    result = store.delete(mid.collection_id)

    assert result["children_reparented"] == 1
    assert store.get(leaf.collection_id).parent_id == root.collection_id


def test_deleting_a_root_makes_its_children_roots(store):
    root = store.create(kind="project", name="Root")
    child = store.create(kind="topic", name="Child", parent_id=root.collection_id)

    store.delete(root.collection_id)

    assert store.get(child.collection_id).parent_id is None


# --------------------------------------------------------------- re-parenting


def test_detaching_needs_an_explicit_null_not_an_omission(store):
    root = store.create(kind="project", name="Root")
    child = store.create(kind="topic", name="Child", parent_id=root.collection_id)

    # Omitted: unchanged.
    assert store.update(child.collection_id, name="Renamed").parent_id == root.collection_id
    # Explicit None: detached.
    assert store.update(child.collection_id, parent_id=None).parent_id is None


def test_unset_is_the_default_for_update(store):
    root = store.create(kind="project", name="Root")
    child = store.create(kind="topic", name="Child", parent_id=root.collection_id)

    assert store.update(child.collection_id, parent_id=UNSET).parent_id == root.collection_id


# ------------------------------------------------------------------ the API


def test_the_api_round_trips_a_subtopic(client):
    parent = client.post(
        "/api/collections", json={"kind": "project", "name": "International Macro"}
    ).json()

    child = client.post(
        "/api/collections",
        json={
            "kind": "topic",
            "name": "Dominant Currency",
            "parent_id": parent["collection_id"],
        },
    )

    assert child.status_code == 201
    assert child.json()["parent_id"] == parent["collection_id"]
    listed = client.get("/api/collections").json()["collections"]
    paths = {c["path"] for c in listed}
    assert "International Macro / Dominant Currency" in paths


def test_the_api_refuses_a_parented_project(client):
    parent = client.post(
        "/api/collections", json={"kind": "project", "name": "Outer"}
    ).json()

    response = client.post(
        "/api/collections",
        json={"kind": "project", "name": "Inner", "parent_id": parent["collection_id"]},
    )

    assert response.status_code == 422


def test_the_api_reports_children_reparented(client):
    root = client.post("/api/collections", json={"kind": "project", "name": "R"}).json()
    mid = client.post(
        "/api/collections",
        json={"kind": "topic", "name": "M", "parent_id": root["collection_id"]},
    ).json()
    client.post(
        "/api/collections",
        json={"kind": "topic", "name": "L", "parent_id": mid["collection_id"]},
    )

    result = client.delete(f"/api/collections/{mid['collection_id']}").json()

    assert result["children_reparented"] == 1
