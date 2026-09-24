"""Live metadata clients, one per provider.

Each does one request and hands the body to the parser in ``providers.py``
that already exists and is already tested, so this module is transport, not
parsing.

Two of the four may produce a URL the fetcher will later accept — Unpaywall
and arXiv. Crossref and OpenAlex are metadata only, and `is_fetchable`
refuses anything they name regardless of what this module returns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from .arxiv_atom import parse_feed
from .config import ARXIV, CROSSREF, OPENALEX, UNPAYWALL, AcquisitionConfig
from .fetch import FetchRefused, fetch_json, fetch_text
from .limits import RateLimiter
from .model import Version, Work, looks_like_doi, normalize_doi
from .providers import (
    arxiv_versions,
    arxiv_works,
    crossref_works,
    openalex_works,
    unpaywall_versions,
)
from .redact import safe_url

log = logging.getLogger(__name__)

CROSSREF_BASE = "https://api.crossref.org"
OPENALEX_BASE = "https://api.openalex.org"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
ARXIV_BASE = "https://export.arxiv.org/api/query"

#: Enough to choose from without asking a provider for a page of results.
MAX_RESULTS = 5

PROJECT_URL = "https://github.com/tlamadon/litmark"


def user_agent(mailto: str | None) -> str:
    """Crossref's recommended shape: who this is, and how to complain."""
    contact = f"; mailto:{mailto}" if mailto else ""
    return f"litmark/0.1.0 (+{PROJECT_URL}{contact})"


@dataclass
class _Client:
    config: AcquisitionConfig
    limiter: RateLimiter
    #: Injected so the whole suite runs offline.
    get_json: Any = fetch_json
    get_text: Any = fetch_text

    # A ClassVar, not a field: a dataclass __init__ would overwrite each
    # subclass's value with the base default.
    name: ClassVar[str] = ""

    def _guard(self) -> None:
        """Refuse to spend a call a provider has told us to hold off on."""
        cooling = self.limiter.cooling_down(self.name)
        if cooling > 0:
            raise FetchRefused("rate_limited", host=self.name, retry_after=cooling)
        self.limiter.wait(self.name)

    def _note(self, url: str) -> None:
        log.debug("%s: %s", self.name, safe_url(url))


class CrossrefSource(_Client):
    """Metadata only. Crossref never yields a URL the fetcher will accept."""

    name = CROSSREF

    def works(self, query: str) -> list[Work]:
        self._guard()
        doi = normalize_doi(query)
        params: dict[str, str] = {}
        if self.config.mailto:
            # Both forms are accepted; sending both is what the polite pool
            # documentation suggests, and it costs nothing.
            params["mailto"] = self.config.mailto
        if doi:
            url = f"{CROSSREF_BASE}/works/{quote(doi, safe='')}"
        else:
            params.update({"query.bibliographic": query, "rows": str(MAX_RESULTS)})
            url = f"{CROSSREF_BASE}/works"
        if params:
            url = f"{url}?{urlencode(params)}"
        self._note(url)
        payload = self.get_json(
            url, headers={"User-Agent": user_agent(self.config.mailto)}
        )
        return crossref_works(payload)

    def versions(self, work: Work) -> list[Version]:
        return []


class OpenAlexSource(_Client):
    """Metadata only, and the one provider that needs a credential."""

    name = OPENALEX

    def works(self, query: str) -> list[Work]:
        self._guard()
        doi = normalize_doi(query)
        if doi:
            params = {"filter": f"doi:{doi}", "per-page": str(MAX_RESULTS)}
        else:
            params = {"search": query, "per-page": str(MAX_RESULTS)}
        url = f"{OPENALEX_BASE}/works?{urlencode(params)}"
        self._note(url)
        # The key travels in a header, never in the URL: OpenAlex accepts
        # api_key= as a parameter, but a URL reaches logs and diagnostics.
        headers = {"User-Agent": user_agent(self.config.mailto)}
        if self.config.openalex_key:
            headers["Authorization"] = f"Bearer {self.config.openalex_key}"
        payload = self.get_json(url, headers=headers)
        return openalex_works(payload)

    def versions(self, work: Work) -> list[Version]:
        return []


class UnpaywallSource(_Client):
    """DOI to lawful open-access locations. The general source of PDF URLs."""

    name = UNPAYWALL

    def works(self, query: str) -> list[Work]:
        return []

    def versions(self, work: Work) -> list[Version]:
        if not work.doi or not self.config.mailto:
            return []  # Unpaywall is DOI-only and requires a contact address.
        self._guard()
        url = (
            f"{UNPAYWALL_BASE}/{quote(work.doi, safe='')}"
            f"?{urlencode({'email': self.config.mailto})}"
        )
        self._note(url)
        try:
            payload = self.get_json(url)
        except FetchRefused as exc:
            if exc.reason == "not_found":
                return []  # Unpaywall simply does not know this DOI.
            raise
        return unpaywall_versions(payload)


class ArxivSource(_Client):
    """arXiv metadata and arXiv-hosted PDFs. Atom, and slow by requirement."""

    name = ARXIV

    def _query(self, params: dict[str, str]) -> list[dict[str, Any]]:
        self._guard()
        url = f"{ARXIV_BASE}?{urlencode(params)}"
        self._note(url)
        return parse_feed(self.get_text(url))

    def works(self, query: str) -> list[Work]:
        if looks_like_doi(query):
            return []  # arXiv search does not take a DOI usefully.
        return arxiv_works(
            self._query({"search_query": f'ti:"{query}"', "max_results": str(MAX_RESULTS)})
        )

    def versions(self, work: Work) -> list[Version]:
        if not work.title:
            return []
        return arxiv_versions(
            self._query({"search_query": f'ti:"{work.title}"', "max_results": "1"})
        )


#: Built in this order so metadata arrives before versions are looked up.
METADATA_CLASSES = {CROSSREF: CrossrefSource, OPENALEX: OpenAlexSource}
VERSION_CLASSES = {UNPAYWALL: UnpaywallSource, ARXIV: ArxivSource}


def build_sources(
    config: AcquisitionConfig,
    *,
    limiter: RateLimiter | None = None,
    get_json: Any = fetch_json,
    get_text: Any = fetch_text,
) -> tuple[list[Any], list[Any]]:
    """The (metadata, version) source lists for whatever is available."""
    shared = limiter or RateLimiter()
    metadata = [
        klass(config=config, limiter=shared, get_json=get_json, get_text=get_text)
        for name, klass in METADATA_CLASSES.items()
        if config.available(name)
    ]
    versions = [
        klass(config=config, limiter=shared, get_json=get_json, get_text=get_text)
        for name, klass in VERSION_CLASSES.items()
        if config.available(name)
    ]
    return metadata, versions
