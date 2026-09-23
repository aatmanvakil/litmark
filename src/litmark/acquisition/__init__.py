"""Resolving a title, citation or DOI to candidate papers.

**Resolution never writes a file.** Searching, ranking and displaying
candidates produce no document, no `Papers/` entry, no metadata and no
reference; abandoning the dialog leaves the project byte-identical. Only a
confirmed download imports anything, and that path lives elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .model import (
    ACCEPTED_MANUSCRIPT,
    PUBLISHED,
    SUBMITTED_PREPRINT,
    Candidate,
    MetadataSource,
    Version,
    Work,
    looks_like_doi,
    normalize_doi,
    rank_candidates,
    rank_versions,
)
from .providers import is_fetchable

__all__ = [
    "ACCEPTED_MANUSCRIPT",
    "PUBLISHED",
    "SUBMITTED_PREPRINT",
    "Candidate",
    "MetadataSource",
    "Resolution",
    "Version",
    "Work",
    "is_fetchable",
    "looks_like_doi",
    "normalize_doi",
    "rank_candidates",
    "rank_versions",
    "resolve",
]


@dataclass
class Resolution:
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "warnings": self.warnings,
            # Stated rather than implied, because it is the invariant that
            # makes this safe to run against a live project.
            "wrote_anything": False,
        }


def _merge(works: list[Work]) -> list[Work]:
    """One entry per DOI; works without one are kept as they are."""
    by_doi: dict[str, Work] = {}
    loose: list[Work] = []
    for work in works:
        if not work.doi:
            loose.append(work)
            continue
        existing = by_doi.get(work.doi)
        if existing is None:
            by_doi[work.doi] = work
            continue
        # Fill gaps from a second provider without overwriting what we have.
        for attribute in ("title", "journal", "year"):
            if getattr(existing, attribute) is None:
                setattr(existing, attribute, getattr(work, attribute))
        if not existing.authors:
            existing.authors = work.authors
    return list(by_doi.values()) + loose


def resolve(
    query: str,
    *,
    metadata_sources: list[MetadataSource],
    version_sources: list[MetadataSource] | None = None,
) -> Resolution:
    """Candidate works for a query, ranked. Writes nothing, anywhere."""
    query = query.strip()
    if not query:
        return Resolution(query=query, warnings=["Enter a title, citation or DOI."])

    works: list[Work] = []
    warnings: list[str] = []
    for source in metadata_sources:
        try:
            works.extend(source.works(query))
        except Exception as exc:  # noqa: BLE001 - reported, never invented around
            warnings.append(f"{source.name} did not answer: {exc}")

    candidates: list[Candidate] = []
    for work in _merge(works):
        versions: list[Version] = []
        for source in version_sources or []:
            try:
                versions.extend(source.versions(work))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{source.name} did not answer: {exc}")
        # A version a provider offers but the fetch rules forbid is still
        # shown — as a link the user may open themselves, never as a download.
        for index, version in enumerate(versions):
            if version.retrievable and not is_fetchable(version):
                versions[index] = Version(
                    **{
                        **version.__dict__,
                        "retrievable": False,
                        "reason": version.reason or "not a permitted source",
                    }
                )
        candidates.append(Candidate(work=work, versions=rank_versions(versions)))

    if not candidates and not warnings:
        warnings.append("No matching work was found.")
    return Resolution(
        query=query,
        candidates=rank_candidates(candidates, query),
        warnings=warnings,
    )
