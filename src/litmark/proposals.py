"""Download proposals: the agent offers, a person decides.

An agent tool may write one of these; nothing else it can call fetches a
byte. The confirmation is a separate, user-initiated request, because a run
cannot pause and wait — so the offer has to outlive the run that made it, and
survive a restart.

One proposal is one *work* with all of its versions. A paper available as a
published article, an accepted manuscript and a preprint is a single decision
with three options, not three decisions; splitting it would ask the same
question three times and allow three files for one paper.

The status flow:

    pending ──confirm(version)──► processing ──success──► confirmed
                                      └──failure──► failed ──confirm──► processing

`confirmed` means the PDF was validated, imported and filed. It does not mean
a button was pressed.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import Database
from .errors import Conflict, InvalidInput, NotFound
from .workspace import utcnow

PENDING = "pending"
PROCESSING = "processing"
CONFIRMED = "confirmed"
FAILED = "failed"
DECLINED = "declined"
SUPERSEDED = "superseded"
EXPIRED = "expired"

#: Statuses a confirmation may be started from. `failed` is included so a
#: retry is possible, and it still costs a human click.
CLAIMABLE = (PENDING, FAILED)
#: Statuses that can still change, so a newer offer supersedes them.
OPEN = (PENDING, FAILED)

TTL = timedelta(minutes=30)
#: A `processing` row older than this lost its process; see `sweep_interrupted`.
STALE_PROCESSING = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Proposal:
    id: str
    status: str
    query: str
    canonical_filename: str
    versions: list[dict[str, Any]]
    doi: str | None = None
    title: str | None = None
    authors: str | None = None
    year: int | None = None
    journal: str | None = None
    conversation_id: str | None = None
    run_id: str | None = None
    after_message_id: str | None = None
    assign_collection_id: str | None = None
    chosen_version_id: str | None = None
    document_id: str | None = None
    error: str | None = None
    attempts: int = 0
    created_at: str = ""
    expires_at: str = ""

    def version(self, version_id: str) -> dict[str, Any] | None:
        for version in self.versions:
            if version.get("version_id") == version_id:
                return version
        return None

    def api_json(self) -> dict[str, Any]:
        return {
            "proposal_id": self.id,
            "status": self.status,
            "query": self.query,
            "work": {
                "title": self.title,
                "authors": self.authors,
                "year": self.year,
                "journal": self.journal,
                "doi": self.doi,
            },
            "versions": self.versions,
            "canonical_filename": self.canonical_filename,
            "assign_collection_id": self.assign_collection_id,
            "chosen_version_id": self.chosen_version_id,
            "document_id": self.document_id,
            "error": self.error,
            "attempts": self.attempts,
            "after_message_id": self.after_message_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> Proposal:
        return cls(
            id=row["id"],
            status=row["status"],
            query=row["query"],
            canonical_filename=row["canonical_filename"],
            versions=json.loads(row["versions_json"]),
            doi=row["doi"],
            title=row["title"],
            authors=row["authors"],
            year=row["year"],
            journal=row["journal"],
            conversation_id=row["conversation_id"],
            run_id=row["run_id"],
            after_message_id=row["after_message_id"],
            assign_collection_id=row["assign_collection_id"],
            chosen_version_id=row["chosen_version_id"],
            document_id=row["document_id"],
            error=row["error"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )


class ProposalStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- writing

    def create(
        self,
        *,
        query: str,
        work: dict[str, Any],
        versions: list[dict[str, Any]],
        canonical_filename: str,
        conversation_id: str | None = None,
        run_id: str | None = None,
        after_message_id: str | None = None,
        assign_collection_id: str | None = None,
    ) -> Proposal:
        if not versions:
            raise InvalidInput("A proposal needs at least one version to offer.")
        proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
        created = _now()
        doi = work.get("doi")
        if doi:
            # A newer offer for the same paper replaces any older one, so a
            # conversation cannot accumulate rival cards for one work.
            self.db.execute(
                "UPDATE proposals SET status = ?, decided_at = ? "
                "WHERE conversation_id IS ? AND doi = ? AND status IN (?, ?)",
                (SUPERSEDED, _iso(created), conversation_id, doi, *OPEN),
            )
        self.db.execute(
            "INSERT INTO proposals(id, kind, conversation_id, run_id, after_message_id,"
            " query, doi, title, authors, year, journal, versions_json,"
            " canonical_filename, assign_collection_id, status, created_at, expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                proposal_id,
                "download",
                conversation_id,
                run_id,
                after_message_id,
                query,
                doi,
                work.get("title"),
                work.get("authors"),
                work.get("year"),
                work.get("journal"),
                json.dumps(versions, ensure_ascii=False),
                canonical_filename,
                assign_collection_id,
                PENDING,
                _iso(created),
                _iso(created + TTL),
            ),
        )
        return self.get(proposal_id)

    def claim(self, proposal_id: str, version_id: str) -> Proposal:
        """Move a proposal into `processing`, or refuse.

        The conditional UPDATE is what makes a double click safe: only one
        request can match, so the loser never reaches the network.
        """
        proposal = self.get(proposal_id)
        version = proposal.version(version_id)
        if version is None:
            raise InvalidInput(
                f"{version_id!r} is not one of the versions offered for this paper.",
                proposal_id=proposal_id,
            )
        if not version.get("retrievable"):
            raise InvalidInput(
                "That version is not available to download: "
                f"{version.get('reason') or 'no direct link'}.",
                proposal_id=proposal_id,
            )
        now = _now()
        with self.db.tx() as connection:
            cursor = connection.execute(
                "UPDATE proposals SET status = ?, chosen_version_id = ?, started_at = ?,"
                " attempts = attempts + 1, error = NULL"
                " WHERE id = ? AND status IN (?, ?) AND expires_at > ?",
                (PROCESSING, version_id, _iso(now), proposal_id, *CLAIMABLE, _iso(now)),
            )
            claimed = cursor.rowcount == 1
        if not claimed:
            current = self.get(proposal_id)
            raise Conflict(
                f"That download is {current.status}, so it cannot be started again.",
                proposal_id=proposal_id,
                status=current.status,
            )
        return self.get(proposal_id)

    def succeed(self, proposal_id: str, document_id: str) -> Proposal:
        self.db.execute(
            "UPDATE proposals SET status = ?, document_id = ?, decided_at = ?, error = NULL"
            " WHERE id = ?",
            (CONFIRMED, document_id, utcnow(), proposal_id),
        )
        return self.get(proposal_id)

    def fail(self, proposal_id: str, reason: str) -> Proposal:
        self.db.execute(
            "UPDATE proposals SET status = ?, error = ?, decided_at = ? WHERE id = ?",
            (FAILED, reason, utcnow(), proposal_id),
        )
        return self.get(proposal_id)

    def decline(self, proposal_id: str) -> Proposal:
        with self.db.tx() as connection:
            cursor = connection.execute(
                "UPDATE proposals SET status = ?, decided_at = ? WHERE id = ? AND status IN (?, ?)",
                (DECLINED, utcnow(), proposal_id, *OPEN),
            )
            declined = cursor.rowcount == 1
        if not declined:
            current = self.get(proposal_id)
            raise Conflict(
                f"That download is {current.status}, so it cannot be declined.",
                proposal_id=proposal_id,
                status=current.status,
            )
        return self.get(proposal_id)

    def sweep_interrupted(self) -> int:
        """Fail rows whose process died mid-download, so they can be retried."""
        cutoff = _iso(_now() - STALE_PROCESSING)
        with self.db.tx() as connection:
            cursor = connection.execute(
                "UPDATE proposals SET status = ?, error = ? WHERE status = ? AND started_at < ?",
                (FAILED, "interrupted before it finished", PROCESSING, cutoff),
            )
            return cursor.rowcount

    # ------------------------------------------------------------- reading

    def get(self, proposal_id: str) -> Proposal:
        row = self.db.query_one("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        if row is None:
            raise NotFound(f"No proposal {proposal_id!r}.", proposal_id=proposal_id)
        proposal = Proposal.from_row(row)
        # Expiry is evaluated on read rather than by a timer, so a restarted
        # process cannot leave a stale offer looking live.
        if proposal.status == PENDING and proposal.expires_at <= _iso(_now()):
            self.db.execute(
                "UPDATE proposals SET status = ? WHERE id = ? AND status = ?",
                (EXPIRED, proposal_id, PENDING),
            )
            proposal.status = EXPIRED
        return proposal

    def for_conversation(self, conversation_id: str) -> list[Proposal]:
        rows = self.db.query(
            "SELECT id FROM proposals WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        )
        return [self.get(row["id"]) for row in rows]

    def pending(self) -> list[Proposal]:
        rows = self.db.query(
            "SELECT id FROM proposals WHERE status = ? ORDER BY created_at", (PENDING,)
        )
        live = [self.get(row["id"]) for row in rows]
        return [proposal for proposal in live if proposal.status == PENDING]
