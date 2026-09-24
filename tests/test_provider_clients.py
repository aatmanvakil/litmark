"""The four live clients, exercised entirely offline.

Every response is a recorded fixture and the transport is injected, so no
socket is opened. A guard asserts that rather than trusting it.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from litmark.acquisition.arxiv_atom import parse_feed
from litmark.acquisition.clients import (
    ArxivSource,
    CrossrefSource,
    OpenAlexSource,
    UnpaywallSource,
    build_sources,
    user_agent,
)
from litmark.acquisition.config import OPENALEX_KEY_ENV, AcquisitionConfig
from litmark.acquisition.fetch import FetchRefused
from litmark.acquisition.limits import MIN_INTERVAL, RateLimiter
from litmark.acquisition.model import Work
from litmark.acquisition.redact import safe_url

FIXTURES = Path(__file__).parent / "fixtures" / "acquisition"
KEY = "sk-live-NEVER-REAL-9876543210"
MAILTO = "someone@example.edu"


def load(name):
    text = (FIXTURES / name).read_text("utf-8")
    return json.loads(text) if name.endswith(".json") else text


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a provider client opened a real socket")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Calls:
    """Records every request a client makes, and replies from a fixture."""

    def __init__(self, reply=None, raises=None):
        self.urls: list[str] = []
        self.headers: list[dict] = []
        self._reply = reply
        self._raises = raises

    def __call__(self, url, headers=None, **kwargs):
        self.urls.append(url)
        self.headers.append(dict(headers or {}))
        if self._raises is not None:
            raise self._raises
        return self._reply


def config(**overrides):
    settings = {
        "acquisition": {
            "mailto": MAILTO,
            "crossref": True,
            "openalex": True,
            "unpaywall": True,
            "arxiv": True,
            **overrides,
        }
    }
    return AcquisitionConfig.from_settings(settings, environ={OPENALEX_KEY_ENV: KEY})


def limiter():
    """A limiter with a fake clock, so the arXiv gate costs no wall time."""
    return RateLimiter(sleep=lambda _: None, now=lambda: 0.0)


# ------------------------------------------------------------- Crossref


def test_crossref_looks_a_doi_up_by_path():
    calls = Calls(load("crossref_work.json"))
    source = CrossrefSource(config=config(), limiter=limiter(), get_json=calls)

    works = source.works("10.1234/exchange.2021")

    assert works[0].doi == "10.1234/exchange.2021"
    assert "/works/10.1234%2Fexchange.2021" in calls.urls[0]


def test_crossref_searches_a_title():
    calls = Calls(load("crossref_search.json"))
    source = CrossrefSource(config=config(), limiter=limiter(), get_json=calls)

    works = source.works("exchange rate disconnect")

    assert len(works) == 2
    assert "query.bibliographic=exchange+rate+disconnect" in calls.urls[0]


def test_crossref_asks_for_the_polite_pool():
    """mailto in the parameter and in the User-Agent, as documented."""
    calls = Calls(load("crossref_work.json"))
    source = CrossrefSource(config=config(), limiter=limiter(), get_json=calls)

    source.works("10.1234/exchange.2021")

    assert f"mailto={MAILTO.replace('@', '%40')}" in calls.urls[0]
    assert f"mailto:{MAILTO}" in calls.headers[0]["User-Agent"]


def test_the_user_agent_identifies_the_project():
    assert user_agent(MAILTO).startswith("litmark/")
    assert "github.com" in user_agent(None)
    assert "mailto" not in user_agent(None)


# ------------------------------------------------------------- OpenAlex


def test_openalex_sends_the_key_as_a_header_never_in_the_url():
    """OpenAlex accepts api_key= in the URL; a URL reaches logs, so: header."""
    calls = Calls(load("openalex_work.json"))
    source = OpenAlexSource(config=config(), limiter=limiter(), get_json=calls)

    source.works("exchange rate")

    assert calls.headers[0]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in calls.urls[0]
    assert "api_key" not in calls.urls[0]
    assert KEY not in safe_url(calls.urls[0])


def test_openalex_filters_by_doi_when_given_one():
    calls = Calls(load("openalex_work.json"))
    source = OpenAlexSource(config=config(), limiter=limiter(), get_json=calls)

    source.works("10.1234/exchange.2021")

    assert "filter=doi" in calls.urls[0]


def test_openalex_without_a_key_is_not_built():
    without = AcquisitionConfig.from_settings(
        {"acquisition": {"mailto": MAILTO, "crossref": True, "openalex": True}},
        environ={},
    )

    metadata, _ = build_sources(without)

    assert [s.name for s in metadata] == ["crossref"]


# ------------------------------------------------------------ Unpaywall


def test_unpaywall_looks_up_versions_by_doi():
    calls = Calls(load("unpaywall_open.json"))
    source = UnpaywallSource(config=config(), limiter=limiter(), get_json=calls)

    versions = source.versions(Work(doi="10.1234/exchange.2021"))

    assert len(versions) == 2
    assert f"email={MAILTO.replace('@', '%40')}" in calls.urls[0]


def test_unpaywall_is_skipped_for_a_work_with_no_doi():
    calls = Calls(load("unpaywall_open.json"))
    source = UnpaywallSource(config=config(), limiter=limiter(), get_json=calls)

    assert source.versions(Work(title="No DOI")) == []
    assert calls.urls == []


def test_unpaywall_is_skipped_without_a_contact_address():
    no_mail = AcquisitionConfig.from_settings(
        {"acquisition": {"unpaywall": True}}, environ={}
    )
    calls = Calls(load("unpaywall_open.json"))
    source = UnpaywallSource(config=no_mail, limiter=limiter(), get_json=calls)

    assert source.versions(Work(doi="10.1/x")) == []
    assert calls.urls == []


def test_an_unknown_doi_is_not_an_error():
    calls = Calls(raises=FetchRefused("not_found", status=404))
    source = UnpaywallSource(config=config(), limiter=limiter(), get_json=calls)

    assert source.versions(Work(doi="10.1/unknown")) == []


def test_a_real_unpaywall_failure_still_propagates():
    calls = Calls(raises=FetchRefused("rate_limited", status=429))
    source = UnpaywallSource(config=config(), limiter=limiter(), get_json=calls)

    with pytest.raises(FetchRefused):
        source.versions(Work(doi="10.1/x"))


# ---------------------------------------------------------------- arXiv


def test_arxiv_parses_an_atom_feed():
    calls = Calls(load("arxiv_feed.xml"))
    source = ArxivSource(config=config(), limiter=limiter(), get_text=calls)

    versions = source.versions(Work(title="Exchange Rates"))

    assert versions[0].url == "https://arxiv.org/pdf/2007.00001v1"
    assert versions[0].retrievable is True


def test_arxiv_works_carry_authors_and_year():
    works = parse_feed(load("arxiv_feed.xml"))

    assert works[0]["authors"] == ["Anna Müller", "Bo Lindqvist"]
    assert works[0]["published"].startswith("2020")


def test_an_empty_arxiv_feed_yields_nothing():
    calls = Calls(load("arxiv_empty.xml"))
    source = ArxivSource(config=config(), limiter=limiter(), get_text=calls)

    assert source.versions(Work(title="Nothing")) == []


def test_malformed_atom_raises_a_plain_value_error():
    """A caller should not have to know it is XML underneath."""
    with pytest.raises(ValueError):
        parse_feed("<feed><unclosed>")


def test_an_entity_expansion_attack_is_refused():
    """arXiv content is third-party; ElementTree would happily expand this."""
    with pytest.raises(ValueError):
        parse_feed(load("arxiv_billion_laughs.xml"))


def test_arxiv_does_not_search_for_a_doi():
    calls = Calls(load("arxiv_feed.xml"))
    source = ArxivSource(config=config(), limiter=limiter(), get_text=calls)

    assert source.works("10.1234/exchange.2021") == []
    assert calls.urls == []


# --------------------------------------------------------- rate limiting


def test_arxiv_is_paced_at_three_seconds():
    """Its terms ask for one request every three seconds."""
    assert MIN_INTERVAL["arxiv"] == 3.0


def test_the_gate_waits_between_arxiv_calls():
    slept: list[float] = []
    clock = {"t": 0.0}

    def sleep(seconds):
        slept.append(seconds)
        clock["t"] += seconds

    gate = RateLimiter(sleep=sleep, now=lambda: clock["t"])
    gate.wait("arxiv")
    gate.wait("arxiv")

    assert slept == [3.0]


def test_a_cooldown_is_observed_before_spending_a_call():
    gate = limiter()
    gate.back_off("crossref", 120.0)
    calls = Calls(load("crossref_work.json"))
    source = CrossrefSource(config=config(), limiter=gate, get_json=calls)

    with pytest.raises(FetchRefused) as caught:
        source.works("10.1/x")

    assert caught.value.reason == "rate_limited"
    assert calls.urls == [], "a call was spent during a cooldown"


def test_a_rate_limit_without_retry_after_still_pauses():
    gate = limiter()

    gate.back_off("openalex", None)

    assert gate.cooling_down("openalex") > 0


# ------------------------------------------------------------ redaction


def test_no_client_url_carries_the_key():
    calls = Calls(load("openalex_work.json"))
    OpenAlexSource(config=config(), limiter=limiter(), get_json=calls).works("x")

    for url in calls.urls:
        assert KEY not in url


def test_a_logged_unpaywall_url_hides_the_address(caplog):
    calls = Calls(load("unpaywall_open.json"))
    source = UnpaywallSource(config=config(), limiter=limiter(), get_json=calls)

    with caplog.at_level("DEBUG"):
        source.versions(Work(doi="10.1234/exchange.2021"))

    assert MAILTO not in caplog.text
    assert "REDACTED" in caplog.text


def test_a_logged_openalex_call_never_shows_the_key(caplog):
    calls = Calls(load("openalex_work.json"))
    source = OpenAlexSource(config=config(), limiter=limiter(), get_json=calls)

    with caplog.at_level("DEBUG"):
        source.works("exchange rate")

    assert KEY not in caplog.text


# ------------------------------------------------------------- assembly


def test_all_four_are_built_when_available():
    metadata, versions = build_sources(config())

    assert [s.name for s in metadata] == ["crossref", "openalex"]
    assert [s.name for s in versions] == ["unpaywall", "arxiv"]


def test_disabled_providers_are_not_built():
    metadata, versions = build_sources(config(crossref=False, arxiv=False))

    assert [s.name for s in metadata] == ["openalex"]
    assert [s.name for s in versions] == ["unpaywall"]


def test_nothing_enabled_builds_nothing():
    empty = AcquisitionConfig.from_settings({}, environ={})

    assert build_sources(empty) == ([], [])


def test_the_sources_share_one_limiter():
    """Otherwise each client would pace itself and the gate would not hold."""
    shared = limiter()
    metadata, versions = build_sources(config(), limiter=shared)

    assert all(source.limiter is shared for source in metadata + versions)


# ------------------------------------------------- independent degradation


class Failing:
    """A source that always raises, to prove the others survive it."""

    def __init__(self, name, error):
        self.name = name
        self._error = error

    def works(self, query):
        raise self._error

    def versions(self, work):
        raise self._error


class Fixed:
    def __init__(self, name, works=(), versions=()):
        self.name = name
        self._works = list(works)
        self._versions = list(versions)

    def works(self, query):
        return list(self._works)

    def versions(self, work):
        return list(self._versions)


def test_one_dead_metadata_provider_does_not_kill_the_search():
    from litmark.acquisition import resolve
    from litmark.acquisition.providers import crossref_works

    good = Fixed("crossref", works=crossref_works(load("crossref_work.json")))
    bad = Failing("openalex", FetchRefused("rate_limited", retry_after=90))

    result = resolve("exchange rate", metadata_sources=[bad, good])

    assert result.candidates, "a failing provider emptied the results"
    assert result.failed_providers == ["openalex"]
    assert any("openalex" in w for w in result.warnings)


def test_one_dead_version_provider_leaves_the_others_attached():
    from litmark.acquisition import resolve
    from litmark.acquisition.providers import crossref_works, unpaywall_versions

    works = Fixed("crossref", works=crossref_works(load("crossref_work.json")))
    good = Fixed("unpaywall", versions=unpaywall_versions(load("unpaywall_open.json")))
    bad = Failing("arxiv", FetchRefused("timeout"))

    result = resolve(
        "exchange rate", metadata_sources=[works], version_sources=[bad, good]
    )

    assert result.candidates[0].versions, "a version provider's failure lost the rest"
    assert result.failed_providers == ["arxiv"]


def test_a_rate_limit_warning_says_how_long_to_wait():
    from litmark.acquisition import resolve

    bad = Failing("crossref", FetchRefused("rate_limited", retry_after=90.0))

    result = resolve("x", metadata_sources=[bad])

    assert "about 90s" in " ".join(result.warnings)


def test_a_warning_never_leaks_a_key_or_address():
    from litmark.acquisition import resolve

    bad = Failing(
        "openalex",
        FetchRefused("http_error", url=f"https://api.openalex.org/w?api_key={KEY}"),
    )

    result = resolve("x", metadata_sources=[bad])

    assert KEY not in " ".join(result.warnings)


def test_every_provider_failing_is_reported_not_silent():
    from litmark.acquisition import resolve

    result = resolve(
        "x",
        metadata_sources=[
            Failing("crossref", FetchRefused("timeout")),
            Failing("openalex", FetchRefused("access_denied")),
        ],
    )

    assert result.candidates == []
    assert sorted(result.failed_providers) == ["crossref", "openalex"]


# ----------------------------------------------------------------- dedup


def test_the_same_doi_from_two_providers_merges():
    from litmark.acquisition import resolve
    from litmark.acquisition.providers import crossref_works, openalex_works

    crossref = Fixed("crossref", works=crossref_works(load("crossref_work.json")))
    openalex = Fixed("openalex", works=openalex_works(load("openalex_work.json")))

    result = resolve("exchange rate", metadata_sources=[crossref, openalex])

    assert len(result.candidates) == 1


def test_a_doi_less_duplicate_merges_on_title_and_year():
    """arXiv and Crossref describe one preprint; only one has a DOI."""
    from litmark.acquisition import resolve

    with_doi = Work(
        title="Exchange Rate Disconnect", year=2021, doi="10.1/x", source="crossref"
    )
    without = Work(title="exchange   rate disconnect!", year=2021, source="arxiv")

    result = resolve(
        "exchange rate", metadata_sources=[Fixed("a", [with_doi]), Fixed("b", [without])]
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].work.doi == "10.1/x"


def test_a_different_year_is_a_different_work():
    from litmark.acquisition import resolve

    first = Work(title="Same Title", year=2020, source="a")
    second = Work(title="Same Title", year=2021, source="b")

    result = resolve("x", metadata_sources=[Fixed("a", [first]), Fixed("b", [second])])

    assert len(result.candidates) == 2


def test_merging_fills_gaps_without_overwriting():
    from litmark.acquisition import resolve

    sparse = Work(title="A Work", doi="10.1/x", source="crossref")
    rich = Work(title="A Work", doi="10.1/x", year=2021, journal="JPE", source="openalex")

    result = resolve("x", metadata_sources=[Fixed("a", [sparse]), Fixed("b", [rich])])

    merged = result.candidates[0].work
    assert (merged.year, merged.journal) == (2021, "JPE")
    assert merged.title == "A Work"


def test_title_key_ignores_punctuation_and_case():
    from litmark.acquisition import title_key

    assert title_key(Work(title="The Cost-of-Living Index!", year=2021)) == title_key(
        Work(title="the cost of living index", year=2021)
    )


def test_a_work_with_no_title_or_doi_is_kept_not_merged():
    from litmark.acquisition import resolve

    blank = Work(source="a")
    other = Work(source="b")

    result = resolve("x", metadata_sources=[Fixed("a", [blank]), Fixed("b", [other])])

    assert len(result.candidates) == 2
