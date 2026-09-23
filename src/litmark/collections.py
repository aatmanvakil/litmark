"""Projects and topics: classifications a paper belongs to, never copies of it.

A collection records membership only. Removing a collection, or a paper from
one, never touches the PDF, its extraction, its summary, or its references.

"Project" here is a classification *inside* a workspace, which is a different
thing from the workspace directory that the CLI and the rest of this package
call a project. One type carries both user-facing kinds so the two never need
separate storage; the interface supplies the vocabulary.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InvalidInput, NotFound
from .workspace import Workspace, atomic_write_text, utcnow

SCHEMA_VERSION = 1

PROJECT = "project"
TOPIC = "topic"
KINDS = (PROJECT, TOPIC)

MAX_NAME_LENGTH = 200

_COLLECTION_ID_RE = re.compile(r"^col-[0-9a-z][0-9a-z-]{0,30}$")


def normalize_name(name: str) -> str:
    return " ".join(name.split())


def _comparable(name: str) -> str:
    return normalize_name(name).casefold()


@dataclass
class Collection:
    collection_id: str
    kind: str
    name: str
    description: str | None = None
    documents: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "name": self.name,
            "documents": list(self.documents),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.description:
            payload["description"] = self.description
        return payload

    def api_json(self) -> dict[str, Any]:
        payload = self.to_json()
        payload["collection_id"] = self.collection_id
        payload["document_count"] = len(self.documents)
        return payload

    @classmethod
    def from_json(cls, collection_id: str, data: dict[str, Any]) -> Collection:
        documents = data.get("documents") or []
        return cls(
            collection_id=collection_id,
            kind=data.get("kind") or PROJECT,
            name=data.get("name") or collection_id,
            description=data.get("description"),
            documents=[str(item) for item in documents],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


class CollectionStore:
    """``collections.json``, read and written atomically under a lock."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        return self._workspace.collections_file

    def _load_raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": SCHEMA_VERSION, "collections": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InvalidInput(
                f"{self.path.name} is not valid JSON ({exc}); "
                "fix or remove the file to continue."
            ) from exc
        data.setdefault("schema_version", SCHEMA_VERSION)
        data.setdefault("collections", {})
        return data

    def all(self) -> dict[str, Collection]:
        raw = self._load_raw()
        return {
            collection_id: Collection.from_json(collection_id, payload)
            for collection_id, payload in raw["collections"].items()
        }

    def list(self) -> list[Collection]:
        return sorted(
            self.all().values(), key=lambda c: (c.kind, _comparable(c.name))
        )

    def get(self, collection_id: str) -> Collection:
        raw = self._load_raw()
        payload = raw["collections"].get(collection_id)
        if payload is None:
            raise NotFound(
                f"No collection {collection_id!r}.", collection_id=collection_id
            )
        return Collection.from_json(collection_id, payload)

    def exists(self, collection_id: str) -> bool:
        return collection_id in self._load_raw()["collections"]

    def allocate_id(self) -> str:
        with self._lock:
            used = self._load_raw()["collections"].keys()
            index = 1
            while f"col-{index:03d}" in used:
                index += 1
            return f"col-{index:03d}"

    def create(
        self,
        *,
        kind: str,
        name: str,
        description: str | None = None,
        documents: list[str] | None = None,
    ) -> Collection:
        clean = self._validated_name(name)
        if kind not in KINDS:
            raise InvalidInput(
                f"Unknown collection kind {kind!r}; expected one of {', '.join(KINDS)}."
            )
        with self._lock:
            raw = self._load_raw()
            self._require_unique_name(raw, kind, clean, ignoring=None)
            collection_id = self.allocate_id()
            now = utcnow()
            collection = Collection(
                collection_id=collection_id,
                kind=kind,
                name=clean,
                description=description or None,
                documents=_unique(documents or []),
                created_at=now,
                updated_at=now,
            )
            raw["collections"][collection_id] = collection.to_json()
            self._write(raw)
        return collection

    def update(
        self,
        collection_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Collection:
        """Rename or re-describe. ``kind`` is immutable, see the module docstring."""
        with self._lock:
            raw = self._load_raw()
            payload = raw["collections"].get(collection_id)
            if payload is None:
                raise NotFound(
                    f"No collection {collection_id!r}.", collection_id=collection_id
                )
            if name is not None:
                clean = self._validated_name(name)
                self._require_unique_name(
                    raw, payload.get("kind", PROJECT), clean, ignoring=collection_id
                )
                payload["name"] = clean
            if description is not None:
                payload["description"] = description or None
                if not payload["description"]:
                    payload.pop("description", None)
            payload["updated_at"] = utcnow()
            self._write(raw)
            return Collection.from_json(collection_id, payload)

    def delete(self, collection_id: str) -> int:
        """Forget a classification. The papers it held are untouched."""
        with self._lock:
            raw = self._load_raw()
            payload = raw["collections"].pop(collection_id, None)
            if payload is None:
                raise NotFound(
                    f"No collection {collection_id!r}.", collection_id=collection_id
                )
            self._write(raw)
            return len(payload.get("documents") or [])

    def set_documents(self, collection_id: str, document_ids: list[str]) -> Collection:
        return self._mutate_members(collection_id, lambda _: _unique(document_ids))

    def add_documents(self, collection_id: str, document_ids: list[str]) -> Collection:
        return self._mutate_members(
            collection_id, lambda current: _unique(current + list(document_ids))
        )

    def remove_document(self, collection_id: str, document_id: str) -> Collection:
        return self._mutate_members(
            collection_id, lambda current: [d for d in current if d != document_id]
        )

    def forget_document(self, document_id: str) -> list[str]:
        """Drop a deleted paper from every collection.

        Unlike a reference, a membership entry carries nothing but the pair, so
        a tombstone would be noise rather than a recovery aid.
        """
        with self._lock:
            raw = self._load_raw()
            touched: list[str] = []
            for collection_id, payload in raw["collections"].items():
                documents = payload.get("documents") or []
                if document_id in documents:
                    payload["documents"] = [d for d in documents if d != document_id]
                    payload["updated_at"] = utcnow()
                    touched.append(collection_id)
            if touched:
                self._write(raw)
            return touched

    def collections_for(self, document_id: str) -> list[Collection]:
        return [c for c in self.list() if document_id in c.documents]

    def member_ids(self, collection_id: str) -> list[str]:
        return list(self.get(collection_id).documents)

    def unfiled(self, document_ids: list[str]) -> list[str]:
        """Documents in no collection. Computed, never stored."""
        classified = {d for c in self.all().values() for d in c.documents}
        return [d for d in document_ids if d not in classified]

    def _mutate_members(self, collection_id: str, change) -> Collection:
        with self._lock:
            raw = self._load_raw()
            payload = raw["collections"].get(collection_id)
            if payload is None:
                raise NotFound(
                    f"No collection {collection_id!r}.", collection_id=collection_id
                )
            payload["documents"] = change(payload.get("documents") or [])
            payload["updated_at"] = utcnow()
            self._write(raw)
            return Collection.from_json(collection_id, payload)

    def _validated_name(self, name: str) -> str:
        clean = normalize_name(name)
        if not clean:
            raise InvalidInput("A collection needs a name.")
        if len(clean) > MAX_NAME_LENGTH:
            raise InvalidInput(
                f"A collection name may be at most {MAX_NAME_LENGTH} characters."
            )
        return clean

    def _require_unique_name(
        self, raw: dict[str, Any], kind: str, name: str, *, ignoring: str | None
    ) -> None:
        """Unique per kind, so a project and a topic may share a name."""
        target = _comparable(name)
        for collection_id, payload in raw["collections"].items():
            if collection_id == ignoring or payload.get("kind") != kind:
                continue
            if _comparable(payload.get("name", "")) == target:
                raise InvalidInput(
                    f"A {kind} named {name!r} already exists.",
                    collection_id=collection_id,
                )

    def _write(self, raw: dict[str, Any]) -> None:
        raw["schema_version"] = SCHEMA_VERSION
        atomic_write_text(self.path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
