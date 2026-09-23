"""Move existing PDFs from ``documents/<id>/original.pdf`` into ``Papers/``.

Explicit, never automatic. ``_ensure_dirs`` runs on every open, and silently
mass-renaming somebody's files is exactly what the project promises not to do
— opening a project must never rewrite existing material.

The ordering is what makes it safe. Each PDF is *linked* to its new name
before the old name is removed, so the bytes always have at least one name and
a crash can only ever leave a harmless extra one:

    1. reserve a unique canonical name
    2. link (or copy and verify) the old path to the new one
    3. fsync the directory
    4. write the pointer into metadata.json
    5. only now unlink the old path

A crash after 2 leaves a stray file the next run adopts by content hash. A
crash after 4 leaves both names, and the old one is removed only once the
hashes agree. "Migrated" is derived per document from a valid pointer, so
there is no global flag to get out of step: a half-migrated project is just a
project.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .documents import DocumentStore
from .naming import comparable, unique_name
from .workspace import Workspace, atomic_write_bytes, sha256_bytes, utcnow

# Folders whose sync daemon may rewrite, dehydrate or duplicate files while a
# migration is running.
CLOUD_MARKERS = (
    "CloudStorage",
    "Dropbox",
    "iCloud Drive",
    "Google Drive",
    "OneDrive",
)


@dataclass
class MigrationReport:
    moved: list[dict[str, str]] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    hardlinks: bool = True
    cloud_synced: bool = False
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed

    def to_json(self) -> dict[str, Any]:
        return {
            "moved": self.moved,
            "already_migrated": self.already,
            "missing": self.missing,
            "failed": self.failed,
            "hardlinks": self.hardlinks,
            "cloud_synced": self.cloud_synced,
            "dry_run": self.dry_run,
        }


def is_cloud_synced(root: Path) -> bool:
    parts = set(root.resolve().parts)
    return any(marker in parts for marker in CLOUD_MARKERS)


def hardlinks_work(root: Path) -> bool:
    """Probe the real target directory rather than trusting the filesystem.

    The volume may support links while a sync client sitting on top of it does
    not, so this creates and removes an actual pair.
    """
    probe = root / f".litmark-linkprobe-{os.getpid()}"
    link = root / f".litmark-linkprobe-{os.getpid()}.lnk"
    try:
        probe.write_bytes(b"probe")
        try:
            os.link(probe, link)
        except OSError:
            return False
        return probe.stat().st_ino == link.stat().st_ino and probe.stat().st_nlink == 2
    except OSError:
        return False
    finally:
        link.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def _link_or_copy(source: Path, target: Path, *, use_links: bool) -> str:
    """Give the bytes a second name. Returns the method actually used."""
    if use_links:
        try:
            os.link(source, target)
            return "link"
        except OSError:
            pass  # Fall through: a copy is always correct, just slower.
    data = source.read_bytes()
    atomic_write_bytes(target, data)
    # A copy can be torn where a link cannot, so it is verified.
    if sha256_bytes(target.read_bytes()) != sha256_bytes(data):
        target.unlink(missing_ok=True)
        raise OSError(f"Copy of {source.name} did not verify.")
    return "copy"


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def needs_migration(documents: DocumentStore) -> list[str]:
    """Documents whose PDF is still at the legacy path."""
    pending = []
    for document in documents.list():
        _, status = documents.resolve_pdf(document.document_id)
        if status == "legacy":
            pending.append(document.document_id)
    return pending


def migrate(
    workspace: Workspace, *, dry_run: bool = False, documents: DocumentStore | None = None
) -> MigrationReport:
    store = documents or DocumentStore(workspace)
    report = MigrationReport(
        dry_run=dry_run,
        cloud_synced=is_cloud_synced(workspace.root),
        hardlinks=True if dry_run else hardlinks_work(workspace.root),
    )
    workspace.papers_dir.mkdir(parents=True, exist_ok=True)
    taken = store.taken_names()

    for document in store.list():
        document_id = document.document_id
        found, status = store.resolve_pdf(document_id)
        if status in {"ok", "relinked"}:
            report.already.append(document_id)
            continue
        if found is None:
            report.missing.append(document_id)
            continue

        if not dry_run:
            # A previous interrupted run may already have linked these bytes
            # under their canonical name. Claim that file rather than
            # reserving a "(2)" beside it and leaving two copies.
            adopted = store.adopt_by_hash(document)
            if adopted is not None:
                found.unlink(missing_ok=True)
                taken.add(comparable(adopted.name))
                report.moved.append(
                    {"document_id": document_id, "to": adopted.name, "how": "adopted"}
                )
                continue

        name = unique_name(document.canonical_name, taken)
        taken.add(comparable(name))
        target = workspace.resolve_inside(workspace.papers_dir / name)
        if dry_run:
            report.moved.append({"document_id": document_id, "to": name, "how": "planned"})
            continue

        try:
            if target.exists():
                # A previous interrupted run already linked it.
                how = "adopted"
            else:
                how = _link_or_copy(found, target, use_links=report.hardlinks)
            _fsync_dir(workspace.papers_dir)
            document.pdf = {
                "path": target.relative_to(workspace.root).as_posix(),
                "canonical_name": name,
                "linked_at": utcnow(),
            }
            store.save(document)
            # Only now, and only when the new copy is provably the same bytes.
            if sha256_bytes(target.read_bytes()) == document.sha256:
                found.unlink(missing_ok=True)
            report.moved.append({"document_id": document_id, "to": name, "how": how})
        except OSError as exc:
            report.failed.append({"document_id": document_id, "error": str(exc)})

    return report


def verify(workspace: Workspace, documents: DocumentStore | None = None) -> dict[str, Any]:
    """Every document resolves to bytes matching its recorded hash."""
    store = documents or DocumentStore(workspace)
    good, bad = [], []
    for document in store.list():
        path, status = store.resolve_pdf(document.document_id)
        if path is None:
            bad.append({"document_id": document.document_id, "problem": "missing"})
            continue
        if sha256_bytes(path.read_bytes()) != document.sha256:
            bad.append({"document_id": document.document_id, "problem": "hash_mismatch"})
            continue
        good.append({"document_id": document.document_id, "status": status})
    return {"ok": not bad, "verified": good, "problems": bad}


def undo(workspace: Workspace, documents: DocumentStore | None = None) -> MigrationReport:
    """Put every PDF back at ``documents/<id>/original.pdf``."""
    store = documents or DocumentStore(workspace)
    report = MigrationReport(hardlinks=hardlinks_work(workspace.root))

    for document in store.list():
        document_id = document.document_id
        if not document.pdf.get("path"):
            report.already.append(document_id)
            continue
        found, status = store.resolve_pdf(document_id)
        if found is None:
            report.missing.append(document_id)
            continue
        legacy = workspace.resolve_inside(store.legacy_pdf_path(document_id))
        legacy.parent.mkdir(parents=True, exist_ok=True)
        try:
            if not legacy.exists():
                _link_or_copy(found, legacy, use_links=report.hardlinks)
            document.pdf = {}
            store.save(document)
            if sha256_bytes(legacy.read_bytes()) == document.sha256:
                found.unlink(missing_ok=True)
            report.moved.append({"document_id": document_id, "to": "documents", "how": "restored"})
        except OSError as exc:
            report.failed.append({"document_id": document_id, "error": str(exc)})

    return report
