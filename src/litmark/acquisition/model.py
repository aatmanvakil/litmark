"""What a resolution returns, and how candidates are ordered.

A **work** is the paper. A **version** is one retrievable manifestation of it.
The distinction matters because the same work is often available as a
published article behind a paywall and as an author manuscript that is free,
and those are different things to offer somebody.

Nothing here touches the network or the filesystem. Resolution is pure: it
produces candidates to show, and only a confirmed download writes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

PUBLISHED = "published"
ACCEPTED_MANUSCRIPT = "accepted_manuscript"
SUBMITTED_PREPRINT = "submitted_preprint"

# Best first. A version of record beats an author manuscript, which beats a
# preprint; anything not lawfully retrievable ranks below all of them however
# authoritative it is, because it cannot be offered as a download.
VERSION_ORDER = {PUBLISHED: 0, ACCEPTED_MANUSCRIPT: 1, SUBMITTED_PREPRINT: 2}

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)


def normalize_doi(value: str | None) -> str | None:
    """A bare ``10.x/y``, however it was written or wrapped."""
    if not value:
        return None
    match = _DOI_RE.search(value.strip())
    if not match:
        return None
    return match.group(0).rstrip(".,;)").lower()


def looks_like_doi(query: str) -> bool:
    return normalize_doi(query) is not None


@dataclass(frozen=True)
class Version:
    """One retrievable manifestation of a work."""

    version_type: str
    url: str | None
    host: str | None = None
    license: str | None = None
    retrievable: bool = False
    source: str = ""
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version_type": self.version_type,
            "url": self.url,
            "host": self.host,
            "license": self.license,
            "retrievable": self.retrievable,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass
class Work:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    source: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "journal": self.journal,
            "doi": self.doi,
            "source": self.source,
        }

    def authors_string(self) -> str:
        """The single free-text form the rest of the application stores."""
        return " and ".join(self.authors)


@dataclass
class Candidate:
    work: Work
    versions: list[Version] = field(default_factory=list)

    @property
    def best(self) -> Version | None:
        return self.versions[0] if self.versions else None

    @property
    def retrievable(self) -> bool:
        return any(version.retrievable for version in self.versions)

    def to_json(self) -> dict[str, Any]:
        return {
            "work": self.work.to_json(),
            "versions": [version.to_json() for version in self.versions],
            "retrievable": self.retrievable,
        }


class MetadataSource(Protocol):
    """A provider of works, versions, or both.

    Kept as a protocol so every test can inject a recorded payload and the
    suite never opens a socket.
    """

    name: str

    def works(self, query: str) -> list[Work]:
        """Candidate works for a title, citation or DOI."""

    def versions(self, work: Work) -> list[Version]:
        """Retrievable manifestations of a known work."""


def rank_versions(versions: list[Version]) -> list[Version]:
    """Retrievable first, then by version type, then publisher over repository."""
    return sorted(
        versions,
        key=lambda v: (
            not v.retrievable,
            VERSION_ORDER.get(v.version_type, 9),
            0 if v.host == "publisher" else 1,
        ),
    )


def _title_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    left = set(re.findall(r"\w+", a.lower()))
    right = set(re.findall(r"\w+", b.lower()))
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def rank_candidates(candidates: list[Candidate], query: str) -> list[Candidate]:
    """Order works by how well they answer the query.

    Ordering only. Nothing here selects a candidate: the user does that.
    """
    wanted_doi = normalize_doi(query)

    def key(candidate: Candidate) -> tuple[Any, ...]:
        doi_match = 0 if wanted_doi and candidate.work.doi == wanted_doi else 1
        return (
            doi_match,
            not candidate.retrievable,
            -_title_similarity(candidate.work.title, query),
            -(candidate.work.year or 0),
        )

    return sorted(candidates, key=key)
