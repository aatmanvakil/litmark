"""Driving collections from chat.

Organisation is unscoped: scope governs what a paper's *contents* the agent
may reach, not how the library is arranged. Filing a paper is the exception,
because that reaches the paper.
"""

from __future__ import annotations

import pytest
from conftest import upload, wait_for_extraction

from litmark.agent.tools import DOCUMENT_ARGUMENTS, UNSCOPED_TOOLS, RunScope, dispatch


@pytest.fixture
def two_papers(client, services, paper_one, paper_two):
    inside = upload(client, "inside.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    outside = upload(client, "outside.pdf", paper_two)["imported"][0]["document"][
        "document_id"
    ]
    for document_id in (inside, outside):
        wait_for_extraction(client, document_id)
    collection = services.collections.create(
        kind="project", name="Minimum wage paper", documents=[inside]
    )
    scope = RunScope(
        collection_id=collection.collection_id,
        name=collection.name,
        document_ids=frozenset([inside]),
        collection_ids=frozenset([collection.collection_id]),
    )
    return inside, outside, collection, scope


# ------------------------------------------------------------------ create


def test_the_agent_can_create_a_project(services):
    result = dispatch(services.tools, "create_collection", {"kind": "project", "name": "New Work"})

    assert result["ok"] is True
    assert result["collection"]["kind"] == "project"


def test_the_agent_can_create_a_nested_subtopic(services):
    parent = dispatch(
        services.tools, "create_collection", {"kind": "project", "name": "International Macro"}
    )["collection"]

    child = dispatch(
        services.tools,
        "create_collection",
        {
            "kind": "topic",
            "name": "Dominant Currency",
            "parent_id": parent["collection_id"],
        },
    )

    assert child["ok"] is True
    assert child["collection"]["path"] == "International Macro / Dominant Currency"


def test_the_agent_cannot_nest_a_project(services):
    parent = dispatch(
        services.tools, "create_collection", {"kind": "project", "name": "Outer"}
    )["collection"]

    result = dispatch(
        services.tools,
        "create_collection",
        {"kind": "project", "name": "Inner", "parent_id": parent["collection_id"]},
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_input"


# ------------------------------------------------------------ rename / move


def test_the_agent_can_rename_and_move(services):
    root = dispatch(services.tools, "create_collection", {"kind": "project", "name": "Root"})[
        "collection"
    ]
    loose = dispatch(services.tools, "create_collection", {"kind": "topic", "name": "Loose"})[
        "collection"
    ]

    renamed = dispatch(
        services.tools,
        "update_collection",
        {"collection_id": loose["collection_id"], "name": "Tidy"},
    )
    moved = dispatch(
        services.tools,
        "update_collection",
        {"collection_id": loose["collection_id"], "parent_id": root["collection_id"]},
    )

    assert renamed["collection"]["name"] == "Tidy"
    assert moved["collection"]["path"] == "Root / Tidy"


def test_detach_moves_to_the_top_level(services):
    root = dispatch(services.tools, "create_collection", {"kind": "project", "name": "Root"})[
        "collection"
    ]
    child = dispatch(
        services.tools,
        "create_collection",
        {"kind": "topic", "name": "Child", "parent_id": root["collection_id"]},
    )["collection"]

    detached = dispatch(
        services.tools,
        "update_collection",
        {"collection_id": child["collection_id"], "detach": True},
    )

    assert detached["collection"]["parent_id"] is None


def test_omitting_the_parent_leaves_it_alone(services):
    root = dispatch(services.tools, "create_collection", {"kind": "project", "name": "Root"})[
        "collection"
    ]
    child = dispatch(
        services.tools,
        "create_collection",
        {"kind": "topic", "name": "Child", "parent_id": root["collection_id"]},
    )["collection"]

    renamed = dispatch(
        services.tools,
        "update_collection",
        {"collection_id": child["collection_id"], "name": "Renamed"},
    )

    assert renamed["collection"]["parent_id"] == root["collection_id"]


# ------------------------------------------------------------------ delete


def test_the_agent_may_delete_an_empty_collection(services):
    created = dispatch(
        services.tools, "create_collection", {"kind": "topic", "name": "Empty"}
    )["collection"]

    result = dispatch(
        services.tools, "delete_collection", {"collection_id": created["collection_id"]}
    )

    assert result["ok"] is True


def test_the_agent_may_not_delete_a_collection_holding_papers(services, two_papers):
    """Text inside a PDF must not be able to dismantle a library."""
    _, _, collection, _ = two_papers

    result = dispatch(
        services.tools, "delete_collection", {"collection_id": collection.collection_id}
    )

    assert result["ok"] is False
    assert result["error"] == "collection_not_empty"
    assert services.collections.exists(collection.collection_id)


def test_the_agent_may_not_delete_a_collection_with_subtopics(services):
    root = dispatch(services.tools, "create_collection", {"kind": "project", "name": "Root"})[
        "collection"
    ]
    dispatch(
        services.tools,
        "create_collection",
        {"kind": "topic", "name": "Child", "parent_id": root["collection_id"]},
    )

    result = dispatch(
        services.tools, "delete_collection", {"collection_id": root["collection_id"]}
    )

    assert result["ok"] is False
    assert "subcollection" in result["message"]


def test_the_interface_can_still_delete_a_full_collection(client, services, two_papers):
    _, _, collection, _ = two_papers

    response = client.delete(f"/api/collections/{collection.collection_id}")

    assert response.status_code == 200
    assert not services.collections.exists(collection.collection_id)


# ------------------------------------------------------------- assign, scope


def test_assignment_is_scope_checked(services, two_papers):
    inside, outside, collection, scope = two_papers
    other = services.collections.create(kind="topic", name="Elsewhere")

    allowed = dispatch(
        services.tools,
        "assign_document",
        {"collection_id": other.collection_id, "document_id": inside},
        scope=scope,
    )
    refused = dispatch(
        services.tools,
        "assign_document",
        {"collection_id": other.collection_id, "document_id": outside},
        scope=scope,
    )

    assert allowed["ok"] is True
    assert refused["error"] == "out_of_scope"
    assert outside not in services.collections.get(other.collection_id).documents


def test_unassignment_is_scope_checked(services, two_papers):
    inside, outside, collection, scope = two_papers
    services.collections.add_documents(collection.collection_id, [outside])

    refused = dispatch(
        services.tools,
        "unassign_document",
        {"collection_id": collection.collection_id, "document_id": outside},
        scope=scope,
    )

    assert refused["error"] == "out_of_scope"
    assert outside in services.collections.get(collection.collection_id).documents


def test_managing_collections_is_unscoped(services, two_papers):
    """The target command creates a subtopic while a scope is active."""
    _, _, _, scope = two_papers

    created = dispatch(
        services.tools,
        "create_collection",
        {"kind": "project", "name": "International Macro"},
        scope=scope,
    )
    child = dispatch(
        services.tools,
        "create_collection",
        {
            "kind": "topic",
            "name": "Dominant Currency",
            "parent_id": created["collection"]["collection_id"],
        },
        scope=scope,
    )

    assert created["ok"] is True
    assert child["ok"] is True


# ------------------------------------------------------------ classification


def test_the_new_tools_are_classified():
    assert DOCUMENT_ARGUMENTS["assign_document"] == "document_id"
    assert DOCUMENT_ARGUMENTS["unassign_document"] == "document_id"
    assert {"create_collection", "update_collection", "delete_collection"} <= UNSCOPED_TOOLS


def test_list_collections_exposes_the_tree(services):
    parent = dispatch(
        services.tools, "create_collection", {"kind": "project", "name": "Root"}
    )["collection"]
    dispatch(
        services.tools,
        "create_collection",
        {"kind": "topic", "name": "Child", "parent_id": parent["collection_id"]},
    )

    listed = dispatch(services.tools, "list_collections", {})["collections"]

    paths = {item["path"] for item in listed}
    assert "Root / Child" in paths
    assert any(item["parent_id"] == parent["collection_id"] for item in listed)


def test_a_collection_change_is_announced_not_logged_as_a_file_edit(services):
    """A collection is not a file, so it must not enter the change/undo path."""
    dispatch(services.tools, "create_collection", {"kind": "topic", "name": "Announced"})

    assert services.tools.drain_changes() == []
    events = services.db.query("SELECT type FROM events ORDER BY seq DESC LIMIT 1")
    assert events[0]["type"] == "collection_updated"
