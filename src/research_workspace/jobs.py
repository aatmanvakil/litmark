"""Single-process background job queue with SQLite recovery.

CPU-heavy work (PDF extraction) must not run on the web event loop, and a job
must survive a server restart. That is the whole requirement here — no Redis,
Celery, or separate database server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .db import Database
from .events import JOB_UPDATED, EventBus
from .workspace import utcnow

log = logging.getLogger(__name__)

JobHandler = Callable[[dict[str, Any]], dict[str, Any] | None]

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

MAX_ATTEMPTS = 3


class JobQueue:
    def __init__(self, db: Database, bus: EventBus, *, workers: int = 2) -> None:
        self._db = db
        self._bus = bus
        self._handlers: dict[str, JobHandler] = {}
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rw-job")
        self._pending: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(self, kind: str, handler: JobHandler) -> None:
        self._handlers[kind] = handler

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ------------------------------------------------------------- enqueue

    def enqueue(self, kind: str, payload: dict[str, Any]) -> str:
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        now = utcnow()
        self._db.execute(
            "INSERT INTO jobs(id, kind, payload_json, status, attempts, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 0, ?, ?)",
            (job_id, kind, json.dumps(payload), QUEUED, now, now),
        )
        self._dispatch(job_id)
        return job_id

    def _dispatch(self, job_id: str) -> None:
        loop = self._loop
        if loop is None:
            log.warning("Job %s queued before the event loop was bound; it runs on recovery.", job_id)
            return
        if asyncio.get_event_loop_policy() and _running_loop() is loop:
            self._spawn(job_id)
        else:
            loop.call_soon_threadsafe(self._spawn, job_id)

    def _spawn(self, job_id: str) -> None:
        task = asyncio.ensure_future(self._run_job(job_id))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ----------------------------------------------------------- execution

    async def _run_job(self, job_id: str) -> None:
        row = self._db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None or row["status"] not in (QUEUED,):
            return
        kind = row["kind"]
        handler = self._handlers.get(kind)
        payload = json.loads(row["payload_json"])
        if handler is None:
            self._finish(job_id, FAILED, error=f"No handler registered for job kind {kind!r}.")
            return

        attempts = int(row["attempts"]) + 1
        self._db.execute(
            "UPDATE jobs SET status = ?, attempts = ?, updated_at = ? WHERE id = ?",
            (RUNNING, attempts, utcnow(), job_id),
        )
        self._publish(job_id, kind, RUNNING, payload)

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._executor, handler, payload)
        except Exception as exc:  # noqa: BLE001 - reported to the user verbatim
            detail = f"{type(exc).__name__}: {exc}"
            log.warning("Job %s (%s) failed: %s\n%s", job_id, kind, detail, traceback.format_exc())
            if attempts < MAX_ATTEMPTS and getattr(exc, "retryable", False):
                self._db.execute(
                    "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
                    (QUEUED, detail, utcnow(), job_id),
                )
                self._dispatch(job_id)
            else:
                self._finish(job_id, FAILED, error=detail, payload=payload, kind=kind)
            return
        self._finish(job_id, SUCCEEDED, payload=payload, kind=kind, result=result)

    def _finish(
        self,
        job_id: str,
        status: str,
        *,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
        kind: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        self._db.execute(
            "UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, utcnow(), job_id),
        )
        self._publish(job_id, kind or "", status, payload or {}, error=error, result=result)

    def _publish(
        self,
        job_id: str,
        kind: str,
        status: str,
        payload: dict[str, Any],
        *,
        error: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        self._bus.publish(
            JOB_UPDATED,
            {
                "job_id": job_id,
                "kind": kind,
                "status": status,
                "document_id": payload.get("document_id"),
                "note_id": payload.get("note_id"),
                "error": error,
                "result": result,
            },
        )

    # ------------------------------------------------------------ recovery

    def recover(self) -> int:
        """Requeue jobs that were running when the process stopped."""
        interrupted = self._db.query("SELECT id FROM jobs WHERE status = ?", (RUNNING,))
        for row in interrupted:
            self._db.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (QUEUED, utcnow(), row["id"]),
            )
        queued = self._db.query(
            "SELECT id FROM jobs WHERE status = ? ORDER BY created_at", (QUEUED,)
        )
        for row in queued:
            self._dispatch(row["id"])
        return len(queued)

    async def drain(self, timeout: float = 30.0) -> None:
        if self._pending:
            await asyncio.wait(set(self._pending), timeout=timeout)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
