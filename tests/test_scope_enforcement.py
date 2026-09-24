"""A scoped conversation must not reach a document outside its collection.

Scope is enforced at the dispatch boundary rather than inside each tool, so
these tests drive `dispatch` directly: that is the one place every call
passes through, including the HTTP thread's.

The load-bearing test is `test_no_tool_can_touch_an_out_of_scope_document`.
It enumerates the schemas rather than naming tools, so a tool added later is
covered without anyone remembering to add it here.
"""

from __future__ import annotations

import pytest
from conftest import upload, wait_for_extraction

from litmark.workspace import revision_of
from litmark.agent.tools import (
    CITATION_CHECKED_TOOLS,
    CLIPPED_TOOLS,
    DOCUMENT_ARGUMENTS,
    TOOL_SCHEMAS,
    UNSCOPED_TOOLS,
    RunScope,
    dispatch,
)

QUOTE = "Assumption 2 requires that unobserved ability is orthogonal"


@pytest.fixture
def two_papers(client, services, paper_one, paper_two):
    """One paper inside a collection, one deliberately outside it."""
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
    )
    return inside, outside, scope


def _arguments_for(name: str, document_id: str) -> dict:
    """The smallest valid call to `name` that addresses `document_id`."""
    if name == "search_documents":
        return {"query": "the", "document_ids": [document_id]}
    if name == "read_pages":
        return {"document_id": document_id, "pages": [1]}
    if name == "resolve_source":
        return {"document_id": document_id, "page_number": 1, "quote": QUOTE}
    if name == "write_summary":
        return {"document_id": document_id, "text": "Rewritten out of scope."}
    return {"document_id": document_id}


def test_no_tool_can_touch_an_out_of_scope_document(services, two_papers):
    """Every document-addressing tool refuses, whether it reads or writes."""
    _, outside, scope = two_papers
    checked = []

    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        if name not in DOCUMENT_ARGUMENTS:
            continue
        result = dispatch(
            services.tools, name, _arguments_for(name, outside), scope=scope
        )
        checked.append(name)
        assert result.get("error") == "out_of_scope", (
            f"{name} reached a document outside the scope: {result}"
        )
        assert outside not in str(result.get("matches") or "")

    # Guards the enumeration itself: if DOCUMENT_ARGUMENTS were emptied, the
    # loop above would pass vacuously.
    assert set(checked) == set(DOCUMENT_ARGUMENTS), checked
    assert {"read_summary", "write_summary", "resolve_source"} <= set(checked)


def test_every_tool_is_classified_for_scope():
    """A tool in no category would silently bypass enforcement."""
    named = set(DOCUMENT_ARGUMENTS) | UNSCOPED_TOOLS | CLIPPED_TOOLS | CITATION_CHECKED_TOOLS
    unclassified = {schema["name"] for schema in TOOL_SCHEMAS} - named

    assert not unclassified, (
        f"these tools obey no scope rule: {sorted(unclassified)}. Add them to "
        "DOCUMENT_ARGUMENTS, CLIPPED_TOOLS, CITATION_CHECKED_TOOLS, or "
        "UNSCOPED_TOOLS."
    )


def test_resolve_source_cannot_mint_a_reference_out_of_scope(services, two_papers):
    """The worst case: a read refusal that still wrote durable state."""
    _, outside, scope = two_papers
    before = set(services.references.all())

    result = dispatch(
        services.tools,
        "resolve_source",
        {"document_id": outside, "page_number": 1, "quote": QUOTE},
        scope=scope,
    )

    assert result["error"] == "out_of_scope"
    assert set(services.references.all()) == before


def test_write_summary_cannot_edit_an_out_of_scope_document(services, two_papers):
    _, outside, scope = two_papers
    before = services.documents.summary_text(outside)

    result = dispatch(
        services.tools,
        "write_summary",
        {"document_id": outside, "text": "Should never land."},
        scope=scope,
    )

    assert result["error"] == "out_of_scope"
    assert services.documents.summary_text(outside) == before


def test_read_summary_refuses_an_out_of_scope_document(services, two_papers):
    inside, outside, scope = two_papers
    existing = services.documents.summary_text(outside)
    services.documents.write_summary(
        outside,
        "Secret summary.",
        expected_revision=revision_of(existing) if existing is not None else None,
    )

    refused = dispatch(services.tools, "read_summary", {"document_id": outside}, scope=scope)

    assert refused["error"] == "out_of_scope"
    assert "Secret" not in str(refused)


def test_in_scope_documents_still_work(services, two_papers):
    inside, _, scope = two_papers

    pages = dispatch(services.tools, "read_pages", {"document_id": inside, "pages": [1]}, scope=scope)
    summary = dispatch(services.tools, "read_summary", {"document_id": inside}, scope=scope)

    assert pages.get("error") is None and pages["pages"]
    assert summary.get("error") is None


def test_unfiltered_search_is_confined_to_the_scope(services, two_papers):
    inside, outside, scope = two_papers

    result = dispatch(services.tools, "search_documents", {"query": "the"}, scope=scope)

    found = {match["document_id"] for match in result["matches"]}
    assert outside not in found
    assert found <= {inside}


def test_model_supplied_document_ids_cannot_widen_scope(services, two_papers):
    """A tool argument is a suggestion; the server decides what is reachable."""
    inside, outside, scope = two_papers

    result = dispatch(
        services.tools,
        "search_documents",
        {"query": "the", "document_ids": [inside, outside]},
        scope=scope,
    )

    found = {match["document_id"] for match in result["matches"]}
    assert outside not in found


def test_list_documents_is_clipped_and_reports_the_omission(services, two_papers):
    inside, outside, scope = two_papers

    result = dispatch(services.tools, "list_documents", {}, scope=scope)

    listed = {document["document_id"] for document in result["documents"]}
    assert listed == {inside}
    assert result["excluded"] == 1
    assert result["scope"]["name"] == "Minimum wage paper"


def test_list_collections_is_never_clipped(services, two_papers):
    """The model needs it to name the collection it asks to leave."""
    _, _, scope = two_papers

    result = dispatch(services.tools, "list_collections", {}, scope=scope)

    assert result["collections"][0]["name"] == "Minimum wage paper"


def test_write_note_rejects_a_citation_to_an_out_of_scope_document(
    client, services, two_papers
):
    """A note is not a document, but a citation reaches one."""
    _, outside, scope = two_papers
    reference = dispatch(
        services.tools,
        "resolve_source",
        # Page-only: the point is to obtain a registered reference to the
        # outside document, not to exercise quote matching.
        {"document_id": outside, "page_number": 1, "allow_page_only": True},
        scope=None,  # Registered by a human, before any scope applied.
    )
    reference_id = reference["reference_id"]
    note = client.post("/api/notes", json={"title": "Scoped"}).json()

    result = dispatch(
        services.tools,
        "write_note",
        {
            "note_id": note["note_id"],
            "text": f"A claim. [p. 1](source:{reference_id})\n",
            "expected_revision": note["revision"],
        },
        scope=scope,
    )

    assert result["error"] == "out_of_scope"
    assert reference_id in result["message"]
    assert reference_id not in client.get(f"/api/notes/{note['note_id']}").json()["text"]


def test_an_unscoped_dispatch_reaches_everything(services, two_papers):
    """Clearing the scope is what restores the whole corpus."""
    inside, outside, _ = two_papers

    result = dispatch(services.tools, "search_documents", {"query": "the"}, scope=None)

    found = {match["document_id"] for match in result["matches"]}
    assert {inside, outside} <= found


def test_human_resolve_over_http_is_never_scoped(client, services, two_papers):
    """Manual PDF selection must keep working while a collection is active."""
    _, outside, _ = two_papers

    response = client.post(
        "/api/references/resolve",
        json={"document_id": outside, "page_number": 1, "quote": QUOTE},
    )

    assert response.status_code == 200
    assert response.json().get("error") is None


def test_send_message_resolves_scope_from_the_registry(client, services, two_papers):
    """The membership comes from the server, not from the request body."""
    inside, outside, scope = two_papers
    conversation = client.post("/api/conversations", json={"title": "Scoped"}).json()

    accepted = client.post(
        f"/api/conversations/{conversation['conversation_id']}/messages",
        json={
            "prompt": "Compare them.",
            "context": {
                "collection_id": scope.collection_id,
                # A client claiming a wider scope must not get one.
                "scope_document_ids": [inside, outside],
            },
        },
    )

    assert accepted.status_code == 202
    stored = client.get(f"/api/conversations/{conversation['conversation_id']}").json()
    context = stored["messages"][0]["context"]
    assert context["scope_document_ids"] == [inside]


def test_send_message_with_an_unknown_collection_is_rejected(client):
    conversation = client.post("/api/conversations", json={"title": "Bad scope"}).json()

    response = client.post(
        f"/api/conversations/{conversation['conversation_id']}/messages",
        json={"prompt": "Hello", "context": {"collection_id": "col-999"}},
    )

    assert response.status_code == 404


# ------------------------------------------------- scope includes descendants


@pytest.fixture
def nested(client, services, paper_one, paper_two, blank_paper):
    """A project, a subtopic under it, and a sibling subtree outside."""
    inner = upload(client, "inner.pdf", paper_one)["imported"][0]["document"]["document_id"]
    outer = upload(client, "outer.pdf", paper_two)["imported"][0]["document"]["document_id"]
    sibling = upload(client, "sibling.pdf", blank_paper)["imported"][0]["document"][
        "document_id"
    ]
    for document_id in (inner, outer, sibling):
        wait_for_extraction(client, document_id)
    root = services.collections.create(kind="project", name="International Macro")
    child = services.collections.create(
        kind="topic",
        name="Dominant Currency",
        parent_id=root.collection_id,
        documents=[inner],
    )
    services.collections.create(
        kind="topic", name="Unrelated", documents=[sibling]
    )
    return {"root": root, "child": child, "inner": inner, "outer": outer, "sibling": sibling}


def _scope_for(services, collection_id):
    collection = services.collections.get(collection_id)
    return RunScope(
        collection_id=collection_id,
        name=collection.name,
        document_ids=frozenset(services.collections.scope_document_ids(collection_id)),
        collection_ids=frozenset(services.collections.descendant_ids(collection_id)),
    )


def test_a_parent_scope_reaches_a_subtopics_papers(services, nested):
    """The whole point: a parent whose papers live one level down."""
    scope = _scope_for(services, nested["root"].collection_id)

    result = dispatch(
        services.tools, "read_pages", {"document_id": nested["inner"], "pages": [1]}, scope=scope
    )

    assert result.get("error") is None
    assert result["pages"]


def test_a_sibling_subtree_stays_out_of_scope(services, nested):
    scope = _scope_for(services, nested["root"].collection_id)

    result = dispatch(
        services.tools, "read_pages", {"document_id": nested["sibling"], "pages": [1]}, scope=scope
    )

    assert result["error"] == "out_of_scope"


def test_scoping_to_the_child_excludes_the_parents_own_papers(services, nested):
    services.collections.add_documents(
        nested["root"].collection_id, [nested["outer"]]
    )
    scope = _scope_for(services, nested["child"].collection_id)

    allowed = dispatch(
        services.tools, "read_summary", {"document_id": nested["inner"]}, scope=scope
    )
    refused = dispatch(
        services.tools, "read_summary", {"document_id": nested["outer"]}, scope=scope
    )

    assert allowed.get("error") is None
    assert refused["error"] == "out_of_scope"


def test_the_scope_block_reports_the_subtree(services, nested):
    scope = _scope_for(services, nested["root"].collection_id)

    described = dispatch(services.tools, "list_documents", {}, scope=scope)["scope"]

    assert described["includes_descendants"] is True
    assert described["collection_count"] == 2


def test_a_refusal_mentions_what_is_under_the_collection(services, nested):
    scope = _scope_for(services, nested["root"].collection_id)

    refused = dispatch(
        services.tools, "read_pages", {"document_id": nested["sibling"], "pages": [1]}, scope=scope
    )

    assert "under it" in refused["message"]


def test_send_message_resolves_the_whole_subtree(client, services, nested):
    conversation = client.post("/api/conversations", json={"title": "Nested"}).json()

    client.post(
        f"/api/conversations/{conversation['conversation_id']}/messages",
        json={
            "prompt": "Review these.",
            "context": {"collection_id": nested["root"].collection_id},
        },
    )

    stored = client.get(f"/api/conversations/{conversation['conversation_id']}").json()
    context = stored["messages"][0]["context"]
    assert nested["inner"] in context["scope_document_ids"]
    assert nested["sibling"] not in context["scope_document_ids"]
    assert len(context["scope_collection_ids"]) == 2
