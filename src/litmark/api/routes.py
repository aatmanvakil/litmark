"""REST resources and the resumable event stream.

Validation errors and conflicts are returned explicitly. There is deliberately
no arbitrary-path file API: notes and documents are addressed by ID.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote as percent_encode

from fastapi import APIRouter, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..acquisition.fetch import FetchRefused
from ..agent.base import RunContext
from ..agent.tools import dispatch
from ..documents import QUEUED
from ..errors import InvalidInput, NotFound, RevisionConflict
from ..events import COLLECTION_UPDATED, DOCUMENT_UPDATED, format_sse
from ..references import (
    PAGE_ONLY,
    RESOLVED,
    Reference,
    citations_by_reference,
    find_page,
    locate_quote,
    rects_for_range,
)
from ..services import Services
from ..workspace import revision_of, sha256_bytes, utcnow


def services_of(request: Request) -> Services:
    return request.app.state.services  # type: ignore[no-any-return]


_HEADER_UNSAFE = re.compile(r'[\\"\x00-\x1f\x7f]')


def _content_disposition(disposition: str, filename: str, fallback: str) -> str:
    """An RFC 6266 header: an ASCII fallback for old clients, UTF-8 for the rest.

    Starlette encodes ordinary header values as latin-1, so a filename outside
    that range raises rather than serving.
    """
    stem = unicodedata.normalize("NFKD", Path(filename).stem).encode("ascii", "ignore").decode()
    stem = re.sub(r"\s+", " ", _HEADER_UNSAFE.sub("_", stem)).strip()
    # The extension comes from the trusted fallback, never the upload: an
    # untrusted suffix can smuggle a non-ASCII character or a quote back in.
    ascii_name = stem + Path(fallback).suffix if re.search(r"[A-Za-z0-9]", stem) else fallback
    return (
        f'{disposition}; filename="{ascii_name}"; '
        f"filename*=UTF-8''{percent_encode(filename, safe='')}"
    )


def _narrow_to_collection(
    services: Services, document_ids: list[str] | None, collection_id: str | None
) -> list[str] | None:
    """Intersect an explicit document filter with a collection's membership.

    Returns ``None`` only when neither narrows anything; an empty list means
    the intersection is genuinely empty and must match nothing.
    """
    if collection_id is None:
        return document_ids
    members = services.collections.member_ids(collection_id)
    if document_ids is None:
        return members
    allowed = set(members)
    return [document_id for document_id in document_ids if document_id in allowed]


def _publish_collection(
    services: Services, collection_id: str, payload: dict[str, Any] | None
) -> None:
    """Announce a membership change. ``None`` means the collection is gone."""
    services.bus.publish(
        COLLECTION_UPDATED, {"collection_id": collection_id, "collection": payload}
    )


# ------------------------------------------------------------------ schemas


class NoteCreate(BaseModel):
    title: str = Field(default="Untitled note", max_length=200)
    text: str | None = None


class NoteSave(BaseModel):
    text: str
    expected_revision: str | None = None


class SummarySave(BaseModel):
    text: str
    expected_revision: str | None = None


class MessageSend(BaseModel):
    prompt: str
    context: dict[str, Any] | None = None


class ConversationCreate(BaseModel):
    title: str = "Conversation"


class ManualReference(BaseModel):
    """A reference created from a human PDF selection."""

    document_id: str
    page_number: int
    quote: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    start: int | None = Field(
        default=None, description="Character offset into the page's extracted text."
    )
    end: int | None = None
    allow_page_only: bool = False


class ResolveRequest(BaseModel):
    document_id: str
    page_number: int
    quote: str | None = None
    prefix: str | None = None
    suffix: str | None = None
    allow_page_only: bool = False


class UndoRequest(BaseModel):
    expected_revision: str | None = None


class BibliographyWrite(BaseModel):
    cited_only: bool = False
    collection_id: str | None = None


class DocumentMetadata(BaseModel):
    """Bibliographic corrections. Absent means unchanged; `clear` unsets."""

    title: str | None = None
    authors: str | None = None
    year: int | None = None
    clear: list[Literal["title", "authors", "year"]] = Field(default_factory=list)


class AcquisitionQuery(BaseModel):
    """A title, a pasted citation, or a DOI."""

    query: str


class AcquisitionDownload(BaseModel):
    """A confirmed version: the query it came from, and the chosen URL."""

    query: str
    url: str


class UnclaimedImport(BaseModel):
    path: str


class CollectionCreate(BaseModel):
    kind: Literal["project", "topic"]
    name: str = Field(max_length=200)
    description: str | None = None
    document_ids: list[str] = Field(default_factory=list)


class CollectionUpdate(BaseModel):
    """``kind`` is deliberately absent: reclassifying is delete plus create."""

    name: str | None = Field(default=None, max_length=200)
    description: str | None = None


class CollectionMembers(BaseModel):
    document_ids: list[str] = Field(default_factory=list)


def build_router() -> APIRouter:
    router = APIRouter()

    # -------------------------------------------------------------- project

    @router.get("/state")
    async def state(request: Request) -> dict[str, Any]:
        return services_of(request).project_state()

    @router.get("/doctor")
    async def doctor(request: Request) -> dict[str, Any]:
        services = services_of(request)
        return {
            "project": str(services.workspace.root),
            "agent": services.agent_availability().to_json(),
            "documents": len(services.documents.list()),
            "notes": len(services.workspace.list_notes()),
            "references": len(services.references.all()),
        }

    # ---------------------------------------------------------------- notes

    @router.get("/notes")
    async def list_notes(request: Request) -> dict[str, Any]:
        notes = services_of(request).workspace.list_notes()
        return {"notes": [note.to_json(include_text=False) for note in notes]}

    @router.post("/notes", status_code=201)
    async def create_note(request: Request, body: NoteCreate) -> dict[str, Any]:
        services = services_of(request)
        note = services.workspace.create_note(title=body.title, text=body.text)
        return note.to_json()

    @router.get("/notes/{note_id}")
    async def read_note(request: Request, note_id: str) -> dict[str, Any]:
        return services_of(request).workspace.read_note(note_id).to_json()

    @router.put("/notes/{note_id}")
    async def save_note(request: Request, note_id: str, body: NoteSave) -> dict[str, Any]:
        services = services_of(request)
        exists = services.workspace.note_exists(note_id)
        note = services.workspace.write_note(
            note_id,
            body.text,
            expected_revision=body.expected_revision,
            create=not exists,
            snapshot_reason="user",
        )
        services.db.execute(
            "INSERT INTO changes(id, target_kind, target_id, origin, summary, "
            "base_revision, new_revision, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (
                # A change ID must be unique per *edit*, not per resulting
                # content: deriving it from the revision hash made saving a
                # note back to text it previously held collide with the earlier
                # change and fail the write with an integrity error.
                f"chg-{uuid.uuid4().hex[:12]}",
                "note",
                note_id,
                "user",
                "Saved from the editor",
                body.expected_revision,
                note.revision,
                utcnow(),
            ),
        )
        return note.to_json()

    @router.delete("/notes/{note_id}", status_code=204)
    async def delete_note(
        request: Request, note_id: str, expected_revision: str | None = None
    ) -> Response:
        services_of(request).workspace.delete_note(
            note_id, expected_revision=expected_revision
        )
        return Response(status_code=204)

    # ------------------------------------------------------------ documents

    @router.post("/documents", status_code=201)
    async def upload_documents(
        request: Request,
        files: Annotated[list[UploadFile], File()],
    ) -> dict[str, Any]:
        services = services_of(request)
        imported: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for upload in files:
            data = await upload.read()
            try:
                document, duplicate = services.documents.import_pdf(
                    data, upload.filename or "document.pdf"
                )
            except InvalidInput as exc:
                errors.append({"filename": upload.filename, "message": exc.message})
                continue
            if not duplicate:
                # The PDF is readable immediately; extraction runs in the background.
                services.enqueue_extraction(document.document_id)
            imported.append(
                {
                    "document": document.api_json(
                        summary_text=services.documents.summary_text(document.document_id)
                    ),
                    "duplicate": duplicate,
                }
            )
        return {"imported": imported, "errors": errors}

    @router.get("/documents")
    async def list_documents(request: Request) -> dict[str, Any]:
        services = services_of(request)
        return {
            "documents": [
                document.api_json(
                    summary_text=services.documents.summary_text(document.document_id)
                )
                for document in services.documents.list()
            ]
        }

    @router.get("/documents/{document_id}")
    async def get_document(request: Request, document_id: str) -> dict[str, Any]:
        services = services_of(request)
        document = services.documents.get(document_id)
        return document.api_json(summary_text=services.documents.summary_text(document_id))

    @router.patch("/documents/{document_id}")
    async def update_document(
        request: Request, document_id: str, body: DocumentMetadata
    ) -> dict[str, Any]:
        """Correct title, authors or publication year.

        Without this the canonical name is stuck at whatever the PDF claimed,
        and a year the file never stated could never be supplied.
        """
        services = services_of(request)
        document = services.documents.update_metadata(
            document_id,
            title=body.title,
            authors=body.authors,
            year=body.year,
            clear=tuple(body.clear),
        )
        payload = document.api_json(
            summary_text=services.documents.summary_text(document_id)
        )
        services.bus.publish(
            DOCUMENT_UPDATED, {"document_id": document_id, "document": payload}
        )
        return payload

    @router.get("/documents/{document_id}/pdf")
    async def get_pdf(request: Request, document_id: str) -> Response:
        services = services_of(request)
        document = services.documents.get(document_id)
        path = services.workspace.resolve_inside(services.documents.pdf_path(document_id))
        return Response(
            content=path.read_bytes(),
            media_type="application/pdf",
            headers={
                "content-disposition": _content_disposition(
                    "inline", document.canonical_name, "document.pdf"
                ),
                "cache-control": "private, max-age=3600",
                "etag": f'"{document.sha256}"',
            },
        )

    @router.get("/documents/{document_id}/pages")
    async def get_pages(
        request: Request,
        document_id: str,
        page: Annotated[list[int] | None, Query()] = None,
    ) -> dict[str, Any]:
        services = services_of(request)
        extraction = services.documents.extraction(document_id)
        wanted = set(page or [])
        pages = [
            {
                "page_number": p.page_number,
                "page_label": p.page_label,
                "width": p.width,
                "height": p.height,
                "rotation": p.rotation,
                "warnings": p.warnings,
                "text": p.text,
            }
            for p in extraction.pages
            if not wanted or p.page_number in wanted
        ]
        return {
            "document_id": document_id,
            "page_count": extraction.page_count,
            "quality": extraction.quality,
            "warnings": extraction.warnings,
            "pages": pages,
        }

    @router.get("/documents/{document_id}/summary")
    async def get_summary(request: Request, document_id: str) -> dict[str, Any]:
        services = services_of(request)
        document = services.documents.get(document_id)
        text = services.documents.summary_text(document_id)
        return {
            "document_id": document_id,
            "text": text,
            "revision": revision_of(text) if text is not None else None,
            "status": document.summary.get("status"),
            "error": document.summary.get("error"),
            "extraction_quality": document.extraction.get("quality"),
            "extraction_warnings": document.extraction.get("warnings") or [],
        }

    @router.put("/documents/{document_id}/summary")
    async def save_summary(
        request: Request, document_id: str, body: SummarySave
    ) -> dict[str, Any]:
        services = services_of(request)
        revision, _previous = services.documents.write_summary(
            document_id, body.text, expected_revision=body.expected_revision
        )
        services.documents.set_summary_status(document_id, "ready")
        return {"document_id": document_id, "revision": revision}

    @router.post("/documents/{document_id}/extract")
    async def reextract(request: Request, document_id: str) -> dict[str, Any]:
        services = services_of(request)
        services.documents.get(document_id)
        return {"job_id": services.enqueue_extraction(document_id)}

    @router.post("/documents/{document_id}/summarize")
    async def summarize(request: Request, document_id: str) -> dict[str, Any]:
        services = services_of(request)
        run_id = services.runner.queue_summary(document_id)
        if run_id is None:
            document = services.documents.get(document_id)
            return {
                "queued": False,
                "reason": document.summary.get("error")
                or "A summary cannot be generated for this document yet.",
            }
        return {"queued": True, "run_id": run_id, "status": QUEUED}

    @router.delete("/documents/{document_id}", status_code=200)
    async def delete_document(request: Request, document_id: str) -> dict[str, Any]:
        services = services_of(request)
        services.documents.delete(document_id)
        # References survive as a recoverable missing-source state.
        touched = services.references.mark_missing(document_id)
        # A membership carries nothing but the pair, so it is pruned rather
        # than tombstoned.
        released = services.collections.forget_document(document_id)
        return {
            "deleted": document_id,
            "references_marked_missing": touched,
            "collections_updated": released,
        }

    # --------------------------------------------------------- acquisition

    @router.post("/acquisition/resolve")
    async def resolve_acquisition(
        request: Request, body: AcquisitionQuery
    ) -> dict[str, Any]:
        """Candidate works for a query.

        Writes nothing: no document, no Papers/ entry, no metadata, no
        reference. Abandoning the result leaves the project byte-identical.
        """
        services = services_of(request)
        return services.resolve_paper(body.query).to_json()

    @router.post("/acquisition/download", status_code=201)
    async def download_acquisition(
        request: Request, body: AcquisitionDownload
    ) -> dict[str, Any]:
        """Fetch a confirmed version and import it."""
        services = services_of(request)
        try:
            return services.acquire_paper(body.query, body.url)
        except FetchRefused as exc:
            raise InvalidInput(
                f"The download was refused: {exc.reason}.", **exc.details
            ) from exc

    # -------------------------------------------------------------- papers

    @router.get("/papers/unclaimed")
    async def unclaimed_papers(request: Request) -> dict[str, Any]:
        """PDFs in Papers/ that no document points at.

        Listed, never imported automatically: a stray file is as likely to be
        a sync artefact as something the user wants in their library.
        """
        return {"unclaimed": services_of(request).documents.unclaimed_papers()}

    @router.post("/papers/unclaimed/import", status_code=201)
    async def import_unclaimed(request: Request, body: UnclaimedImport) -> dict[str, Any]:
        services = services_of(request)
        source = services.workspace.resolve_inside(body.path)
        if not source.is_file() or source.parent != services.workspace.papers_dir:
            raise NotFound(f"No unclaimed paper at {body.path!r}.", path=body.path)
        data = source.read_bytes()
        document, duplicate = services.documents.import_pdf(data, source.name)
        if not duplicate:
            # import_pdf wrote its own canonical copy, so the loose original
            # would otherwise remain as a second file with the same bytes.
            stored, _ = services.documents.resolve_pdf(document.document_id)
            if stored is not None and stored != source:
                source.unlink(missing_ok=True)
            services.enqueue_extraction(document.document_id)
        return {
            "document": document.api_json(),
            "duplicate": duplicate,
        }

    @router.post("/documents/{document_id}/pdf", status_code=200)
    async def relink_pdf(
        request: Request,
        document_id: str,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        """Restore a missing source by re-supplying its bytes."""
        services = services_of(request)
        document = services.documents.get(document_id)
        data = await file.read()
        digest = sha256_bytes(data)
        if digest != document.sha256:
            raise InvalidInput(
                "Those bytes are a different document. Import them as a new "
                "paper instead; changing a document's PDF would silently move "
                "its existing references onto different content.",
                document_id=document_id,
            )
        restored = services.documents.place_pdf(document, data)
        return restored.api_json()

    # ---------------------------------------------------------- collections

    def _known_documents(services: Services, document_ids: list[str]) -> list[str]:
        for document_id in document_ids:
            services.documents.get(document_id)
        return document_ids

    @router.get("/collections")
    async def list_collections(request: Request) -> dict[str, Any]:
        services = services_of(request)
        documents = [d.document_id for d in services.documents.list()]
        return {
            "collections": [c.api_json() for c in services.collections.list()],
            "unfiled": services.collections.unfiled(documents),
        }

    @router.post("/collections", status_code=201)
    async def create_collection(
        request: Request, body: CollectionCreate
    ) -> dict[str, Any]:
        services = services_of(request)
        _known_documents(services, body.document_ids)
        collection = services.collections.create(
            kind=body.kind,
            name=body.name,
            description=body.description,
            documents=body.document_ids,
        )
        _publish_collection(services, collection.collection_id, collection.api_json())
        return collection.api_json()

    @router.patch("/collections/{collection_id}")
    async def update_collection(
        request: Request, collection_id: str, body: CollectionUpdate
    ) -> dict[str, Any]:
        services = services_of(request)
        collection = services.collections.update(
            collection_id, name=body.name, description=body.description
        )
        _publish_collection(services, collection_id, collection.api_json())
        return collection.api_json()

    @router.delete("/collections/{collection_id}")
    async def delete_collection(request: Request, collection_id: str) -> dict[str, Any]:
        services = services_of(request)
        released = services.collections.delete(collection_id)
        _publish_collection(services, collection_id, None)
        # The papers themselves are untouched; only the classification is gone.
        return {"deleted": collection_id, "documents_released": released}

    @router.put("/collections/{collection_id}/documents")
    async def set_collection_documents(
        request: Request, collection_id: str, body: CollectionMembers
    ) -> dict[str, Any]:
        services = services_of(request)
        _known_documents(services, body.document_ids)
        collection = services.collections.set_documents(collection_id, body.document_ids)
        _publish_collection(services, collection_id, collection.api_json())
        return collection.api_json()

    @router.post("/collections/{collection_id}/documents")
    async def add_collection_documents(
        request: Request, collection_id: str, body: CollectionMembers
    ) -> dict[str, Any]:
        services = services_of(request)
        _known_documents(services, body.document_ids)
        collection = services.collections.add_documents(collection_id, body.document_ids)
        _publish_collection(services, collection_id, collection.api_json())
        return collection.api_json()

    @router.delete("/collections/{collection_id}/documents/{document_id}")
    async def remove_collection_document(
        request: Request, collection_id: str, document_id: str
    ) -> dict[str, Any]:
        services = services_of(request)
        before = services.collections.get(collection_id).documents
        collection = services.collections.remove_document(collection_id, document_id)
        _publish_collection(services, collection_id, collection.api_json())
        return {
            "collection_id": collection_id,
            "document_id": document_id,
            "removed": document_id in before,
        }

    # --------------------------------------------------------------- search

    @router.get("/search")
    async def search(
        request: Request,
        q: str,
        document_id: Annotated[list[str] | None, Query()] = None,
        collection_id: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        services = services_of(request)
        scope = _narrow_to_collection(services, document_id, collection_id)
        hits = services.documents.search(q, document_ids=scope, limit=limit)
        notes = []
        needle = q.strip().lower()
        if needle:
            for note in services.workspace.list_notes():
                position = note.text.lower().find(needle)
                if position >= 0:
                    notes.append(
                        {
                            "note_id": note.note_id,
                            "title": note.title,
                            "snippet": " ".join(
                                note.text[max(0, position - 80) : position + 120].split()
                            ),
                        }
                    )
        return {"query": q, "passages": [hit.to_json() for hit in hits], "notes": notes}

    # ----------------------------------------------------------- references

    @router.get("/references")
    async def list_references(
        request: Request, document_id: str | None = None
    ) -> dict[str, Any]:
        """Every reference, with where each one is cited from.

        The source panel draws all of a document's marks at once and colours
        them by origin, so it needs the citing notes alongside the geometry.
        """
        services = services_of(request)
        registry = services.references.all()
        citations = citations_by_reference(services.workspace, services.documents)
        references = [
            {**reference.api_json(), "cited_by": citations.get(reference_id, [])}
            for reference_id, reference in registry.items()
            if document_id is None or reference.document_id == document_id
        ]
        references.sort(key=lambda item: (item["page_number"], item["reference_id"]))
        return {"references": references}

    @router.get("/references/{reference_id}")
    async def get_reference(request: Request, reference_id: str) -> dict[str, Any]:
        services = services_of(request)
        reference = services.references.get(reference_id)
        payload = reference.api_json()
        try:
            document = services.documents.get(reference.document_id)
        except NotFound:
            payload["missing_source"] = True
            payload["message"] = (
                "The cited document is no longer in this project. The reference is "
                "preserved so it can be recovered."
            )
            return payload
        payload["document_title"] = document.display_title
        payload["document_sha256_matches"] = document.sha256 == reference.document_sha256
        if not payload["document_sha256_matches"]:
            payload["message"] = (
                "The stored PDF's bytes differ from the ones this reference was made "
                "against; the highlight may no longer be in the right place."
            )
        return payload

    @router.post("/references", status_code=201)
    async def create_reference(request: Request, body: ManualReference) -> dict[str, Any]:
        """Create a reference from a human PDF selection on one page.

        The browser sends the offsets of the selected characters in the page's
        extracted text; geometry is computed here so CSS pixels are never stored.
        """
        services = services_of(request)
        document = services.documents.get(body.document_id)
        page = find_page(services.documents.extraction(body.document_id), body.page_number)

        rects: list[list[float]] = []
        quote = body.quote
        status = RESOLVED
        message: str | None = None

        if body.start is not None and body.end is not None and body.end > body.start:
            rects = rects_for_range(page, body.start, body.end)
            quote = page.text[body.start : body.end]
        elif quote:
            match = locate_quote(page, quote, prefix=body.prefix, suffix=body.suffix)
            if match.ok:
                rects = match.rects
                quote = match.quote
            elif body.allow_page_only:
                status, message = PAGE_ONLY, match.message
            else:
                return JSONResponse(
                    {"error": {"code": match.status, "message": match.message}},
                    status_code=422,
                )
        elif body.allow_page_only:
            status, message = PAGE_ONLY, "Page-only citation."
        else:
            raise InvalidInput("Supply a selection range, a quotation, or allow_page_only.")

        if not rects and status == RESOLVED:
            status = PAGE_ONLY
            message = "The selection produced no usable geometry; cited the page only."

        reference = Reference(
            reference_id=services.references.allocate_id(),
            document_id=body.document_id,
            document_sha256=document.sha256,
            page_number=page.page_number,
            page_label=page.page_label,
            status=status,
            quote=quote,
            prefix=body.prefix,
            suffix=body.suffix,
            rects=rects,
            note=message,
            created_at=utcnow(),
        )
        services.references.put(reference)
        label = f"{document.display_title}, p. {page.page_label or page.page_number}"
        return {
            **reference.api_json(),
            "markdown_link": f"[{label}](source:{reference.reference_id})",
            "message": message,
        }

    @router.post("/references/resolve")
    async def resolve_reference(request: Request, body: ResolveRequest) -> dict[str, Any]:
        services = services_of(request)
        # Deliberately unscoped: this is the human selecting a passage in the
        # viewer, not the agent reaching for a document. Scoping it would break
        # manual selection whenever a collection happened to be active.
        return dispatch(services.tools, "resolve_source", body.model_dump(), scope=None)

    # --------------------------------------------------------- bibliography

    # ``response_model=None``: this route answers with either JSON or the
    # ``.bib`` file itself, which is not one Pydantic response shape.
    @router.get("/bibliography", response_model=None)
    async def get_bibliography(
        request: Request,
        cited_only: bool = False,
        format: str = "json",
        collection_id: str | None = None,
    ) -> Response | dict[str, Any]:
        """The BibTeX view of the project, as JSON or as the file itself.

        ``cited_only`` narrows it to works a note or summary actually cites.
        """
        services = services_of(request)
        bibliography = services.bibliography(
            cited_only=cited_only, collection_id=collection_id
        )
        if format == "bibtex":
            return PlainTextResponse(
                bibliography.to_bibtex(),
                media_type="application/x-bibtex; charset=utf-8",
                headers={"content-disposition": 'attachment; filename="references.bib"'},
            )
        return bibliography.api_json()

    @router.post("/bibliography")
    async def write_bibliography(
        request: Request, body: BibliographyWrite
    ) -> dict[str, Any]:
        """Write ``references.bib`` into the project directory."""
        return services_of(request).write_bibliography(
            cited_only=body.cited_only, collection_id=body.collection_id
        )

    # -------------------------------------------------------- conversations

    @router.get("/conversations")
    async def list_conversations(request: Request) -> dict[str, Any]:
        return {"conversations": services_of(request).runner.list_conversations()}

    @router.post("/conversations", status_code=201)
    async def create_conversation(
        request: Request, body: ConversationCreate
    ) -> dict[str, Any]:
        services = services_of(request)
        conversation_id = services.runner.create_conversation(body.title)
        return services.runner.conversation(conversation_id)

    @router.get("/conversations/{conversation_id}")
    async def get_conversation(request: Request, conversation_id: str) -> dict[str, Any]:
        return services_of(request).runner.conversation(conversation_id)

    @router.get("/conversations/{conversation_id}/export")
    async def export_conversation(
        request: Request, conversation_id: str, format: str = "markdown"
    ) -> Response:
        services = services_of(request)
        if format == "json":
            payload = services.runner.conversation(conversation_id)
            return Response(
                content=json.dumps(payload, indent=2, ensure_ascii=False),
                media_type="application/json",
                headers={
                    "content-disposition": f'attachment; filename="{conversation_id}.json"'
                },
            )
        return PlainTextResponse(
            services.runner.transcript_markdown(conversation_id),
            headers={"content-disposition": f'attachment; filename="{conversation_id}.md"'},
        )

    @router.post("/conversations/{conversation_id}/messages", status_code=202)
    async def send_message(
        request: Request, conversation_id: str, body: MessageSend
    ) -> dict[str, Any]:
        services = services_of(request)
        services.runner.conversation(conversation_id)  # Confirms it exists.
        context = RunContext.from_json(body.context)
        if context.collection_id:
            # Resolved here, from the registry, rather than trusted from the
            # client: a caller must not be able to widen its own scope. An
            # unknown collection is a 404 before the run starts.
            collection = services.collections.get(context.collection_id)
            context.collection_name = collection.name
            context.scope_document_ids = list(collection.documents)
        return services.runner.submit(
            conversation_id=conversation_id,
            prompt=body.prompt,
            context=context,
        )

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str) -> dict[str, Any]:
        return await services_of(request).runner.cancel(run_id)

    # -------------------------------------------------------------- changes

    @router.get("/changes")
    async def list_changes(request: Request, limit: int = 50) -> dict[str, Any]:
        rows = services_of(request).db.query(
            "SELECT * FROM changes ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        )
        return {
            "changes": [
                {
                    "change_id": row["id"],
                    "target_kind": row["target_kind"],
                    "target_id": row["target_id"],
                    "origin": row["origin"],
                    "summary": row["summary"],
                    "base_revision": row["base_revision"],
                    "new_revision": row["new_revision"],
                    "undoable": bool(row["snapshot_path"]) and not row["undone_at"],
                    "undone_at": row["undone_at"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ]
        }

    @router.post("/changes/{change_id}/undo")
    async def undo_change(
        request: Request, change_id: str, body: UndoRequest
    ) -> dict[str, Any]:
        """Undo is itself version-checked, so it cannot remove later edits."""
        services = services_of(request)
        row = services.db.query_one("SELECT * FROM changes WHERE id = ?", (change_id,))
        if row is None:
            raise NotFound(f"No change {change_id!r}.")
        if row["undone_at"]:
            raise InvalidInput("That change has already been undone.")
        if not row["snapshot_path"]:
            raise InvalidInput(
                "This change has no previous content to restore (it created the file)."
            )
        snapshot = services.workspace.resolve_inside(row["snapshot_path"])
        if not snapshot.is_file():
            raise NotFound("The recovery snapshot for this change is missing.")

        previous_text = snapshot.read_text("utf-8")
        expected = body.expected_revision or row["new_revision"]
        if row["target_kind"] == "note":
            note = services.workspace.write_note(
                row["target_id"],
                previous_text,
                expected_revision=expected,
                snapshot_reason="undo",
            )
            new_revision = note.revision
        else:
            new_revision, _ = services.documents.write_summary(
                row["target_id"], previous_text, expected_revision=expected
            )
        services.db.execute(
            "UPDATE changes SET undone_at = ? WHERE id = ?", (utcnow(), change_id)
        )
        return {"change_id": change_id, "revision": new_revision, "undone": True}

    # --------------------------------------------------------------- events

    @router.get("/events")
    async def events(
        request: Request,
        since: int = 0,
        conversation_id: str | None = None,
    ) -> StreamingResponse:
        services = services_of(request)
        last_event_id = request.headers.get("last-event-id")
        if last_event_id and last_event_id.isdigit():
            since = max(since, int(last_event_id))

        async def stream():  # type: ignore[no-untyped-def]
            # Open the response immediately. Without this the headers are not
            # flushed until the first event, so on a quiet project the browser
            # sits in EventSource's CONNECTING state — and shows "Reconnecting…"
            # — until the 15-second keepalive finally arrives.
            yield ": open\n\nretry: 2000\n\n"
            async for event in services.bus.stream(
                since=since, conversation_id=conversation_id
            ):
                if await request.is_disconnected():
                    break
                yield format_sse(event)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "cache-control": "no-cache, no-transform",
                "connection": "keep-alive",
                "x-accel-buffering": "no",
            },
        )

    return router


__all__ = ["build_router", "RevisionConflict"]
