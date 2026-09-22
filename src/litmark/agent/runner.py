"""Run scheduling, persistence, and cancellation.

Agent runs are serialized per project — including automatic summaries — and an
explicit user request always goes ahead of queued summary work. Every run's
events are persisted with durable sequence IDs before they are broadcast, so a
reconnecting browser can replay without duplicating messages or edits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections import deque
from typing import Any

from ..db import Database
from ..documents import QUEUED, READY, UNAVAILABLE, DocumentStore
from ..errors import AgentUnavailable, InvalidInput, NotFound
from ..events import DONE, ERROR, MESSAGE_ADDED, RUN_STARTED, EventBus
from ..workspace import Workspace, utcnow
from .base import AgentBackend, AgentEvent, RunContext, RunRequest
from .prompts import summary_prompt

log = logging.getLogger(__name__)

QUEUED_STATUS = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"


class AgentRunner:
    def __init__(
        self,
        *,
        db: Database,
        bus: EventBus,
        workspace: Workspace,
        documents: DocumentStore,
        backend: AgentBackend,
    ) -> None:
        self._db = db
        self._bus = bus
        self._workspace = workspace
        self._documents = documents
        self._backend = backend
        self._user_queue: deque[str] = deque()
        self._summary_queue: deque[str] = deque()
        self._pending: dict[str, tuple[str, RunContext]] = {}
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._current: tuple[str, asyncio.Task[None]] | None = None
        self._stopping = False

    @property
    def backend(self) -> AgentBackend:
        return self._backend

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the worker if a loop is running.

        Submitting outside a running loop is legitimate — a job thread can
        queue a summary — so the run simply stays queued until the serving
        loop starts and picks it up.
        """
        if self._worker is not None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._worker = asyncio.ensure_future(self._loop())

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._current is not None:
            _, task = self._current
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def recover(self) -> int:
        """Mark runs that were in flight when the process stopped."""
        rows = self._db.query(
            "SELECT id, conversation_id FROM runs WHERE status IN (?, ?)",
            (RUNNING, QUEUED_STATUS),
        )
        for row in rows:
            self._db.execute(
                "UPDATE runs SET status = ?, ended_at = ?, error = ? WHERE id = ?",
                (
                    INTERRUPTED,
                    utcnow(),
                    "The server restarted while this run was in progress.",
                    row["id"],
                ),
            )
            self._bus.publish(
                DONE,
                {
                    "terminal_reason": INTERRUPTED,
                    "message": "This run was interrupted by a server restart.",
                },
                conversation_id=row["conversation_id"],
                run_id=row["id"],
            )
        return len(rows)

    # --------------------------------------------------------- conversation

    def create_conversation(self, title: str = "Conversation") -> str:
        conversation_id = f"conv-{uuid.uuid4().hex[:12]}"
        now = utcnow()
        self._db.execute(
            "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?, ?, ?, ?)",
            (conversation_id, title, now, now),
        )
        return conversation_id

    def default_conversation(self) -> str:
        row = self._db.query_one("SELECT id FROM conversations ORDER BY created_at LIMIT 1")
        return row["id"] if row else self.create_conversation()

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if row is None:
            raise NotFound(f"No conversation {conversation_id!r}.")
        messages = self._db.query(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation_id,),
        )
        runs = self._db.query(
            "SELECT * FROM runs WHERE conversation_id = ? ORDER BY started_at DESC LIMIT 20",
            (conversation_id,),
        )
        return {
            "conversation_id": row["id"],
            "title": row["title"],
            "backend_session_id": row["backend_session_id"],
            "messages": [_message_json(m) for m in messages],
            "runs": [_run_json(r) for r in runs],
        }

    def list_conversations(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM conversations ORDER BY updated_at DESC")
        return [
            {
                "conversation_id": row["id"],
                "title": row["title"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def transcript_markdown(self, conversation_id: str) -> str:
        data = self.conversation(conversation_id)
        lines = [f"# {data['title']}", ""]
        for message in data["messages"]:
            who = "You" if message["role"] == "user" else "Assistant"
            lines.append(f"## {who} · {message['created_at']}")
            lines.append("")
            lines.append(message["content"].strip() or "_(no text)_")
            lines.append("")
        return "\n".join(lines)

    # ------------------------------------------------------------- submitting

    def submit(
        self,
        *,
        conversation_id: str,
        prompt: str,
        context: RunContext | None = None,
        kind: str = "chat",
    ) -> dict[str, Any]:
        availability = self._backend.availability()
        if not availability.ready:
            details = availability.to_json()
            details.pop("message", None)
            raise AgentUnavailable(availability.message, **details)
        if not prompt.strip():
            raise InvalidInput("An empty message cannot be sent.")

        context = context or RunContext()
        run_id = f"run-{uuid.uuid4().hex[:12]}"
        now = utcnow()

        if kind == "chat":
            message_id = f"msg-{uuid.uuid4().hex[:12]}"
            self._db.execute(
                "INSERT INTO messages(id, conversation_id, run_id, role, content, "
                "context_json, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    conversation_id,
                    run_id,
                    "user",
                    prompt,
                    json.dumps(context.to_json()),
                    now,
                ),
            )
            self._bus.publish(
                MESSAGE_ADDED,
                {
                    "message_id": message_id,
                    "role": "user",
                    "content": prompt,
                    "context": context.to_json(),
                    "created_at": now,
                },
                conversation_id=conversation_id,
                run_id=run_id,
            )

        self._db.execute(
            "INSERT INTO runs(id, conversation_id, status, kind, started_at) VALUES(?, ?, ?, ?, ?)",
            (run_id, conversation_id, QUEUED_STATUS, kind, now),
        )
        self._db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        self._pending[run_id] = (prompt, context)
        if kind == "chat":
            self._user_queue.append(run_id)
        else:
            self._summary_queue.append(run_id)
        self._wake.set()
        self.start()
        return {"run_id": run_id, "conversation_id": conversation_id, "status": QUEUED_STATUS}

    def queue_summary(self, document_id: str) -> str | None:
        """Queue an automatic summary if extraction and the agent permit it."""
        document = self._documents.get(document_id)
        if document.extraction.get("status") != "ok":
            return None
        if document.extraction.get("quality") == "none":
            self._documents.set_summary_status(
                document_id,
                UNAVAILABLE,
                error="No text could be extracted, so no summary can be written.",
            )
            return None
        if not self._backend.availability().ready:
            self._documents.set_summary_status(
                document_id,
                UNAVAILABLE,
                error="Connect an agent backend to generate summaries.",
            )
            return None

        conversation_id = self.default_conversation()
        prompt = summary_prompt(
            document_id=document_id,
            title=document.display_title,
            quality=document.extraction.get("quality"),
            warnings=document.extraction.get("warnings") or [],
        )
        self._documents.set_summary_status(document_id, QUEUED)
        result = self.submit(
            conversation_id=conversation_id,
            prompt=prompt,
            context=RunContext(document_ids=[document_id]),
            kind="summary",
        )
        return str(result["run_id"])

    async def cancel(self, run_id: str) -> dict[str, Any]:
        """Stop a run, cancelling backend work rather than hiding its output."""
        if run_id in self._user_queue:
            self._user_queue.remove(run_id)
        if run_id in self._summary_queue:
            self._summary_queue.remove(run_id)
        self._pending.pop(run_id, None)

        await self._backend.cancel_run(run_id)
        if self._current is not None and self._current[0] == run_id:
            self._current[1].cancel()
        row = self._db.query_one("SELECT status FROM runs WHERE id = ?", (run_id,))
        if row is not None and row["status"] in (QUEUED_STATUS,):
            self._finish(run_id, CANCELLED)
        return {"run_id": run_id, "status": CANCELLED}

    # ------------------------------------------------------------- the loop

    async def _loop(self) -> None:
        while not self._stopping:
            run_id = self._next_run()
            if run_id is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            task = asyncio.ensure_future(self._execute(run_id))
            self._current = (run_id, task)
            try:
                await task
            except asyncio.CancelledError:
                self._finish(run_id, CANCELLED)
            except Exception:  # noqa: BLE001 - one bad run must not kill the loop
                log.exception("Run %s crashed", run_id)
                self._finish(run_id, FAILED, error="The run crashed unexpectedly.")
            finally:
                self._current = None

    def _next_run(self) -> str | None:
        """Explicit user requests always precede queued summaries."""
        if self._user_queue:
            return self._user_queue.popleft()
        if self._summary_queue:
            return self._summary_queue.popleft()
        return None

    async def _execute(self, run_id: str) -> None:
        entry = self._pending.pop(run_id, None)
        row = self._db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
        if entry is None or row is None:
            return
        prompt, context = entry
        conversation_id = row["conversation_id"]
        kind = row["kind"]

        conversation = self._db.query_one(
            "SELECT backend_session_id FROM conversations WHERE id = ?", (conversation_id,)
        )
        session_id = conversation["backend_session_id"] if conversation else None

        self._db.execute(
            "UPDATE runs SET status = ? WHERE id = ?", (RUNNING, run_id)
        )
        self._bus.publish(
            RUN_STARTED,
            {"kind": kind, "backend": self._backend.name},
            conversation_id=conversation_id,
            run_id=run_id,
        )

        request = RunRequest(
            run_id=run_id,
            conversation_id=conversation_id,
            prompt=prompt,
            context=context,
            backend_session_id=session_id,
            kind=kind,
        )

        text_parts: list[str] = []
        errored: str | None = None
        terminal = "endTurn"
        try:
            async for event in self._backend.start_run(request):
                payload = dict(event.data)
                if event.type == "text_delta" and payload.get("final"):
                    text_parts.append(str(payload.get("text", "")))
                elif event.type == "text_delta" and not payload.get("partial"):
                    text_parts.append(str(payload.get("text", "")))
                if event.type == ERROR:
                    errored = str(payload.get("message", "The run failed."))
                if event.type == DONE:
                    terminal = str(payload.get("terminal_reason", "endTurn"))
                    new_session = payload.get("backend_session_id")
                    if new_session:
                        self._db.execute(
                            "UPDATE conversations SET backend_session_id = ? WHERE id = ?",
                            (new_session, conversation_id),
                        )
                self._bus.publish(
                    event.type, payload, conversation_id=conversation_id, run_id=run_id
                )
        except asyncio.CancelledError:
            self._persist_assistant(conversation_id, run_id, text_parts, kind)
            self._finish(run_id, CANCELLED)
            raise

        self._persist_assistant(conversation_id, run_id, text_parts, kind)
        if kind == "summary":
            self._settle_summary(context, errored)
        if errored:
            self._finish(run_id, FAILED, error=errored)
        elif terminal == "cancelled":
            self._finish(run_id, CANCELLED)
        else:
            self._finish(run_id, SUCCEEDED)

    def _persist_assistant(
        self, conversation_id: str, run_id: str, parts: list[str], kind: str
    ) -> None:
        text = "".join(parts).strip()
        if not text:
            return
        message_id = f"msg-{uuid.uuid4().hex[:12]}"
        now = utcnow()
        self._db.execute(
            "INSERT INTO messages(id, conversation_id, run_id, role, content, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (message_id, conversation_id, run_id, "assistant", text, now),
        )
        self._db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        self._bus.publish(
            MESSAGE_ADDED,
            {
                "message_id": message_id,
                "role": "assistant",
                "content": text,
                "kind": kind,
                "created_at": now,
            },
            conversation_id=conversation_id,
            run_id=run_id,
        )

    def _settle_summary(self, context: RunContext, errored: str | None) -> None:
        for document_id in context.document_ids:
            try:
                document = self._documents.get(document_id)
            except NotFound:
                continue
            if errored:
                self._documents.set_summary_status(document_id, "failed", error=errored)
            elif self._documents.summary_text(document_id):
                if document.summary.get("status") != READY:
                    self._documents.set_summary_status(document_id, READY)
            else:
                self._documents.set_summary_status(
                    document_id,
                    "failed",
                    error="The run finished without writing a summary.",
                )

    def _finish(self, run_id: str, status: str, *, error: str | None = None) -> None:
        self._db.execute(
            "UPDATE runs SET status = ?, ended_at = ?, error = ? WHERE id = ?",
            (status, utcnow(), error, run_id),
        )


def _message_json(row: Any) -> dict[str, Any]:
    return {
        "message_id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "run_id": row["run_id"],
        "context": json.loads(row["context_json"]) if row["context_json"] else None,
        "created_at": row["created_at"],
    }


def _run_json(row: Any) -> dict[str, Any]:
    return {
        "run_id": row["id"],
        "status": row["status"],
        "kind": row["kind"],
        "error": row["error"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
    }
