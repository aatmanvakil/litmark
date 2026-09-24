"""arXiv speaks Atom, not JSON.

The existing `arxiv_works` / `arxiv_versions` parsers take dictionaries, so
this turns one Atom feed into the shape they expect. Parsed with defusedxml:
the feed is third-party content, and stdlib ElementTree will happily expand
a billion-laughs entity.
"""

from __future__ import annotations

from typing import Any

from defusedxml import ElementTree

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV = "{http://arxiv.org/schemas/atom}"


def _text(node: Any, path: str) -> str | None:
    found = node.find(path)
    if found is None or found.text is None:
        return None
    return " ".join(found.text.split()) or None


def parse_feed(xml: str) -> list[dict[str, Any]]:
    """Entries from an arXiv Atom feed, as the parsers expect them.

    A malformed feed raises ``ValueError`` rather than propagating an XML
    exception, so a caller can treat every provider failure alike.
    """
    try:
        root = ElementTree.fromstring(xml)
    except Exception as exc:  # noqa: BLE001 - any XML failure is one failure
        raise ValueError(f"arXiv returned a feed that could not be parsed: {exc}") from exc

    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        title = _text(entry, f"{ATOM}title")
        if title is None:
            continue  # An error feed carries a single untitled entry.
        pdf_url = None
        for link in entry.findall(f"{ATOM}link"):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break
        entries.append(
            {
                "title": title,
                "authors": [
                    name
                    for author in entry.findall(f"{ATOM}author")
                    if (name := _text(author, f"{ATOM}name"))
                ],
                "published": _text(entry, f"{ATOM}published"),
                "pdf_url": pdf_url,
                "doi": _text(entry, f"{ARXIV}doi"),
                "license": _text(entry, f"{ATOM}rights"),
            }
        )
    return entries
