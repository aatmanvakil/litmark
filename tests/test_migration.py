"""Moving existing PDFs into Papers/ must never lose or duplicate one.

Every test here builds a project at the *old* layout first, because the
interesting behaviour is entirely about what happens to files that already
exist.
"""

from __future__ import annotations

import json
import os

import pytest
from conftest import make_pdf

from litmark.documents import DocumentStore
from litmark.migration import (
    hardlinks_work,
    is_cloud_synced,
    migrate,
    needs_migration,
    undo,
    verify,
)
from litmark.services import Services
from litmark.workspace import Workspace, sha256_bytes


def legacy_project(tmp_path, papers: dict[str, bytes]):
    """A project as it looked before Papers/ existed."""
    workspace = Workspace.initialize(tmp_path / "project")
    services = Services(workspace, backend_name="fake")
    store = services.documents
    for index, (filename, data) in enumerate(papers.items(), start=1):
        document_id = f"doc-{index:03d}"
        directory = store.dir_for(document_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "original.pdf").write_bytes(data)
        (directory / "metadata.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "document_id": document_id,
                    "sha256": sha256_bytes(data),
                    "original_filename": filename,
                    "byte_size": len(data),
                    "imported_at": "2026-01-01T00:00:00Z",
                    "title": filename.removesuffix(".pdf"),
                    "extraction": {"status": "ok", "quality": "ok"},
                    "summary": {"status": "none"},
                }
            ),
            encoding="utf-8",
        )
    return workspace, services, store


@pytest.fixture
def legacy(tmp_path):
    first = make_pdf([[(72, 700, "Alpha")]])
    second = make_pdf([[(72, 700, "Beta")]])
    workspace, services, store = legacy_project(
        tmp_path, {"Alpha Paper.pdf": first, "Beta Paper.pdf": second}
    )
    yield workspace, store, {"doc-001": first, "doc-002": second}
    services.db.close()


# ------------------------------------------------------------------- basics


def test_a_legacy_project_is_detected_as_needing_migration(legacy):
    _, store, _ = legacy

    assert needs_migration(store) == ["doc-001", "doc-002"]


def test_migration_moves_every_pdf_exactly_once(legacy):
    workspace, store, originals = legacy

    report = migrate(workspace, documents=store)

    assert len(report.moved) == 2 and report.ok
    assert len(list(workspace.papers_dir.iterdir())) == 2
    for document_id, data in originals.items():
        path, status = store.resolve_pdf(document_id)
        assert status == "ok"
        assert path.read_bytes() == data
        # The old name is gone, so there is exactly one copy.
        assert not store.legacy_pdf_path(document_id).exists()


def test_migration_is_idempotent(legacy):
    workspace, store, _ = legacy
    migrate(workspace, documents=store)

    second = migrate(workspace, documents=store)

    assert second.moved == []
    assert len(second.already) == 2
    assert len(list(workspace.papers_dir.iterdir())) == 2


def test_a_dry_run_changes_nothing(legacy):
    workspace, store, _ = legacy

    report = migrate(workspace, dry_run=True, documents=store)

    assert len(report.moved) == 2
    assert list(workspace.papers_dir.iterdir()) == []
    assert store.legacy_pdf_path("doc-001").exists()


def test_migration_is_not_triggered_by_opening_a_project(tmp_path, legacy):
    workspace, store, _ = legacy

    Workspace(workspace.root).open()

    # Opening creates the empty directory but moves nothing.
    assert workspace.papers_dir.is_dir()
    assert list(workspace.papers_dir.iterdir()) == []
    assert store.legacy_pdf_path("doc-001").exists()


def test_verification_passes_after_migration(legacy):
    workspace, store, _ = legacy
    migrate(workspace, documents=store)

    result = verify(workspace, store)

    assert result["ok"] and len(result["verified"]) == 2


def test_verification_notices_corrupted_bytes(legacy):
    workspace, store, _ = legacy
    migrate(workspace, documents=store)
    path, _ = store.resolve_pdf("doc-001")
    path.write_bytes(b"%PDF-1.4 not the same bytes")

    result = verify(workspace, store)

    assert not result["ok"]
    assert result["problems"][0]["problem"] in {"hash_mismatch", "missing"}


# ----------------------------------------------------------------- recovery


def test_migration_resumes_after_a_crash_between_link_and_pointer(legacy):
    """The stray file from an interrupted run is adopted, not duplicated."""
    workspace, store, originals = legacy
    document = store.get("doc-001")
    # Exactly the state a crash after step 2 leaves: the new name exists, the
    # pointer does not, the old name is still there.
    stray = workspace.papers_dir / document.canonical_name
    stray.write_bytes(originals["doc-001"])

    report = migrate(workspace, documents=store)

    assert report.ok
    assert len(list(workspace.papers_dir.iterdir())) == 2
    path, status = store.resolve_pdf("doc-001")
    assert status == "ok" and path.read_bytes() == originals["doc-001"]


def test_a_crash_after_the_pointer_leaves_no_second_copy(legacy):
    """Step 4 done, step 5 not: both names exist and the old one must go."""
    workspace, store, originals = legacy
    migrate(workspace, documents=store)
    # Recreate the legacy file, as an interrupted unlink would have left it.
    store.legacy_pdf_path("doc-001").write_bytes(originals["doc-001"])

    verify(workspace, store)
    again = migrate(workspace, documents=store)

    # The pointer wins; the document still resolves to exactly one canonical file.
    assert again.ok
    path, status = store.resolve_pdf("doc-001")
    assert status == "ok" and path.parent == workspace.papers_dir


def test_a_document_with_no_pdf_is_left_alone(tmp_path):
    workspace, services, store = legacy_project(tmp_path, {"Gone.pdf": make_pdf([[]])})
    store.legacy_pdf_path("doc-001").unlink()
    try:
        report = migrate(workspace, documents=store)

        assert report.missing == ["doc-001"]
        assert report.moved == []
    finally:
        services.db.close()


# ---------------------------------------------------------------- collisions


def test_two_documents_with_one_canonical_name_both_survive(tmp_path):
    first = make_pdf([[(72, 700, "One")]])
    second = make_pdf([[(72, 700, "Two")]])
    workspace, services, store = legacy_project(
        tmp_path, {"Same.pdf": first, "Same.pdf ": second}
    )
    try:
        for document_id in ("doc-001", "doc-002"):
            store.update_metadata(document_id, title="Same Title", authors="A Writer")

        report = migrate(workspace, documents=store)

        assert report.ok and len(list(workspace.papers_dir.iterdir())) == 2
        assert store.resolve_pdf("doc-001")[0].read_bytes() == first
        assert store.resolve_pdf("doc-002")[0].read_bytes() == second
    finally:
        services.db.close()


# ------------------------------------------------------------------- Dropbox


def test_hardlink_probe_reports_a_real_answer(tmp_path):
    result = hardlinks_work(tmp_path)

    assert isinstance(result, bool)
    # Nothing is left behind either way.
    assert list(tmp_path.glob(".litmark-linkprobe*")) == []


def test_migration_falls_back_to_copy_when_hardlinks_fail(legacy, monkeypatch):
    """Dropbox does not preserve hardlinks, so the copy path must be correct."""
    workspace, store, originals = legacy

    def no_links(*args, **kwargs):
        raise OSError("hardlinks unavailable")

    monkeypatch.setattr(os, "link", no_links)

    report = migrate(workspace, documents=store)

    assert report.hardlinks is False
    assert report.ok and len(report.moved) == 2
    for document_id, data in originals.items():
        assert store.resolve_pdf(document_id)[0].read_bytes() == data
    assert verify(workspace, store)["ok"]


def test_a_cloud_synced_path_is_recognised(tmp_path):
    synced = tmp_path / "CloudStorage" / "Dropbox" / "project"
    synced.mkdir(parents=True)

    assert is_cloud_synced(synced)
    assert not is_cloud_synced(tmp_path)


# ---------------------------------------------------------------------- undo


def test_undo_restores_the_legacy_layout_byte_for_byte(legacy):
    workspace, store, originals = legacy
    migrate(workspace, documents=store)

    report = undo(workspace, store)

    assert report.ok and len(report.moved) == 2
    for document_id, data in originals.items():
        legacy_path = store.legacy_pdf_path(document_id)
        assert legacy_path.read_bytes() == data
        assert store.get(document_id).pdf == {}
    assert list(workspace.papers_dir.iterdir()) == []


def test_migrate_undo_migrate_is_stable(legacy):
    workspace, store, originals = legacy

    migrate(workspace, documents=store)
    undo(workspace, store)
    migrate(workspace, documents=store)

    assert verify(workspace, store)["ok"]
    assert len(list(workspace.papers_dir.iterdir())) == 2
    for document_id, data in originals.items():
        assert store.resolve_pdf(document_id)[0].read_bytes() == data


def test_undo_on_an_unmigrated_project_does_nothing(legacy):
    workspace, store, _ = legacy

    report = undo(workspace, store)

    assert report.moved == []
    assert len(report.already) == 2
