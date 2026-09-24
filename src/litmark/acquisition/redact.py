"""Keep credentials and personal data out of anything that escapes.

Two of the four providers need something sensitive in the request: OpenAlex
an API key, Unpaywall a contact address. The key can be kept in a header, but
the address cannot — Unpaywall requires it as a query parameter. So every
place a URL or an exception can reach a log, an error, the agent, or the
interface passes through here first.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query parameters whose value never appears anywhere but the wire.
SENSITIVE_PARAMS = frozenset(
    {"api_key", "apikey", "key", "token", "access_token", "email", "mailto"}
)

#: Headers never written out, in any form — not even a prefix.
SENSITIVE_HEADERS = frozenset({"authorization", "crossref-plus-api-token", "cookie"})

# No angle brackets: urlencode would percent-encode them into %3C...%3E,
# which is still redacted but unreadable in the log it exists for.
PLACEHOLDER = "REDACTED"


def safe_url(url: str) -> str:
    """A URL safe to print, with sensitive parameters blanked."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return PLACEHOLDER
    if not parts.query:
        return url
    cleaned = [
        (key, PLACEHOLDER if key.lower() in SENSITIVE_PARAMS else value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit(parts._replace(query=urlencode(cleaned)))


def safe_headers(headers: dict[str, Any]) -> dict[str, Any]:
    return {
        name: (PLACEHOLDER if name.lower() in SENSITIVE_HEADERS else value)
        for name, value in headers.items()
    }


def safe_details(details: dict[str, Any]) -> dict[str, Any]:
    """Scrub a details dict destined for an error payload or a log."""
    cleaned: dict[str, Any] = {}
    for name, value in details.items():
        if name.lower() in SENSITIVE_PARAMS or name.lower() in SENSITIVE_HEADERS:
            cleaned[name] = PLACEHOLDER
        elif isinstance(value, str) and "://" in value:
            cleaned[name] = safe_url(value)
        else:
            cleaned[name] = value
    return cleaned
