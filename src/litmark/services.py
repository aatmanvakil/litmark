"""Wiring: one object that owns the whole project's runtime state.

Kept deliberately small — it constructs the per-domain modules, registers job
handlers, and performs restart recovery. One server process serves one project.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

from .agent.base import AgentBackend, Availability
from .agent.claude_sdk import ClaudeAgentBackend
from .agent.fake import FakeBackend
from .agent.runner import AgentRunner
from .agent.tools import ProjectTools
from .bibliography import Bibliography, build_bibliography
from .acquisition import Resolution, is_fetchable, resolve
from .acquisition.fetch import fetch_pdf
from .collections import CollectionStore
from .db import Database
from .documents import DocumentStore
from .errors import InvalidInput
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
        # Providers are injected; none is configured until a transport exists.
        self._metadata_sources: list[Any] = []
        self._version_sources: list[Any] = []
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

    def bibliography(
        self, *, cited_only: bool = False, collection_id: str | None = None
    ) -> Bibliography:
        """Build the BibTeX view of the project's documents and references.

        ``cited_only`` and ``collection_id`` compose: the first narrows to
        works something actually cites, the second to a collection's members.
        """
        documents = self.documents.list()
        if collection_id is not None:
            members = set(self.collections.member_ids(collection_id))
            documents = [d for d in documents if d.document_id in members]
        return build_bibliography(
            documents,
            self.references.all(),
            citations_by_reference(self.workspace, self.documents),
            cited_only=cited_only,
        )

    def write_bibliography(
        self, *, cited_only: bool = False, collection_id: str | None = None
    ) -> dict[str, Any]:
        """Write ``references.bib`` into the project directory.

        The generated text is deterministic, so regenerating an unchanged
        bibliography rewrites nothing. When it has changed, any previous file —
        including one edited by hand — is snapshotted first.
        """
        bibliography = self.bibliography(
            cited_only=cited_only, collection_id=collection_id
        )
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

    # -------------------------------------------------------- acquisition

    def metadata_sources(self) -> tuple[list[Any], list[Any]]:
        """Configured providers, as (metadata, version) lists.

        Empty until a transport is configured. Resolution then reports that it
        cannot reach anything, rather than inventing a result.
        """
        return list(self._metadata_sources), list(self._version_sources)

    def resolve_paper(self, query: str) -> Resolution:
        metadata, versions = self.metadata_sources()
        if not metadata:
            return Resolution(
                query=query.strip(),
                warnings=[
                    "No metadata provider is configured, so a paper cannot be "
                    "looked up. You can still import a PDF you already have."
                ],
            )
        return resolve(query, metadata_sources=metadata, version_sources=versions)

    def acquire_paper(self, query: str, url: str) -> dict[str, Any]:
        """Download a confirmed version and import it.

        The URL is not taken on trust from the request. The resolution is
        re-run and the URL must appear in *this server's* own candidates and
        pass the fetchable rules — otherwise a caller could hand the fetcher
        any address it liked by claiming a permitted source.
        """
        resolution = self.resolve_paper(query)
        chosen = None
        for candidate in resolution.candidates:
            for version in candidate.versions:
                if version.url == url and is_fetchable(version):
                    chosen = (candidate, version)
                    break
            if chosen:
                break
        if chosen is None:
            raise InvalidInput(
                "That download was not offered for this query. Resolve the "
                "paper again and choose one of the versions listed.",
                url=url,
            )

        candidate, version = chosen
        doi = candidate.work.doi
        if doi:
            for existing in self.documents.list():
                if existing.doi and existing.doi == doi:
                    return {
                        "duplicate": True,
                        "matched_on": "doi",
                        "document": existing.api_json(),
                    }

        fetched = fetch_pdf(
            url,
            max_bytes=self.documents.max_upload_bytes,
            allowed_host=urlparse(url).hostname,
        )
        document, duplicate = self.documents.import_pdf(
            fetched.data, f"{candidate.work.title or 'paper'}.pdf"
        )
        if not duplicate:
            # Confirmed metadata replaces what /Info guessed — this is how a
            # canonical name stops saying n.d.
            document = self.documents.update_metadata(
                document.document_id,
                title=candidate.work.title,
                authors=candidate.work.authors_string() or None,
                year=candidate.work.year,
                doi=doi,
            )
            self.enqueue_extraction(document.document_id)
        return {
            "duplicate": duplicate,
            "matched_on": "sha256" if duplicate else None,
            "document": document.api_json(),
            "source_url": fetched.url,
            "source_host": fetched.host,
        }

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
            "collections": self.collections.api_list(),
            "unfiled": self.collections.unfiled([d["document_id"] for d in documents]),
            "conversations": self.runner.list_conversations(),
            "latest_seq": self.db.latest_seq(),
        }


def open_services(root: Path, *, backend_name: str | None = None) -> Services:
    workspace = Workspace(root).open()
    return Services(workspace, backend_name=backend_name)
