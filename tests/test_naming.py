"""The canonical filename rules.

The year is the interesting one: what a PDF reports is when the file was
produced, so it must never reach a filename until someone confirms it is the
publication year.
"""

from __future__ import annotations

import unicodedata

from litmark.naming import (
    MAX_BASENAME_BYTES,
    YEAR_CONFIRMED,
    YEAR_FROM_CREATIONDATE,
    author_segment,
    canonical_name,
    comparable,
    surnames,
    unique_name,
)


def name(authors=None, year=None, title="A Title", year_source=None):
    return canonical_name(
        authors=authors, year=year, title=title, year_source=year_source
    )


# ------------------------------------------------------------------ authors


def test_one_author():
    assert author_segment("Alice Researcher") == "Researcher"


def test_three_authors_are_all_named():
    assert (
        author_segment("Alice Researcher and Bob Coauthor and Carol Third")
        == "Researcher, Coauthor, Third"
    )


def test_four_authors_become_et_al():
    assert (
        author_segment("Alice A and Bob B and Carol C and Dan D") == "A et al."
    )


def test_surname_first_form_is_understood():
    assert author_segment("Researcher, Alice") == "Researcher"


def test_a_comma_separated_list_is_split():
    assert surnames("Alice Researcher, Bob Coauthor") == ["Researcher", "Coauthor"]


def test_missing_authors_become_unknown():
    assert name(authors=None).startswith("Unknown (")
    assert name(authors="   ").startswith("Unknown (")


# --------------------------------------------------------------------- year


def test_creationdate_year_never_reaches_a_filename():
    """The PDF's year is when the file was made, not when the work appeared."""
    assert "(n.d.)" in name(year=2016, year_source=YEAR_FROM_CREATIONDATE)
    assert "2016" not in name(year=2016, year_source=YEAR_FROM_CREATIONDATE)


def test_an_unsourced_year_is_also_withheld():
    assert "(n.d.)" in name(year=2016, year_source=None)


def test_a_confirmed_year_is_used():
    assert "(2016)" in name(year=2016, year_source=YEAR_CONFIRMED)


def test_a_confirmed_but_absent_year_is_still_nd():
    assert "(n.d.)" in name(year=None, year_source=YEAR_CONFIRMED)


# --------------------------------------------------------------------- shape


def test_the_full_shape():
    assert (
        name(
            authors="Alice Researcher and Bob Coauthor",
            year=2016,
            title="Exchange Rate Disconnect",
            year_source=YEAR_CONFIRMED,
        )
        == "Researcher, Coauthor (2016) – Exchange Rate Disconnect.pdf"
    )


def test_the_separator_is_an_en_dash():
    assert "–" in name()
    assert " - " not in name()


# ---------------------------------------------------------------- sanitizing


def test_path_separators_cannot_appear():
    result = name(title="Before/After: A Study")
    assert "/" not in result
    assert ":" not in result


def test_control_characters_are_dropped():
    result = name(title="Bad\r\nTitle")
    assert "\r" not in result and "\n" not in result


def test_accents_survive():
    assert "Müller" in name(authors="Anna Müller")


def test_the_name_is_nfc():
    result = name(authors="Anna Müller")  # NFD input
    assert result == unicodedata.normalize("NFC", result)


def test_a_trailing_dot_is_not_left_on_the_stem():
    assert ".." not in name(title="A Study.")


# -------------------------------------------------------------------- length


def test_a_long_title_is_truncated_to_the_byte_budget():
    result = name(title="Word " * 200)

    assert len(result.encode("utf-8")) <= MAX_BASENAME_BYTES
    assert result.endswith(".pdf")


def test_truncation_counts_bytes_not_characters():
    result = name(title="é" * 300)

    assert len(result.encode("utf-8")) <= MAX_BASENAME_BYTES
    # A character-based cut would have overshot, since é is two bytes.
    assert len(result) < 300


def test_a_pathological_author_list_still_leaves_room_for_a_title():
    result = name(authors=" and ".join(f"Person{i} Surname{i}" for i in range(3)) , title="Readable Title")
    assert "Readable" in result or len(result.encode("utf-8")) <= MAX_BASENAME_BYTES


# ----------------------------------------------------------------- collisions


def test_a_free_name_is_returned_unchanged():
    assert unique_name("A (2016) - T.pdf", set()) == "A (2016) - T.pdf"


def test_a_taken_name_is_suffixed():
    taken = {comparable("A (2016) - T.pdf")}

    assert unique_name("A (2016) - T.pdf", taken) == "A (2016) - T (2).pdf"


def test_suffixes_keep_climbing():
    taken = {comparable("A.pdf"), comparable("A (2).pdf")}

    assert unique_name("A.pdf", taken) == "A (3).pdf"


def test_collisions_are_case_insensitive():
    """APFS would treat these as the same file."""
    taken = {comparable("Researcher (2016) – Title.pdf")}

    assert unique_name("RESEARCHER (2016) – TITLE.pdf", taken) != (
        "RESEARCHER (2016) – TITLE.pdf"
    )


def test_collisions_are_normalization_insensitive():
    """HFS+ stores NFD, so the two spellings name one file."""
    nfc = unicodedata.normalize("NFC", "Müller (2016) – T.pdf")
    nfd = unicodedata.normalize("NFD", "Müller (2016) – T.pdf")

    assert nfc != nfd  # different strings...
    assert comparable(nfc) == comparable(nfd)  # ...one file
    assert unique_name(nfd, {comparable(nfc)}) != nfd


def test_et_al_keeps_its_period():
    """A trailing dot is stripped from the stem, but "et al." is not the stem."""
    result = name(authors="A One and B Two and C Three and D Four")

    assert result.startswith("One et al. (")
