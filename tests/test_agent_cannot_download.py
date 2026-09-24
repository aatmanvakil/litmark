"""No agent tool may fetch a PDF. Only a human click does.

This enumerates TOOL_SCHEMAS rather than naming tools, so a tool added later
is covered without anyone remembering to come back here — the same shape as
the scope-enforcement test.
"""

from __future__ import annotations

import socket

import pytest

from litmark.agent.tools import TOOL_SCHEMAS, dispatch
from litmark.proposals import PENDING, ProposalStore


def _download_shaped_arguments(name: str) -> dict:
    """Arguments that would fetch something, if the tool were able to."""
    base = {
        "query": "10.1234/exchange.2021",
        "url": "https://journals.example.org/jpe/exchange.pdf",
        "version_id": "v1",
        "document_id": "doc-001",
        "collection_id": "col-001",
        "note_id": "welcome",
        "name": "Anything",
        "kind": "topic",
        "text": "hello",
        "pages": [1],
        "page_number": 1,
        "path": "Papers/smuggled.pdf",
    }
    schema = next(s for s in TOOL_SCHEMAS if s["name"] == name)
    properties = schema["input_schema"].get("properties") or {}
    return {key: value for key, value in base.items() if key in properties}


def _snapshot(workspace):
    """Every file in the project, with its bytes."""
    return {
        path: path.read_bytes()
        for path in sorted(workspace.root.rglob("*"))
        if path.is_file() and ".research" not in path.parts
    }


def test_no_tool_can_write_a_byte_to_papers(services, monkeypatch):
    """A26. The machine-checkable form of 'the agent cannot download'."""
    opened: list[str] = []

    def forbidden(*args, **kwargs):
        opened.append("socket")
        raise AssertionError("a tool opened a socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    workspace = services.workspace
    before = _snapshot(workspace)
    papers_before = sorted(p.name for p in workspace.papers_dir.iterdir())

    called = []
    for schema in TOOL_SCHEMAS:
        name = schema["name"]
        called.append(name)
        # Errors are fine and expected; what matters is that nothing lands.
        dispatch(services.tools, name, _download_shaped_arguments(name))

    assert called == [s["name"] for s in TOOL_SCHEMAS], "not every tool was tried"
    assert sorted(p.name for p in workspace.papers_dir.iterdir()) == papers_before
    assert _snapshot(workspace) == before, "a tool changed a project file"
    assert opened == [], "a tool reached the network"


def test_propose_download_writes_an_offer_and_nothing_else(client, services, monkeypatch):
    """A27. The offer is a row; the project is byte-identical."""
    monkeypatch.setattr(
        services, "resolve_paper", lambda query: _fake_resolution()
    )
    workspace = services.workspace
    before = _snapshot(workspace)

    result = dispatch(services.tools, "propose_download", {"query": "exchange rate"})

    assert result["ok"] is True
    assert result["downloaded"] is False
    assert result["proposal"]["status"] == PENDING
    # An offer, not a file.
    assert _snapshot(workspace) == before
    assert list(workspace.papers_dir.iterdir()) == []
    assert client.get("/api/documents").json()["documents"] == []


def test_the_offer_carries_every_version_in_one_card(services, monkeypatch):
    monkeypatch.setattr(services, "resolve_paper", lambda query: _fake_resolution())

    result = dispatch(services.tools, "propose_download", {"query": "exchange rate"})

    versions = result["proposal"]["versions"]
    assert len(versions) == 3
    assert [v["version_id"] for v in versions] == ["v1", "v2", "v3"]
    # One proposal, not three.
    assert len(ProposalStore(services.db).pending()) == 1


def test_a_paywalled_version_is_offered_without_a_download(services, monkeypatch):
    monkeypatch.setattr(services, "resolve_paper", lambda query: _fake_resolution())

    result = dispatch(services.tools, "propose_download", {"query": "exchange rate"})

    paywalled = [v for v in result["proposal"]["versions"] if not v["retrievable"]]
    assert paywalled, "the paywalled version was dropped instead of shown"
    assert paywalled[0]["reason"]


def test_the_tool_tells_the_model_it_has_not_downloaded(services, monkeypatch):
    monkeypatch.setattr(services, "resolve_paper", lambda query: _fake_resolution())

    result = dispatch(services.tools, "propose_download", {"query": "exchange rate"})

    assert "Nothing has been downloaded" in result["message"]


def test_find_paper_writes_nothing(services, monkeypatch):
    monkeypatch.setattr(services, "resolve_paper", lambda query: _fake_resolution())
    before = _snapshot(services.workspace)

    result = dispatch(services.tools, "find_paper", {"query": "exchange rate"})

    assert result["ok"] is True
    assert result["wrote_anything"] is False
    assert _snapshot(services.workspace) == before


def test_without_a_provider_the_tools_say_so(services):
    found = dispatch(services.tools, "find_paper", {"query": "anything"})
    offered = dispatch(services.tools, "propose_download", {"query": "anything"})

    assert found["candidates"] == [] or found.get("ok") is True
    assert offered.get("ok") is False or offered.get("error")


def test_an_unknown_target_collection_is_refused_before_offering(services, monkeypatch):
    monkeypatch.setattr(services, "resolve_paper", lambda query: _fake_resolution())

    result = dispatch(
        services.tools,
        "propose_download",
        {"query": "exchange rate", "assign_collection_id": "col-999"},
    )

    assert result.get("error") == "not_found"
    assert ProposalStore(services.db).pending() == []


# ------------------------------------------------------------------ fixture


def _fake_resolution():
    """A resolved work with three versions, one of them paywalled."""
    from litmark.acquisition import Candidate, Resolution, Version, Work

    work = Work(
        title="Exchange Rate Disconnect",
        authors=["Anna Müller", "Bo Lindqvist"],
        year=2021,
        journal="JPE",
        doi="10.1234/exchange.2021",
        source="crossref",
    )
    versions = [
        Version(
            version_type="published",
            url="https://journals.example.org/jpe/exchange.pdf",
            host="publisher",
            license="cc-by",
            retrievable=True,
            source="unpaywall",
        ),
        Version(
            version_type="accepted_manuscript",
            url="https://eprints.example.ac.uk/1/accepted.pdf",
            host="repository",
            retrievable=True,
            source="unpaywall",
        ),
        Version(
            version_type="published",
            url="https://doi.org/10.1234/exchange.2021",
            host="publisher",
            retrievable=False,
            source="unpaywall",
            reason="paywalled",
        ),
    ]
    return Resolution(
        query="exchange rate", candidates=[Candidate(work=work, versions=versions)]
    )
