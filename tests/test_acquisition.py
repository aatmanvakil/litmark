"""Resolving a query to candidate papers, entirely offline.

Every provider answer here is a recorded fixture, and a socket guard proves
the suite never reaches the network. The invariant under test throughout is
that resolution writes nothing at all.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from conftest import upload

from litmark.acquisition import (
    ACCEPTED_MANUSCRIPT,
    PUBLISHED,
    SUBMITTED_PREPRINT,
    Version,
    Work,
    is_fetchable,
    looks_like_doi,
    normalize_doi,
    rank_versions,
    resolve,
)
from litmark.acquisition.providers import (
    arxiv_versions,
    arxiv_works,
    crossref_works,
    openalex_works,
    unpaywall_versions,
    unpaywall_work,
)

FIXTURES = Path(__file__).parent / "fixtures" / "acquisition"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text("utf-8"))


@pytest.fixture
def no_network(monkeypatch):
    """Refuse every socket, so a provider call cannot quietly reach out.

    Not autouse: the API tests below drive TestClient, whose transport is
    in-process but still constructs socket objects. The guard is applied to
    the resolution path itself, which is what must never dial out.
    """

    def forbidden(*args, **kwargs):
        raise AssertionError("resolution must not touch the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Recorded:
    """A MetadataSource backed by a fixture."""

    def __init__(self, name, works_payload=None, versions_payload=None, fails=False):
        self.name = name
        self._works = works_payload or []
        self._versions = versions_payload or []
        self._fails = fails

    def works(self, query):
        if self._fails:
            raise RuntimeError("provider unreachable")
        return list(self._works)

    def versions(self, work):
        if self._fails:
            raise RuntimeError("provider unreachable")
        return list(self._versions)


# ------------------------------------------------------------------- parsing


def test_crossref_yields_metadata():
    works = crossref_works(fixture("crossref_work.json"))

    assert len(works) == 1
    work = works[0]
    assert work.doi == "10.1234/exchange.2021"
    assert work.title == "Exchange Rate Disconnect in General Equilibrium"
    assert work.journal == "Journal of Political Economy"
    assert work.year == 2021
    assert work.authors == ["Anna Müller", "Bo Lindqvist"]


def test_openalex_yields_metadata_and_normalises_the_doi():
    works = openalex_works(fixture("openalex_work.json"))

    assert works[0].doi == "10.1234/exchange.2021"  # the https:// wrapper is stripped
    assert works[0].journal == "Journal of Political Economy"


def test_unpaywall_labels_each_version():
    versions = unpaywall_versions(fixture("unpaywall_open.json"))

    labels = {v.version_type for v in versions}
    assert labels == {PUBLISHED, ACCEPTED_MANUSCRIPT}
    assert all(v.retrievable for v in versions)


def test_a_paywalled_work_yields_a_non_retrievable_version():
    versions = unpaywall_versions(fixture("unpaywall_paywalled.json"))

    assert len(versions) == 1
    assert versions[0].retrievable is False
    assert versions[0].reason == "paywalled"


def test_arxiv_yields_a_preprint():
    versions = arxiv_versions(fixture("arxiv_entries.json"))

    assert versions[0].version_type == SUBMITTED_PREPRINT
    assert versions[0].retrievable is True
    assert arxiv_works(fixture("arxiv_entries.json"))[0].title.startswith("A Preprint")


# ------------------------------------------------------------------ fetchable


def test_only_unpaywall_and_arxiv_urls_are_fetchable():
    unpaywall = unpaywall_versions(fixture("unpaywall_open.json"))[0]
    arxiv = arxiv_versions(fixture("arxiv_entries.json"))[0]

    assert is_fetchable(unpaywall)
    assert is_fetchable(arxiv)


def test_a_crossref_or_openalex_url_is_never_fetchable():
    """Metadata providers may not hand the fetcher a URL."""
    for source in ("crossref", "openalex", "user"):
        version = Version(
            version_type=PUBLISHED,
            url="https://journals.example.org/paper.pdf",
            retrievable=True,
            source=source,
        )
        assert not is_fetchable(version)


def test_an_arxiv_url_hosted_elsewhere_is_refused():
    """The arXiv source may only produce arXiv-hosted URLs."""
    version = arxiv_versions(fixture("arxiv_offsite.json"))[0]

    assert version.retrievable is False
    assert not is_fetchable(version)


def test_a_non_https_url_is_never_fetchable():
    version = Version(
        version_type=PUBLISHED,
        url="http://journals.example.org/paper.pdf",
        retrievable=True,
        source="unpaywall",
    )

    assert not is_fetchable(version)


def test_doi_org_is_not_a_download_source():
    """It is a redirector; following it would be landing-page scraping."""
    version = unpaywall_versions(fixture("unpaywall_paywalled.json"))[0]

    assert version.url == "https://doi.org/10.5555/closed.2019"
    assert not is_fetchable(version)


# -------------------------------------------------------------------- ranking


def test_a_published_open_version_outranks_a_preprint():
    versions = rank_versions(
        arxiv_versions(fixture("arxiv_entries.json"))
        + unpaywall_versions(fixture("unpaywall_open.json"))
    )

    assert versions[0].version_type == PUBLISHED
    assert versions[-1].version_type == SUBMITTED_PREPRINT


def test_a_retrievable_preprint_outranks_a_paywalled_published_version():
    """Authority does not help if it cannot lawfully be offered."""
    paywalled = unpaywall_versions(fixture("unpaywall_paywalled.json"))
    preprint = arxiv_versions(fixture("arxiv_entries.json"))

    ranked = rank_versions(paywalled + preprint)

    assert ranked[0].version_type == SUBMITTED_PREPRINT
    assert ranked[0].retrievable is True


def test_an_exact_doi_match_ranks_first():
    other = Work(title="Something Else", doi="10.9999/other", source="crossref")
    wanted = crossref_works(fixture("crossref_work.json"))[0]
    source = Recorded("test", works_payload=[other, wanted])

    resolution = resolve("10.1234/exchange.2021", metadata_sources=[source])

    assert resolution.candidates[0].work.doi == "10.1234/exchange.2021"


# ------------------------------------------------------------------- resolve


def test_resolution_opens_no_socket(no_network):
    """Recorded sources must be the only thing resolution consults."""
    works = Recorded("crossref", works_payload=crossref_works(fixture("crossref_work.json")))
    versions = Recorded(
        "unpaywall", versions_payload=unpaywall_versions(fixture("unpaywall_open.json"))
    )

    result = resolve("exchange rate", metadata_sources=[works], version_sources=[versions])

    assert result.candidates  # and no AssertionError from the guard


def test_resolution_writes_nothing(tmp_path, no_network):
    """The invariant: abandoning a search leaves the project untouched."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "marker").write_text("unchanged")
    before = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}

    source = Recorded(
        "crossref", works_payload=crossref_works(fixture("crossref_work.json"))
    )
    versions = Recorded(
        "unpaywall", versions_payload=unpaywall_versions(fixture("unpaywall_open.json"))
    )
    result = resolve("exchange rate", metadata_sources=[source], version_sources=[versions])

    assert result.candidates
    assert result.to_json()["wrote_anything"] is False
    after = {p: p.read_bytes() for p in project.rglob("*") if p.is_file()}
    assert after == before


def test_candidates_carry_everything_the_confirmation_must_show():
    works = Recorded("crossref", works_payload=crossref_works(fixture("crossref_work.json")))
    versions = Recorded(
        "unpaywall", versions_payload=unpaywall_versions(fixture("unpaywall_open.json"))
    )

    candidate = resolve(
        "exchange rate", metadata_sources=[works], version_sources=[versions]
    ).candidates[0].to_json()

    assert candidate["work"]["title"]
    assert candidate["work"]["authors"]
    assert candidate["work"]["year"]
    assert candidate["work"]["journal"]
    assert candidate["work"]["doi"]
    assert candidate["versions"][0]["version_type"]
    assert candidate["versions"][0]["url"]


def test_two_providers_describing_one_work_merge_on_doi():
    crossref = Recorded("crossref", works_payload=crossref_works(fixture("crossref_work.json")))
    openalex = Recorded("openalex", works_payload=openalex_works(fixture("openalex_work.json")))

    result = resolve("exchange rate", metadata_sources=[crossref, openalex])

    assert len(result.candidates) == 1


def test_a_provider_failure_is_reported_not_invented_around():
    broken = Recorded("crossref", fails=True)

    result = resolve("exchange rate", metadata_sources=[broken])

    assert result.candidates == []
    assert any("did not answer" in warning for warning in result.warnings)


def test_no_match_says_so():
    result = resolve("nothing matches this", metadata_sources=[Recorded("crossref")])

    assert result.candidates == []
    assert "No matching work was found." in result.warnings


def test_an_empty_query_is_refused_without_calling_a_provider():
    result = resolve("   ", metadata_sources=[Recorded("crossref", fails=True)])

    assert result.candidates == []
    assert result.warnings == ["Enter a title, citation or DOI."]


def test_a_version_from_a_forbidden_source_is_shown_but_not_offered():
    """Displayed as a link the user may open, never as a download."""
    works = Recorded("crossref", works_payload=crossref_works(fixture("crossref_work.json")))
    smuggled = Recorded(
        "openalex",
        versions_payload=[
            Version(
                version_type=PUBLISHED,
                url="https://journals.example.org/paper.pdf",
                retrievable=True,
                source="openalex",
            )
        ],
    )

    candidate = resolve(
        "exchange rate", metadata_sources=[works], version_sources=[smuggled]
    ).candidates[0]

    assert candidate.versions[0].retrievable is False
    assert candidate.versions[0].reason == "not a permitted source"
    assert candidate.retrievable is False


# ----------------------------------------------------------------------- DOI


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("10.1234/abc", "10.1234/abc"),
        ("https://doi.org/10.1234/abc", "10.1234/abc"),
        ("doi:10.1234/ABC", "10.1234/abc"),
        ("  10.1234/abc.  ", "10.1234/abc"),
        ("not a doi", None),
        (None, None),
    ],
)
def test_doi_normalisation(raw, expected):
    assert normalize_doi(raw) == expected


def test_looks_like_doi():
    assert looks_like_doi("10.1234/abc")
    assert not looks_like_doi("exchange rate disconnect")


# ---------------------------------------------------------------- http route


def test_the_resolve_route_writes_nothing(client, services):
    """A live project must be safe to search against."""
    root = services.workspace.root
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}

    response = client.post("/api/acquisition/resolve", json={"query": "10.1234/abc"})

    assert response.status_code == 200
    assert response.json()["wrote_anything"] is False
    after = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert after == before
    assert client.get("/api/documents").json()["documents"] == []
    assert client.get("/api/references").json()["references"] == []


def test_with_no_provider_configured_the_route_says_so(client):
    payload = client.post("/api/acquisition/resolve", json={"query": "anything"}).json()

    assert payload["candidates"] == []
    assert "import a PDF you already have" in " ".join(payload["warnings"])


# ------------------------------------------------------- confirm to import


def _configure(services, works, versions):
    services._metadata_sources = [Recorded("crossref", works_payload=works)]
    services._version_sources = [Recorded("unpaywall", versions_payload=versions)]


def test_an_unoffered_url_is_refused(client, services):
    """A caller cannot hand the fetcher an address by claiming a source."""
    _configure(
        services,
        crossref_works(fixture("crossref_work.json")),
        unpaywall_versions(fixture("unpaywall_open.json")),
    )

    response = client.post(
        "/api/acquisition/download",
        json={"query": "exchange rate", "url": "https://evil.example/payload.pdf"},
    )

    assert response.status_code == 422
    assert "was not offered" in response.text
    assert client.get("/api/documents").json()["documents"] == []


def test_a_paywalled_version_cannot_be_downloaded(client, services):
    _configure(
        services,
        crossref_works(fixture("crossref_work.json")),
        unpaywall_versions(fixture("unpaywall_paywalled.json")),
    )

    response = client.post(
        "/api/acquisition/download",
        json={"query": "closed", "url": "https://doi.org/10.5555/closed.2019"},
    )

    assert response.status_code == 422
    assert client.get("/api/documents").json()["documents"] == []


def test_a_known_doi_short_circuits_before_any_download(client, services, paper_one):
    """No fetch is attempted when the DOI is already in the library."""
    document_id = upload(client, "have-it.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    client.patch(f"/api/documents/{document_id}", json={"year": 2021})
    services.documents.update_metadata(document_id, doi="10.1234/exchange.2021")
    _configure(
        services,
        crossref_works(fixture("crossref_work.json")),
        unpaywall_versions(fixture("unpaywall_open.json")),
    )

    payload = client.post(
        "/api/acquisition/download",
        json={
            "query": "exchange rate",
            "url": "https://journals.example.org/jpe/exchange.pdf",
        },
    ).json()

    assert payload["duplicate"] is True
    assert payload["matched_on"] == "doi"
    assert payload["document"]["document_id"] == document_id


# --------------------------------------- predicted name and the pinned work


def test_the_predicted_name_matches_what_the_import_produces(client, services, paper_one):
    """The card promises a filename; the download must honour it."""
    predicted = services.documents.predict_canonical_name(
        title="Exchange Rate Disconnect",
        authors="Anna Müller and Bo Lindqvist",
        year=2021,
        year_confirmed=True,
    )

    document_id = upload(client, "whatever.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    actual = services.documents.update_metadata(
        document_id,
        title="Exchange Rate Disconnect",
        authors="Anna Müller and Bo Lindqvist",
        year=2021,
    )

    assert predicted == "Müller, Lindqvist (2021) – Exchange Rate Disconnect.pdf"
    assert actual.pdf["canonical_name"] == predicted


def test_an_unconfirmed_year_is_predicted_as_nd(services):
    predicted = services.documents.predict_canonical_name(
        title="A Work", authors="Alice Writer", year=2016, year_confirmed=False
    )

    assert "(n.d.)" in predicted


def test_a_taken_name_is_predicted_with_a_suffix(client, services, paper_one):
    document_id = upload(client, "a.pdf", paper_one)["imported"][0]["document"][
        "document_id"
    ]
    services.documents.update_metadata(document_id, title="Shared", authors="A Writer")

    predicted = services.documents.predict_canonical_name(
        title="Shared", authors="A Writer", year=None, year_confirmed=False
    )

    assert predicted.endswith("(2).pdf")


def test_a_swapped_work_at_the_same_url_is_refused(client, services):
    """Pinning the URL alone would import a paper nobody confirmed."""
    _configure(
        services,
        crossref_works(fixture("crossref_work.json")),
        unpaywall_versions(fixture("unpaywall_open.json")),
    )
    url = "https://journals.example.org/jpe/exchange.pdf"

    with pytest.raises(Exception) as caught:
        services.acquire_paper(url=url, query="exchange rate", expect_doi="10.9999/different")

    assert "no longer matches" in str(caught.value)
    assert client.get("/api/documents").json()["documents"] == []


def test_the_expected_doi_lets_the_right_work_through(services):
    _configure(
        services,
        crossref_works(fixture("crossref_work.json")),
        unpaywall_versions(fixture("unpaywall_open.json")),
    )

    # The matching DOI selects the candidate; the fetch itself is not reached
    # here because no transport is configured, so a refusal proves selection.
    with pytest.raises(Exception) as caught:
        services.acquire_paper(
            url="https://journals.example.org/jpe/exchange.pdf",
            query="exchange rate",
            expect_doi="10.1234/exchange.2021",
        )

    assert "no longer matches" not in str(caught.value)
