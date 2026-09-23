"""Scope narrowing must never silently widen.

An empty document scope means "no documents are in scope". Treating it as
falsy made it mean "every document", which is the opposite.
"""

from __future__ import annotations

from conftest import upload, wait_for_extraction


def _import(client, paper, name="paper.pdf"):
    document_id = upload(client, name, paper)["imported"][0]["document"]["document_id"]
    wait_for_extraction(client, document_id)
    return document_id


def test_search_empty_document_ids_returns_nothing(client, services, paper_one):
    _import(client, paper_one)

    assert services.documents.search("mobility", document_ids=[]) == []


def test_search_none_document_ids_covers_every_document(client, services, paper_one):
    _import(client, paper_one)

    assert services.documents.search("mobility", document_ids=None)


def test_search_named_document_ids_still_narrow(client, services, paper_one, paper_two):
    first = _import(client, paper_one, "one.pdf")
    _import(client, paper_two, "two.pdf")

    hits = services.documents.search("mobility", document_ids=[first])

    assert hits and {hit.document_id for hit in hits} == {first}
