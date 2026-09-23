"""Turning each provider's payload into works and versions.

Parsing only: every function here takes a decoded payload and returns data.
The transport is injected, so these are exercised against recorded fixtures
and the test suite never opens a socket.

Each provider has exactly one role, and only two may yield a URL the server
will ever fetch:

    Crossref   metadata only            never fetchable
    OpenAlex   metadata only            never fetchable
    Unpaywall  open-access locations    fetchable
    arXiv      arXiv-hosted PDFs        fetchable, arxiv.org only
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from .model import (
    ACCEPTED_MANUSCRIPT,
    PUBLISHED,
    SUBMITTED_PREPRINT,
    Version,
    Work,
    normalize_doi,
)

CROSSREF = "crossref"
OPENALEX = "openalex"
UNPAYWALL = "unpaywall"
ARXIV = "arxiv"

# Only these two ever produce a URL the fetcher will accept.
FETCHABLE_SOURCES = frozenset({UNPAYWALL, ARXIV})

ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org", "export.arxiv.org"})

_UNPAYWALL_VERSIONS = {
    "publishedversion": PUBLISHED,
    "acceptedversion": ACCEPTED_MANUSCRIPT,
    "submittedversion": SUBMITTED_PREPRINT,
}


def _year(value: Any) -> int | None:
    try:
        year = int(str(value)[:4])
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 2999 else None


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).hostname


# ------------------------------------------------------------------ Crossref


def crossref_works(payload: dict[str, Any]) -> list[Work]:
    """Metadata only. Crossref URLs are never fetched."""
    items = (payload.get("message") or {}).get("items")
    if items is None:
        item = payload.get("message")
        items = [item] if isinstance(item, dict) and item.get("DOI") else []
    works = []
    for item in items:
        titles = item.get("title") or []
        containers = item.get("container-title") or []
        issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
        works.append(
            Work(
                title=titles[0] if titles else None,
                authors=[
                    " ".join(p for p in (a.get("given"), a.get("family")) if p).strip()
                    for a in (item.get("author") or [])
                    if a.get("family") or a.get("given")
                ],
                year=_year(issued[0] if issued else None),
                journal=containers[0] if containers else None,
                doi=normalize_doi(item.get("DOI")),
                source=CROSSREF,
            )
        )
    return works


# ------------------------------------------------------------------ OpenAlex


def openalex_works(payload: dict[str, Any]) -> list[Work]:
    """Metadata only. OpenAlex locations are not treated as fetchable."""
    works = []
    for item in payload.get("results") or []:
        venue = (item.get("primary_location") or {}).get("source") or {}
        works.append(
            Work(
                title=item.get("title") or item.get("display_name"),
                authors=[
                    (a.get("author") or {}).get("display_name", "")
                    for a in (item.get("authorships") or [])
                    if (a.get("author") or {}).get("display_name")
                ],
                year=_year(item.get("publication_year")),
                journal=venue.get("display_name"),
                doi=normalize_doi(item.get("doi")),
                source=OPENALEX,
            )
        )
    return works


# ----------------------------------------------------------------- Unpaywall


def unpaywall_versions(payload: dict[str, Any]) -> list[Version]:
    """Open-access locations, the only general source of fetchable URLs."""
    versions: list[Version] = []
    locations = payload.get("oa_locations")
    if locations is None:
        best = payload.get("best_oa_location")
        locations = [best] if best else []
    for location in locations:
        if not location:
            continue
        url = location.get("url_for_pdf")
        host_type = location.get("host_type")
        versions.append(
            Version(
                version_type=_UNPAYWALL_VERSIONS.get(
                    str(location.get("version") or "").lower(), SUBMITTED_PREPRINT
                ),
                url=url,
                host="publisher" if host_type == "publisher" else "repository",
                license=location.get("license"),
                # A location without a direct PDF link is not fetchable: the
                # server will not open a landing page looking for one.
                retrievable=bool(url) and bool(payload.get("is_oa", True)),
                source=UNPAYWALL,
                reason=None if url else "no direct pdf link",
            )
        )
    if not versions:
        versions.append(
            Version(
                version_type=PUBLISHED,
                url=payload.get("doi_url"),
                host="publisher",
                license=None,
                retrievable=False,
                source=UNPAYWALL,
                reason="paywalled",
            )
        )
    return versions


def unpaywall_work(payload: dict[str, Any]) -> Work:
    return Work(
        title=payload.get("title"),
        authors=[
            " ".join(p for p in (a.get("given"), a.get("family")) if p).strip()
            for a in (payload.get("z_authors") or [])
            if a.get("family") or a.get("given")
        ],
        year=_year(payload.get("year")),
        journal=payload.get("journal_name"),
        doi=normalize_doi(payload.get("doi")),
        source=UNPAYWALL,
    )


# --------------------------------------------------------------------- arXiv


def arxiv_versions(entries: list[dict[str, Any]]) -> list[Version]:
    """arXiv PDFs. A URL is accepted only when arXiv itself hosts it."""
    versions = []
    for entry in entries:
        url = entry.get("pdf_url")
        host = _host_of(url)
        on_arxiv = host in ARXIV_HOSTS
        versions.append(
            Version(
                version_type=SUBMITTED_PREPRINT,
                url=url,
                host="repository",
                license=entry.get("license"),
                retrievable=bool(url) and on_arxiv,
                source=ARXIV,
                reason=None if on_arxiv else "not hosted by arxiv",
            )
        )
    return versions


def arxiv_works(entries: list[dict[str, Any]]) -> list[Work]:
    return [
        Work(
            title=entry.get("title"),
            authors=list(entry.get("authors") or []),
            year=_year(entry.get("published")),
            journal=None,
            doi=normalize_doi(entry.get("doi")),
            source=ARXIV,
        )
        for entry in entries
    ]


def is_fetchable(version: Version) -> bool:
    """The gate every download passes.

    A URL is fetchable only if a provider allowed to supply one produced it in
    this resolution. There is no path by which a user-supplied or Crossref or
    OpenAlex URL becomes fetchable.
    """
    if not version.retrievable or not version.url:
        return False
    if version.source not in FETCHABLE_SOURCES:
        return False
    if urlparse(version.url).scheme != "https":
        return False
    if version.source == ARXIV and _host_of(version.url) not in ARXIV_HOSTS:
        return False
    return True
