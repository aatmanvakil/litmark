"""Document ingestion, stored extraction, summaries, and keyword search.

Extraction status and summary status are tracked separately: a summary that
never ran, or failed, must not make a perfectly readable PDF look unavailable.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InvalidInput, NotFound
from .extraction import (
    EXTRACTION_VERSION,
    DocumentExtraction,
    ExtractionError,
    PageExtraction,
    extract_pdf,
    normalize_text,
    pdf_metadata,
)
from .naming import YEAR_FROM_CREATIONDATE
from .workspace import Workspace, atomic_write_bytes, atomic_write_text, revision_of, sha256_bytes, utcnow

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
PDF_MAGIC = b"%PDF-"

# Extraction status
PENDING = "pending"
RUNNING = "running"
OK = "ok"
FAILED = "failed"

# Summary status
NONE = "none"
QUEUED = "queued"
READY = "ready"
UNAVAILABLE = "unavailable"


@dataclass
class Document:
    document_id: str
    sha256: str
    original_filename: str
    byte_size: int
    imported_at: str
    title: str | None = None
    authors: str | None = None
    year: int | None = None
    # Where `year` came from. A year read from /CreationDate is when the file
    # was produced, not when the work was published, so it is withheld from
    # the canonical filename until someone confirms it.
    year_source: str | None = None
    page_count: int | None = None
    extraction: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def display_title(self) -> str:
        return self.title or Path(self.original_filename).stem or self.document_id

    @property
    def canonical_name(self) -> str:
        """The filename this paper's PDF should carry."""
        from .naming import canonical_name

        return canonical_name(
            authors=self.authors,
            year=self.year,
            title=self.display_title,
            year_source=self.year_source,
        )

    @property
    def readable(self) -> bool:
        """The PDF can always be read once stored, whatever extraction did."""
        return True

    @property
    def searchable(self) -> bool:
        return self.extraction.get("status") == OK and self.extraction.get("quality") != "none"

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "document_id": self.document_id,
            "sha256": self.sha256,
            "original_filename": self.original_filename,
            "byte_size": self.byte_size,
            "imported_at": self.imported_at,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "year_source": self.year_source,
            "page_count": self.page_count,
            "extraction": self.extraction,
            "summary": self.summary,
        }

    def api_json(self, *, summary_text: str | None = None) -> dict[str, Any]:
        payload = self.to_json()
        payload.pop("schema_version", None)
        payload["display_title"] = self.display_title
        payload["searchable"] = self.searchable
        payload["canonical_name"] = self.canonical_name
        if summary_text is not None:
            payload["summary_text"] = summary_text
            payload["summary_revision"] = revision_of(summary_text)
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Document:
        return cls(
            document_id=data["document_id"],
            sha256=data["sha256"],
            original_filename=data.get("original_filename", "document.pdf"),
            byte_size=int(data.get("byte_size", 0)),
            imported_at=data.get("imported_at", ""),
            title=data.get("title"),
            authors=data.get("authors"),
            year=data.get("year"),
            year_source=data.get("year_source"),
            page_count=data.get("page_count"),
            extraction=data.get("extraction") or {},
            summary=data.get("summary") or {},
        )


@dataclass
class SearchHit:
    document_id: str
    document_title: str
    page_number: int
    page_label: str | None
    snippet: str
    quote: str
    score: float

    def to_json(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_title": self.document_title,
            "page_number": self.page_number,
            "page_label": self.page_label,
            "snippet": self.snippet,
            "quote": self.quote,
            "score": round(self.score, 4),
        }


class DocumentStore:
    def __init__(self, workspace: Workspace, *, max_upload_bytes: int = 64 * 1024 * 1024) -> None:
        self._workspace = workspace
        self._lock = threading.RLock()
        self._extraction_cache: dict[str, tuple[float, DocumentExtraction]] = {}
        self.max_upload_bytes = max_upload_bytes

    # ---------------------------------------------------------------- paths

    def dir_for(self, document_id: str) -> Path:
        return self._workspace.document_dir(document_id)

    def pdf_path(self, document_id: str) -> Path:
        return self.dir_for(document_id) / "original.pdf"

    def metadata_path(self, document_id: str) -> Path:
        return self.dir_for(document_id) / "metadata.json"

    def pages_path(self, document_id: str) -> Path:
        return self.dir_for(document_id) / "pages.json"

    def summary_path(self, document_id: str) -> Path:
        return self.dir_for(document_id) / "summary.md"

    # ----------------------------------------------------------- read model

    def list(self) -> list[Document]:
        documents: list[Document] = []
        root = self._workspace.documents_dir
        if not root.is_dir():
            return documents
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            metadata = entry / "metadata.json"
            if not metadata.is_file():
                continue
            try:
                documents.append(Document.from_json(json.loads(metadata.read_text("utf-8"))))
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("Skipping unreadable metadata at %s: %s", metadata, exc)
        return documents

    def get(self, document_id: str) -> Document:
        path = self._workspace.resolve_inside(self.metadata_path(document_id))
        if not path.is_file():
            raise NotFound(f"No document {document_id!r}.", document_id=document_id)
        return Document.from_json(json.loads(path.read_text("utf-8")))

    def find_by_hash(self, sha256: str) -> Document | None:
        for document in self.list():
            if document.sha256 == sha256:
                return document
        return None

    def save(self, document: Document) -> Document:
        atomic_write_text(
            self.metadata_path(document.document_id),
            json.dumps(document.to_json(), indent=2, ensure_ascii=False) + "\n",
        )
        return document

    def next_id(self) -> str:
        with self._lock:
            index = 1
            while (self._workspace.documents_dir / f"doc-{index:03d}").exists():
                index += 1
            return f"doc-{index:03d}"

    # ------------------------------------------------------------- ingestion

    def import_pdf(self, data: bytes, filename: str) -> tuple[Document, bool]:
        """Store an uploaded PDF. Returns ``(document, is_duplicate)``.

        Identical bytes resolve to the existing document rather than creating
        an accidental second copy.
        """
        if not data:
            raise InvalidInput("The uploaded file is empty.")
        if len(data) > self.max_upload_bytes:
            raise InvalidInput(
                f"{filename} is {len(data) / 1e6:.1f} MB, over the "
                f"{self.max_upload_bytes / 1e6:.0f} MB limit for this project."
            )
        if not data.lstrip()[:5].startswith(PDF_MAGIC):
            raise InvalidInput(
                f"{filename} does not look like a PDF (no %PDF header). "
                "Only PDF import is supported in this version."
            )

        digest = sha256_bytes(data)
        with self._lock:
            existing = self.find_by_hash(digest)
            if existing is not None:
                return existing, True

            document_id = self.next_id()
            directory = self._workspace.resolve_inside(self.dir_for(document_id))
            directory.mkdir(parents=True, exist_ok=True)
            # The original bytes are stored unchanged and never rewritten.
            atomic_write_bytes(directory / "original.pdf", data)

            info = pdf_metadata(directory / "original.pdf")
            document = Document(
                document_id=document_id,
                sha256=digest,
                original_filename=_safe_filename(filename),
                byte_size=len(data),
                imported_at=utcnow(),
                title=info.get("title"),
                authors=info.get("authors"),
                year=info.get("year"),
                # From /CreationDate, so not a publication year.
                year_source=YEAR_FROM_CREATIONDATE if info.get("year") else None,
                page_count=info.get("page_count"),
                extraction={
                    "status": PENDING,
                    "version": EXTRACTION_VERSION,
                    "quality": None,
                    "warnings": [],
                    "error": None,
                    "encrypted": bool(info.get("encrypted")),
                },
                summary={"status": NONE, "error": None, "generated_at": None},
            )
            return self.save(document), False

    def delete(self, document_id: str) -> None:
        import shutil

        directory = self._workspace.resolve_inside(self.dir_for(document_id))
        if not directory.is_dir():
            raise NotFound(f"No document {document_id!r}.", document_id=document_id)
        trash = self._workspace.history_dir / f"deleted-{utcnow().replace(':', '')}-{document_id}"
        shutil.move(str(directory), str(trash))
        self._extraction_cache.pop(document_id, None)

    # ------------------------------------------------------------ extraction

    def run_extraction(self, document_id: str) -> dict[str, Any]:
        """Job handler. Runs in a worker thread; never touches the event loop."""
        document = self.get(document_id)
        document.extraction = {**document.extraction, "status": RUNNING, "error": None}
        self.save(document)

        pdf = self._workspace.resolve_inside(self.pdf_path(document_id))
        try:
            extraction = extract_pdf(pdf)
        except ExtractionError as exc:
            document = self.get(document_id)
            document.extraction = {
                **document.extraction,
                "status": FAILED,
                "kind": exc.kind,
                "error": str(exc),
                "completed_at": utcnow(),
            }
            document.summary = {
                **document.summary,
                "status": UNAVAILABLE,
                "error": "No text could be extracted, so no summary can be written.",
            }
            self.save(document)
            return {"status": FAILED, "error": str(exc), "kind": exc.kind}

        atomic_write_text(
            self.pages_path(document_id),
            json.dumps(extraction.to_json(), ensure_ascii=False) + "\n",
        )
        self._extraction_cache.pop(document_id, None)

        document = self.get(document_id)
        document.page_count = extraction.page_count or document.page_count
        if not document.title:
            document.title = _guess_title(extraction)
        document.extraction = {
            "status": OK,
            "version": extraction.extraction_version,
            "quality": extraction.quality,
            "warnings": extraction.warnings,
            "empty_pages": extraction.empty_pages(),
            "error": None,
            "completed_at": utcnow(),
            "encrypted": document.extraction.get("encrypted", False),
        }
        if extraction.quality == "none":
            document.summary = {
                **document.summary,
                "status": UNAVAILABLE,
                "error": "No text could be extracted, so no summary can be written.",
            }
        self.save(document)
        return {
            "status": OK,
            "pages": extraction.page_count,
            "quality": extraction.quality,
        }

    def extraction(self, document_id: str) -> DocumentExtraction:
        """Load ``pages.json``, cached by file modification time."""
        path = self._workspace.resolve_inside(self.pages_path(document_id))
        if not path.is_file():
            raise NotFound(
                f"Document {document_id!r} has no extracted text yet.",
                document_id=document_id,
            )
        stamp = path.stat().st_mtime
        cached = self._extraction_cache.get(document_id)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        extraction = DocumentExtraction.from_json(json.loads(path.read_text("utf-8")))
        self._extraction_cache[document_id] = (stamp, extraction)
        return extraction

    def page(self, document_id: str, page_number: int) -> PageExtraction:
        from .references import find_page

        return find_page(self.extraction(document_id), page_number)

    # -------------------------------------------------------------- summary

    def summary_text(self, document_id: str) -> str | None:
        path = self._workspace.resolve_inside(self.summary_path(document_id))
        return path.read_text("utf-8") if path.is_file() else None

    def write_summary(
        self, document_id: str, text: str, *, expected_revision: str | None
    ) -> tuple[str, str | None]:
        self.get(document_id)  # Confirms the document exists.
        return self._workspace.write_versioned(
            self.summary_path(document_id),
            text,
            expected_revision=expected_revision,
            snapshot_reason="summary",
        )

    def set_summary_status(
        self, document_id: str, status: str, *, error: str | None = None
    ) -> Document:
        document = self.get(document_id)
        document.summary = {
            **document.summary,
            "status": status,
            "error": error,
            "generated_at": utcnow() if status == READY else document.summary.get("generated_at"),
        }
        return self.save(document)

    # --------------------------------------------------------------- search

    def search(
        self,
        query: str,
        *,
        document_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[SearchHit]:
        """Case-insensitive keyword search over extracted page text.

        Keyword search over the extracted text is enough for this application;
        no embedding service or vector database is involved.
        """
        terms = [t for t in re.split(r"\s+", normalize_text(query).text.strip().lower()) if t]
        if not terms:
            return []
        phrase = " ".join(terms)

        hits: list[SearchHit] = []
        for document in self.list():
            # `is not None`, not a truth test: an empty list means "no documents
            # are in scope", which must return nothing rather than everything.
            if document_ids is not None and document.document_id not in document_ids:
                continue
            if not document.searchable:
                continue
            try:
                extraction = self.extraction(document.document_id)
            except NotFound:
                continue
            for page in extraction.pages:
                text = page.text
                if not text:
                    continue
                normalized = normalize_text(text)
                lowered = normalized.lowered
                position = lowered.find(phrase)
                if position >= 0:
                    score = 2.0 + phrase.count(" ")
                    matched = len(phrase)
                else:
                    present = [t for t in terms if t in lowered]
                    if len(present) < max(1, len(terms) // 2):
                        continue
                    position = lowered.find(present[0])
                    matched = len(present[0])
                    score = len(present) / len(terms)
                raw_start = normalized.offsets[position]
                raw_end = normalized.offsets[min(position + matched, len(normalized.offsets)) - 1] + 1
                hits.append(
                    SearchHit(
                        document_id=document.document_id,
                        document_title=document.display_title,
                        page_number=page.page_number,
                        page_label=page.page_label,
                        snippet=_snippet(text, raw_start, raw_end),
                        quote=text[raw_start:raw_end],
                        score=score,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.document_id, hit.page_number))
        return hits[:limit]


def _snippet(text: str, start: int, end: int, *, window: int = 110) -> str:
    left = max(0, start - window)
    right = min(len(text), end + window)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    return prefix + " ".join(text[left:right].split()) + suffix


def _guess_title(extraction: DocumentExtraction) -> str | None:
    """Fall back to the largest text near the top of the first page."""
    if not extraction.pages:
        return None
    first = extraction.pages[0]
    for line in first.lines[:6]:
        candidate = line.text.strip()
        if 8 <= len(candidate) <= 200 and not candidate.lower().startswith(("http", "doi")):
            return candidate
    return None


def _safe_filename(filename: str) -> str:
    name = Path(filename or "document.pdf").name
    return name[:200] or "document.pdf"
