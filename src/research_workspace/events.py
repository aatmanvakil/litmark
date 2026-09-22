"""Durable event bus behind the server-sent-events endpoint.

Every event is written to SQLite first and gets a monotonic sequence ID, then
fanned out to connected subscribers. A client that reconnects sends its last
seen sequence ID and is replayed from the database, so a dropped connection
never duplicates or loses messages.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from .db import Database

# Normalized event types. Agent adapters emit these; the browser switches on them.
TEXT_DELTA = "text_delta"
TOOL_STARTED = "tool_started"
TOOL_FINISHED = "tool_finished"
FILE_PROPOSED = "file_proposed"
FILE_CHANGED = "file_changed"
ERROR = "error"
DONE = "done"
RUN_STARTED = "run_started"
MESSAGE_ADDED = "message_added"
DOCUMENT_UPDATED = "document_updated"
JOB_UPDATED = "job_updated"


class EventBus:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the serving loop so worker threads can publish safely."""
        self._loop = loop

    # ------------------------------------------------------------- publish

    def publish(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist and broadcast. Safe to call from any thread."""
        event = self._db.append_event(
            type=type,
            payload=payload or {},
            conversation_id=conversation_id,
            run_id=run_id,
        )
        self._fanout(event)
        return event

    def _fanout(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if loop is None or not self._subscribers:
            return
        if _running_loop() is loop:
            self._deliver(event)
        else:
            loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stalled client falls behind; it will replay from its last
                # sequence ID when it reconnects rather than block the server.
                self._subscribers.discard(queue)

    # ----------------------------------------------------------- subscribe

    async def stream(
        self, *, since: int = 0, conversation_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay persisted events after ``since``, then follow live ones."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2048)
        self._subscribers.add(queue)
        try:
            cursor = since
            while True:
                backlog = self._db.events_since(cursor, conversation_id=conversation_id)
                if not backlog:
                    break
                for event in backlog:
                    cursor = max(cursor, int(event["seq"]))
                    yield event

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield {"type": "ping", "seq": cursor}
                    continue
                seq = int(event.get("seq", 0))
                if seq <= cursor:
                    continue  # Already delivered during replay.
                if (
                    conversation_id is not None
                    and event.get("conversation_id") is not None
                    and event.get("conversation_id") != conversation_id
                ):
                    continue
                cursor = seq
                yield event
        finally:
            self._subscribers.discard(queue)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    with contextlib.suppress(RuntimeError):
        return asyncio.get_running_loop()
    return None


def format_sse(event: dict[str, Any]) -> str:
    """Encode an event as an SSE frame, using the sequence ID as the event ID."""
    import json

    lines = []
    seq = event.get("seq")
    if seq is not None:
        lines.append(f"id: {seq}")
    lines.append(f"event: {event.get('type', 'message')}")
    lines.append(f"data: {json.dumps(event)}")
    return "\n".join(lines) + "\n\n"
