"""The offer-then-confirm lifecycle.

`confirmed` must mean the PDF was validated, imported and filed — not that a
button was pressed. And two clicks must produce one file, which is a
concurrency property, so the double-confirm test uses real threads: two
sequential calls would pass even against a read-then-write claim.
"""

from __future__ import annotations

import json
import threading

import pytest

from litmark.errors import Conflict, InvalidInput, NotFound
from litmark.proposals import (
    CONFIRMED,
    DECLINED,
    EXPIRED,
    FAILED,
    PENDING,
    PROCESSING,
    SUPERSEDED,
    ProposalStore,
)

WORK = {
    "title": "Exchange Rate Disconnect",
    "authors": "Anna Müller and Bo Lindqvist",
    "year": 2021,
    "journal": "JPE",
    "doi": "10.1234/exchange.2021",
}

VERSIONS = [
    {
        "version_id": "v1",
        "version_type": "published",
        "url": "https://journals.example.org/a.pdf",
        "host": "publisher",
        "license": "cc-by",
        "retrievable": True,
        "source": "unpaywall",
        "reason": None,
    },
    {
        "version_id": "v2",
        "version_type": "accepted_manuscript",
        "url": "https://eprints.example.ac.uk/a.pdf",
        "host": "repository",
        "license": None,
        "retrievable": True,
        "source": "unpaywall",
        "reason": None,
    },
    {
        "version_id": "v3",
        "version_type": "published",
        "url": "https://doi.org/10.1234/exchange.2021",
        "host": "publisher",
        "license": None,
        "retrievable": False,
        "source": "unpaywall",
        "reason": "paywalled",
    },
]


@pytest.fixture
def store(services):
    return ProposalStore(services.db)


def make(store, **overrides):
    payload = {
        "query": "exchange rate",
        "work": WORK,
        "versions": VERSIONS,
        "canonical_filename": "Müller, Lindqvist (2021) – Exchange Rate Disconnect.pdf",
        "conversation_id": "conv-1",
    }
    payload.update(overrides)
    return store.create(**payload)


# ------------------------------------------------------ one work, all versions


def test_one_paper_is_one_proposal_with_every_version(store):
    proposal = make(store)

    assert proposal.status == PENDING
    assert [v["version_id"] for v in proposal.versions] == ["v1", "v2", "v3"]
    assert proposal.canonical_filename.endswith(".pdf")
    assert proposal.work_doi() if hasattr(proposal, "work_doi") else proposal.doi


def test_a_non_retrievable_version_is_kept_and_explained(store):
    proposal = make(store)

    paywalled = proposal.version("v3")

    assert paywalled["retrievable"] is False
    assert paywalled["reason"] == "paywalled"


def test_a_proposal_needs_at_least_one_version(store):
    with pytest.raises(InvalidInput):
        make(store, versions=[])


def test_a_newer_offer_supersedes_an_older_one_for_the_same_paper(store):
    first = make(store)

    make(store)

    assert store.get(first.id).status == SUPERSEDED


def test_a_different_paper_does_not_supersede(store):
    first = make(store)

    make(store, work={**WORK, "doi": "10.5555/other"})

    assert store.get(first.id).status == PENDING


# ------------------------------------------------- the version must belong


def test_an_unknown_version_is_refused(store):
    proposal = make(store)

    with pytest.raises(InvalidInput) as caught:
        store.claim(proposal.id, "v99")

    assert "not one of the versions" in caught.value.message
    assert store.get(proposal.id).status == PENDING


def test_a_non_retrievable_version_cannot_be_claimed(store):
    proposal = make(store)

    with pytest.raises(InvalidInput) as caught:
        store.claim(proposal.id, "v3")

    assert "paywalled" in caught.value.message
    assert store.get(proposal.id).status == PENDING


# ----------------------------------------------------------------- lifecycle


def test_claiming_moves_to_processing_not_confirmed(store):
    proposal = make(store)

    claimed = store.claim(proposal.id, "v1")

    # The bytes have not been fetched yet, so this is emphatically not done.
    assert claimed.status == PROCESSING
    assert claimed.chosen_version_id == "v1"
    assert claimed.attempts == 1


def test_only_success_reaches_confirmed(store):
    proposal = make(store)
    store.claim(proposal.id, "v1")

    settled = store.succeed(proposal.id, "doc-001")

    assert settled.status == CONFIRMED
    assert settled.document_id == "doc-001"


def test_failure_records_a_reason_and_no_document(store):
    proposal = make(store)
    store.claim(proposal.id, "v1")

    failed = store.fail(proposal.id, "blocked_address")

    assert failed.status == FAILED
    assert failed.error == "blocked_address"
    assert failed.document_id is None


def test_a_failed_proposal_can_be_retried_and_counts_attempts(store):
    proposal = make(store)
    store.claim(proposal.id, "v1")
    store.fail(proposal.id, "timeout")

    retried = store.claim(proposal.id, "v2")

    assert retried.status == PROCESSING
    assert retried.attempts == 2
    assert retried.chosen_version_id == "v2"
    assert retried.error is None


def test_a_confirmed_proposal_cannot_be_retried(store):
    proposal = make(store)
    store.claim(proposal.id, "v1")
    store.succeed(proposal.id, "doc-001")

    with pytest.raises(Conflict):
        store.claim(proposal.id, "v2")


def test_declining_closes_it(store):
    proposal = make(store)

    declined = store.decline(proposal.id)

    assert declined.status == DECLINED
    with pytest.raises(Conflict):
        store.claim(proposal.id, "v1")


def test_an_expired_proposal_reports_itself_expired(store, monkeypatch):
    proposal = make(store)
    services_db = store.db
    services_db.execute(
        "UPDATE proposals SET expires_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", proposal.id),
    )

    assert store.get(proposal.id).status == EXPIRED
    with pytest.raises(Conflict):
        store.claim(proposal.id, "v1")


def test_an_interrupted_download_is_swept_and_retryable(store):
    proposal = make(store)
    store.claim(proposal.id, "v1")
    store.db.execute(
        "UPDATE proposals SET started_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00Z", proposal.id),
    )

    swept = store.sweep_interrupted()

    assert swept == 1
    recovered = store.get(proposal.id)
    assert recovered.status == FAILED
    assert "interrupted" in recovered.error
    assert store.claim(proposal.id, "v1").status == PROCESSING


# -------------------------------------------------------------- concurrency


def test_two_concurrent_confirmations_produce_one_claim(store):
    """Real threads: sequential calls would pass a read-then-write claim."""
    proposal = make(store)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        try:
            store.claim(proposal.id, "v1")
            with lock:
                outcomes.append("claimed")
        except Conflict:
            with lock:
                outcomes.append("conflict")

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["claimed", "conflict"], outcomes
    assert store.get(proposal.id).attempts == 1


def test_many_concurrent_confirmations_still_claim_once(store):
    proposal = make(store)
    barrier = threading.Barrier(8)
    claims = []
    lock = threading.Lock()

    def attempt():
        barrier.wait()
        try:
            store.claim(proposal.id, "v1")
            with lock:
                claims.append(1)
        except Conflict:
            pass

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(claims) == 1
    assert store.get(proposal.id).attempts == 1


# --------------------------------------------------------------- durability


def test_a_proposal_survives_a_restart(tmp_path, services, store):
    """A run cannot pause, so the offer must outlive the process."""
    proposal = make(store)
    reopened = ProposalStore(services.db)

    found = reopened.get(proposal.id)

    assert found.status == PENDING
    assert [v["version_id"] for v in found.versions] == ["v1", "v2", "v3"]


def test_proposals_are_listed_for_their_conversation(store):
    make(store, conversation_id="conv-1")
    make(store, conversation_id="conv-2", work={**WORK, "doi": "10.5555/two"})

    assert len(store.for_conversation("conv-1")) == 1
    assert len(store.for_conversation("conv-2")) == 1


def test_an_unknown_proposal_is_a_not_found(store):
    with pytest.raises(NotFound):
        store.get("prop-nope")


# --------------------------------------------------------------------- http


def test_the_confirm_route_refuses_a_foreign_version(client, services):
    proposal = make(ProposalStore(services.db))

    response = client.post(
        f"/api/proposals/{proposal.id}/confirm", json={"version_id": "v99"}
    )

    assert response.status_code == 422
    assert client.get("/api/documents").json()["documents"] == []


def test_the_confirm_route_conflicts_on_a_decided_proposal(client, services):
    store = ProposalStore(services.db)
    proposal = make(store)
    store.decline(proposal.id)

    response = client.post(
        f"/api/proposals/{proposal.id}/confirm", json={"version_id": "v1"}
    )

    assert response.status_code == 409
    assert "declined" in response.text


def test_the_conversation_carries_its_proposals(client, services):
    conversation = client.post("/api/conversations", json={"title": "Chat"}).json()
    make(ProposalStore(services.db), conversation_id=conversation["conversation_id"])

    payload = client.get(
        f"/api/conversations/{conversation['conversation_id']}"
    ).json()

    assert len(payload["proposals"]) == 1
    assert payload["proposals"][0]["status"] == PENDING
    assert len(payload["proposals"][0]["versions"]) == 3


def test_pending_proposals_are_listed(client, services):
    make(ProposalStore(services.db))

    listed = client.get("/api/proposals").json()["proposals"]

    assert len(listed) == 1
    assert listed[0]["canonical_filename"].endswith(".pdf")
