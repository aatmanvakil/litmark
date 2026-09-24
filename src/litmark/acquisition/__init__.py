"""Resolving a title, citation or DOI to candidate papers.

**Resolution never writes a file.** Searching, ranking and displaying
candidates produce no document, no `Papers/` entry, no metadata and no
reference; abandoning the dialog leaves the project byte-identical. Only a
confirmed download imports anything, and that path lives elsewhere.
"""

from __future__ import annotations

import re
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
    "title_key",
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


def _describe(exc: Exception) -> str:
    """A short reason, with anything sensitive already redacted."""
    from .fetch import FetchRefused

    if isinstance(exc, FetchRefused):
        wait = exc.details.get("retry_after")
        if exc.reason == "rate_limited" and wait:
            return f"rate limited; try again in about {int(wait)}s"
        return exc.reason
    return type(exc).__name__


@dataclass
class Resolution:
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Providers that did not answer, so the interface can say which.
    failed_providers: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "candidates": [candidate.to_json() for candidate in self.candidates],
            "warnings": self.warnings,
            "providers_failed": self.failed_providers,
            # Stated rather than implied, because it is the invariant that
            # makes this safe to run against a live project.
            "wrote_anything": False,
        }


def _absorb(existing: Work, other: Work) -> None:
    """Fill gaps from a second provider without overwriting what we have."""
    for attribute in ("title", "journal", "year", "doi"):
        if getattr(existing, attribute) is None:
            setattr(existing, attribute, getattr(other, attribute))
    if not existing.authors:
        existing.authors = other.authors


def title_key(work: Work) -> str | None:
    """A comparison key for works with no DOI to join on.

    Crossref and arXiv describe the same preprint with different
    punctuation and capitalisation, and only one of them has a DOI, so
    matching on the DOI alone leaves a visible duplicate in the card.
    """
    if not work.title:
        return None
    words = re.findall(r"[a-z0-9]+", work.title.lower())
    if not words:
        return None
    return " ".join(words) + (f"|{work.year}" if work.year else "")


def _merge(works: list[Work]) -> list[Work]:
    """One entry per work: by DOI, then by normalised title and year."""
    by_doi: dict[str, Work] = {}
    by_title: dict[str, Work] = {}
    ordered: list[Work] = []

    for work in works:
        existing = by_doi.get(work.doi) if work.doi else None
        if existing is None:
            key = title_key(work)
            if key is not None:
                existing = by_title.get(key)
        if existing is not None:
            _absorb(existing, work)
            # A DOI learned from the second provider indexes the merged work.
            if existing.doi and existing.doi not in by_doi:
                by_doi[existing.doi] = existing
            continue
        ordered.append(work)
        if work.doi:
            by_doi[work.doi] = work
        key = title_key(work)
        if key is not None:
            by_title[key] = work
    return ordered


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
    failed: list[str] = []
    for source in metadata_sources:
        try:
            works.extend(source.works(query))
        except Exception as exc:  # noqa: BLE001 - reported, never invented around
            warnings.append(f"{source.name} did not answer: {_describe(exc)}")
            failed.append(source.name)

    candidates: list[Candidate] = []
    for work in _merge(works):
        versions: list[Version] = []
        for source in version_sources or []:
            try:
                versions.extend(source.versions(work))
            except Exception as exc:  # noqa: BLE001 - one provider, one failure
                # Independent degradation: Unpaywall timing out must still
                # leave Crossref's work with its arXiv version attached.
                note = f"{source.name} did not answer: {_describe(exc)}"
                if note not in warnings:
                    warnings.append(note)
                if source.name not in failed:
                    failed.append(source.name)
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
        failed_providers=failed,
    )
