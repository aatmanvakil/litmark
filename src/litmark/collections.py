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
# Deep enough for a real taxonomy, shallow enough that the sidebar stays
# readable at the narrowest pane width.
MAX_DEPTH = 5

_COLLECTION_ID_RE = re.compile(r"^col-[0-9a-z][0-9a-z-]{0,30}$")


class _Unset:
    """Distinguishes "leave the parent alone" from "move it to the top"."""


UNSET = _Unset()


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
    # A project is always a root; a topic may sit under a project or another
    # topic. See `validate_parent` — the rule is about projects, not about
    # kinds matching, and Project -> Topic is the case that matters most.
    parent_id: str | None = None
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
        if self.parent_id:
            payload["parent_id"] = self.parent_id
        return payload

    def api_json(self) -> dict[str, Any]:
        payload = self.to_json()
        payload["collection_id"] = self.collection_id
        payload["document_count"] = len(self.documents)
        payload.setdefault("parent_id", None)
        return payload

    @classmethod
    def from_json(cls, collection_id: str, data: dict[str, Any]) -> Collection:
        documents = data.get("documents") or []
        return cls(
            collection_id=collection_id,
            kind=data.get("kind") or PROJECT,
            name=data.get("name") or collection_id,
            description=data.get("description"),
            parent_id=data.get("parent_id") or None,
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
        loaded = {
            collection_id: Collection.from_json(collection_id, payload)
            for collection_id, payload in raw["collections"].items()
        }
        # collections.json is hand-editable, so a parent it names may be
        # missing, self-referential, in a cycle, or set on a project. Drop the
        # edge and keep the collection rather than failing to load.
        for collection in loaded.values():
            parent = collection.parent_id
            if parent is None:
                continue
            if (
                collection.kind == PROJECT
                or parent == collection.collection_id
                or parent not in loaded
                or collection.collection_id
                in self._ancestor_ids(raw["collections"], parent)
            ):
                collection.parent_id = None
        return loaded

    def api_list(self) -> list[dict[str, Any]]:
        """Collections with their path and subtree size, for the UI and agent."""
        out = []
        for collection in self.list():
            payload = collection.api_json()
            payload["path"] = self.path_of(collection.collection_id)
            payload["descendant_count"] = len(
                self.descendant_ids(collection.collection_id)
            ) - 1
            out.append(payload)
        return out

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
        parent_id: str | None = None,
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
            self.validate_parent(raw, kind, parent_id, moving=None)
            collection_id = self.allocate_id()
            now = utcnow()
            collection = Collection(
                collection_id=collection_id,
                kind=kind,
                name=clean,
                description=description or None,
                parent_id=parent_id,
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
        parent_id: str | None | _Unset = UNSET,
    ) -> Collection:
        """Rename, re-describe or move. ``kind`` stays immutable.

        ``parent_id`` is tri-state: absent means unchanged, ``None`` detaches
        to the top level, and an ID moves it. A plain ``None`` default could
        not tell "leave it alone" from "make it a root".
        """
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
            if not isinstance(parent_id, _Unset):
                self.validate_parent(
                    raw, payload.get("kind", PROJECT), parent_id, moving=collection_id
                )
                if parent_id:
                    payload["parent_id"] = parent_id
                else:
                    payload.pop("parent_id", None)
            payload["updated_at"] = utcnow()
            self._write(raw)
            return Collection.from_json(collection_id, payload)

    def delete(self, collection_id: str) -> dict[str, int]:
        """Forget a classification. The papers it held are untouched.

        Children are spliced up to the deleted collection's own parent rather
        than orphaned, which would lose the structure, or refused, which would
        leave a chat user unable to remove anything with a subtopic.
        """
        with self._lock:
            raw = self._load_raw()
            payload = raw["collections"].pop(collection_id, None)
            if payload is None:
                raise NotFound(
                    f"No collection {collection_id!r}.", collection_id=collection_id
                )
            grandparent = payload.get("parent_id")
            reparented = 0
            for other in raw["collections"].values():
                if other.get("parent_id") == collection_id:
                    if grandparent:
                        other["parent_id"] = grandparent
                    else:
                        other.pop("parent_id", None)
                    other["updated_at"] = utcnow()
                    reparented += 1
            self._write(raw)
            return {
                "documents_released": len(payload.get("documents") or []),
                "children_reparented": reparented,
            }

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

    # ------------------------------------------------------------- hierarchy

    def validate_parent(
        self, raw: dict[str, Any], kind: str, parent_id: str | None, *, moving: str | None
    ) -> None:
        """A project is always a root; a topic may sit under anything.

        Every forbidden case reduces to that one rule, so this is deliberately
        not a "kinds must match" check — Project -> Topic is the case the whole
        feature exists for.
        """
        if kind == PROJECT:
            if parent_id:
                raise InvalidInput(
                    "A project is always a top-level collection, so it cannot "
                    "be placed inside another collection. Topics can be nested.",
                    collection_id=moving,
                )
            return
        if parent_id is None:
            return
        collections = raw["collections"]
        if parent_id not in collections:
            raise NotFound(f"No collection {parent_id!r}.", collection_id=parent_id)
        if moving is not None and parent_id == moving:
            raise InvalidInput("A collection cannot be inside itself.")
        ancestors = self._ancestor_ids(collections, parent_id)
        if moving is not None and moving in ancestors:
            raise InvalidInput(
                "That would put a collection inside one of its own subtopics."
            )
        # +1 for the collection being placed, on top of its parent's chain.
        if len(ancestors) + 2 > MAX_DEPTH:
            raise InvalidInput(
                f"Collections can nest {MAX_DEPTH} levels deep; that would be deeper."
            )

    def _ancestor_ids(self, collections: dict[str, Any], start: str) -> list[str]:
        """Ancestors of ``start``, nearest first.

        Hard-capped by the number of collections: the file is hand-editable,
        so a cycle written by hand must degrade rather than spin forever.
        """
        chain: list[str] = []
        seen = {start}
        current = collections.get(start, {}).get("parent_id")
        for _ in range(len(collections) + 1):
            if not current or current in seen or current not in collections:
                break
            chain.append(current)
            seen.add(current)
            current = collections[current].get("parent_id")
        return chain

    def children_of(self, collection_id: str | None) -> list[Collection]:
        return [c for c in self.list() if c.parent_id == collection_id]

    def descendant_ids(self, collection_id: str) -> list[str]:
        """``collection_id`` first, then every collection beneath it."""
        by_parent: dict[str | None, list[str]] = {}
        for collection in self.list():
            by_parent.setdefault(collection.parent_id, []).append(collection.collection_id)
        found = [collection_id]
        seen = {collection_id}
        queue = [collection_id]
        while queue:
            current = queue.pop(0)
            for child in by_parent.get(current, []):
                if child in seen:
                    continue  # A hand-written cycle must not loop forever.
                seen.add(child)
                found.append(child)
                queue.append(child)
        return found

    def scope_document_ids(self, collection_id: str) -> list[str]:
        """Every document in a collection or in anything beneath it."""
        collections = self.all()
        found: list[str] = []
        for member_id in self.descendant_ids(collection_id):
            collection = collections.get(member_id)
            if collection is None:
                continue
            for document_id in collection.documents:
                if document_id not in found:
                    found.append(document_id)
        return found

    def path_of(self, collection_id: str) -> str:
        """``International Macro / Dominant Currency``, for addressing by name."""
        raw = self._load_raw()
        names = [raw["collections"].get(collection_id, {}).get("name", collection_id)]
        for ancestor in self._ancestor_ids(raw["collections"], collection_id):
            names.append(raw["collections"][ancestor].get("name", ancestor))
        return " / ".join(reversed(names))

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
