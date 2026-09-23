"""Wiring: one object that owns the whole project's runtime state.

Kept deliberately small — it constructs the per-domain modules, registers job
handlers, and performs restart recovery. One server process serves one project.
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from .agent.base import AgentBackend, Availability
from .agent.claude_sdk import ClaudeAgentBackend
from .agent.fake import FakeBackend
from .agent.runner import AgentRunner
from .agent.tools import ProjectTools
from .bibliography import Bibliography, build_bibliography
from .collections import CollectionStore
from .db import Database
from .documents import DocumentStore
from .events import DOCUMENT_UPDATED, EventBus
from .jobs import JobQueue
from .references import ReferenceStore, citations_by_reference
from .workspace import Workspace, atomic_write_text

log = logging.getLogger(__name__)

EXTRACT_JOB = "extract_document"


class Services:
    def __init__(self, workspace: Workspace, *, backend_name: str | None = None) -> None:
        self.workspace = workspace
        settings = workspace.settings()
        ingestion = settings.get("ingestion") or {}
        agent_settings = settings.get("agent") or {}

        self.db = Database(workspace.db_path)
        self.bus = EventBus(self.db)
        self.jobs = JobQueue(self.db, self.bus)
        self.documents = DocumentStore(
            workspace,
            max_upload_bytes=int(float(ingestion.get("max_upload_mb", 64)) * 1024 * 1024),
        )
        self.references = ReferenceStore(workspace)
        self.collections = CollectionStore(workspace)
        self.tools = ProjectTools(
            workspace, self.documents, self.references, self.db, self.collections
        )
        self.auto_summary = bool(agent_settings.get("auto_summary", True))

        self.backend_name = backend_name or str(agent_settings.get("backend", "claude"))
        self.backend: AgentBackend = self._build_backend(self.backend_name)
        self.runner = AgentRunner(
            db=self.db,
            bus=self.bus,
            workspace=workspace,
            documents=self.documents,
            backend=self.backend,
        )

        # A per-process credential so an unrelated website cannot drive the API,
        # even though the server listens on loopback.
        self.session_token = secrets.token_urlsafe(32)
        self.jobs.register(EXTRACT_JOB, self._extract_document)

    def _build_backend(self, name: str) -> AgentBackend:
        if name == "fake":
            return FakeBackend(self.tools)
        return ClaudeAgentBackend(self.tools, cwd=str(self.workspace.root))

    # ------------------------------------------------------------ lifecycle

    def bind_loop(self, loop: Any) -> None:
        self.bus.bind_loop(loop)
        self.jobs.bind_loop(loop)

    def recover(self) -> dict[str, int]:
        """Requeue interrupted jobs and mark unfinished runs."""
        return {
            "jobs": self.jobs.recover(),
            "runs": self.runner.recover(),
        }

    async def shutdown(self) -> None:
        await self.runner.stop()
        self.jobs.shutdown()
        self.db.close()

    # ---------------------------------------------------------------- jobs

    def enqueue_extraction(self, document_id: str) -> str:
        return self.jobs.enqueue(EXTRACT_JOB, {"document_id": document_id})

    def _extract_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Job handler; runs in a worker thread, off the web event loop."""
        document_id = str(payload["document_id"])
        result = self.documents.run_extraction(document_id)
        document = self.documents.get(document_id)
        self.bus.publish(
            DOCUMENT_UPDATED,
            {"document_id": document_id, "document": document.api_json()},
        )
        if result.get("status") == "ok" and self.auto_summary:
            try:
                self.runner.queue_summary(document_id)
            except Exception as exc:  # noqa: BLE001 - a summary failure is not fatal
                log.info("Automatic summary for %s not queued: %s", document_id, exc)
        return result

    # -------------------------------------------------------- bibliography

    def bibliography(self, *, cited_only: bool = False) -> Bibliography:
        """Build the BibTeX view of the project's documents and references."""
        return build_bibliography(
            self.documents.list(),
            self.references.all(),
            citations_by_reference(self.workspace, self.documents),
            cited_only=cited_only,
        )

    def write_bibliography(self, *, cited_only: bool = False) -> dict[str, Any]:
        """Write ``references.bib`` into the project directory.

        The generated text is deterministic, so regenerating an unchanged
        bibliography rewrites nothing. When it has changed, any previous file —
        including one edited by hand — is snapshotted first.
        """
        bibliography = self.bibliography(cited_only=cited_only)
        text = bibliography.to_bibtex()
        path = self.workspace.resolve_inside(self.workspace.bibliography_file)
        current = path.read_text("utf-8") if path.is_file() else None
        changed = current != text
        if changed:
            if current is not None:
                self.workspace.snapshot(path, reason="bibliography")
            atomic_write_text(path, text)
        return {
            "path": path.name,
            "entries": len(bibliography.entries),
            "written": changed,
            "warnings": bibliography.warnings,
        }

    # -------------------------------------------------------------- status

    def agent_availability(self) -> Availability:
        return self.backend.availability()

    def project_state(self) -> dict[str, Any]:
        settings = self.workspace.settings()
        documents = []
        for document in self.documents.list():
            summary = self.documents.summary_text(document.document_id)
            documents.append(document.api_json(summary_text=summary))
        return {
            "project": {
                "name": settings.get("name", self.workspace.root.name),
                "root": str(self.workspace.root),
                "schema_version": settings.get("schema_version", 1),
            },
            "agent": self.agent_availability().to_json(),
            "documents": documents,
            "notes": [note.to_json(include_text=False) for note in self.workspace.list_notes()],
            "collections": [c.api_json() for c in self.collections.list()],
            "unfiled": self.collections.unfiled([d["document_id"] for d in documents]),
            "conversations": self.runner.list_conversations(),
            "latest_seq": self.db.latest_seq(),
        }


def open_services(root: Path, *, backend_name: str | None = None) -> Services:
    workspace = Workspace(root).open()
    return Services(workspace, backend_name=backend_name)
