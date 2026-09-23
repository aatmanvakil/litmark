"""The canonical filename a stored PDF is given.

    Author, Author, Author (Year) – Title.pdf
    FirstAuthor et al. (Year) – Title.pdf

The separator is an en dash, kept rather than folded to a hyphen: APFS stores
arbitrary UTF-8 and only ``/`` and NUL are illegal, so substituting it would
make the name a lie about itself.

A year appears only once it has been *confirmed*. The year read from a PDF is
the ``/CreationDate`` year — when the file was produced, not when the work was
published — so putting it in a filename would state something the document
never said. Until someone confirms it, the name says ``n.d.``
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Only "/" and NUL are illegal on APFS, but ":" is a separator to Finder and
# the control range breaks display everywhere.
_UNSAFE = re.compile(r"[\x00-\x1f\x7f]")
_SEPARATORS = re.compile(r"[/:]")

EN_DASH = "–"
UNKNOWN_AUTHOR = "Unknown"
NO_DATE = "n.d."
SUFFIX = ".pdf"

# macOS allows 255 bytes per component; 180 leaves room for a " (12)" suffix
# and for the collision loop to stay well clear of the limit.
MAX_BASENAME_BYTES = 180

YEAR_CONFIRMED = "confirmed"
YEAR_FROM_CREATIONDATE = "creationdate"


def surname_of(name: str) -> str:
    """The surname from one author, however the producer wrote it."""
    head = name.split(",")[0].strip() if "," in name else name.strip()
    tokens = head.split()
    # "Researcher, Alice" leaves one token, the surname. "Alice Researcher"
    # leaves two, and the surname is the last.
    return tokens[-1] if tokens else ""


def surnames(authors: str | None) -> list[str]:
    """Every author's surname, reusing the BibTeX normalizer's comma rules."""
    if not authors or not authors.strip():
        return []
    # Imported here, not at module scope: bibliography imports Document, and
    # documents imports this module for the canonical name.
    from .bibliography import authors_field

    normalized = authors_field(authors)
    if not normalized:
        return []
    found = [surname_of(part) for part in normalized.split(" and ")]
    return [name for name in found if name]


def author_segment(authors: str | None) -> str:
    names = surnames(authors)
    if not names:
        return UNKNOWN_AUTHOR
    if len(names) <= 3:
        return ", ".join(names)
    return f"{names[0]} et al."


def year_segment(year: int | None, year_source: str | None) -> str:
    """A year only when it is known to be the *publication* year."""
    if year and year_source == YEAR_CONFIRMED:
        return str(year)
    return NO_DATE


def sanitize(value: str) -> str:
    cleaned = _SEPARATORS.sub("-", _UNSAFE.sub("", value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.strip(". ")


def _truncate_to_bytes(value: str, budget: int) -> str:
    """Cut on a codepoint boundary, preferring the last space."""
    if len(value.encode("utf-8")) <= budget:
        return value
    cut = value
    while len(cut.encode("utf-8")) > budget:
        cut = cut[:-1]
    spaced = cut.rsplit(" ", 1)[0]
    if len(spaced) >= budget // 2:
        cut = spaced
    return cut.strip(". ") or cut.strip()


def canonical_name(
    *,
    authors: str | None,
    year: int | None,
    title: str,
    year_source: str | None = None,
) -> str:
    """The filename for one paper, before any collision suffix."""
    prefix = f"{sanitize(author_segment(authors))} ({year_segment(year, year_source)}) {EN_DASH} "
    clean_title = sanitize(title) or "Untitled"
    budget = MAX_BASENAME_BYTES - len(prefix.encode("utf-8")) - len(SUFFIX.encode("utf-8"))
    if budget < 8:
        # A pathological author list; keep the title readable instead.
        prefix = f"{UNKNOWN_AUTHOR} ({year_segment(year, year_source)}) {EN_DASH} "
        budget = MAX_BASENAME_BYTES - len(prefix.encode("utf-8")) - len(SUFFIX.encode("utf-8"))
    return unicodedata.normalize("NFC", prefix + _truncate_to_bytes(clean_title, budget) + SUFFIX)


def comparable(name: str) -> str:
    """The identity key for collisions.

    APFS is case- and normalization-insensitive while HFS+ stores NFD, so the
    spelling on disk can never be trusted to tell two names apart.
    """
    return unicodedata.normalize("NFC", name).casefold()


def unique_name(name: str, taken: set[str]) -> str:
    """``name`` if free, else the same stem with ``(2)``, ``(3)``… appended."""
    if comparable(name) not in taken:
        return name
    stem, suffix = Path(name).stem, Path(name).suffix or SUFFIX
    index = 2
    while True:
        candidate = f"{stem} ({index}){suffix}"
        if comparable(candidate) not in taken:
            return candidate
        index += 1
