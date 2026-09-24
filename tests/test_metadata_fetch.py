"""The metadata chokepoint: four known hosts, nothing else.

`fetch_pdf` correctly has no host allowlist, because a PDF URL comes from a
provider and cannot be enumerated. Metadata goes to exactly four hosts, so it
can be pinned to them — and these tests hold it there.
"""

from __future__ import annotations

import httpx
import pytest

from litmark.acquisition.fetch import (
    MAX_METADATA_BYTES,
    METADATA_HOSTS,
    FetchRefused,
    fetch_json,
    fetch_metadata,
    fetch_text,
    PinnedAddressTransport,
)
from litmark.acquisition.redact import PLACEHOLDER, safe_details, safe_url

PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.5"
SECRET = "sk-live-do-not-leak-0123456789"


class Recorder(httpx.BaseTransport):
    def __init__(self, responses=None):
        self.targets: list[str] = []
        self.requests: list[httpx.Request] = []
        self._responses = list(responses or [])

    def handle_request(self, request):
        self.targets.append(request.url.host)
        self.requests.append(request)
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(
            200, headers={"content-type": "application/json"}, content=b'{"ok": true}'
        )

    def close(self):
        pass


def resolver_for(*addresses):
    calls = {"n": 0}

    def resolve(hostname):
        calls["n"] += 1
        return list(addresses) or [PUBLIC]

    resolve.calls = calls
    return resolve


def transport(responses=None):
    inner = Recorder(responses)
    return PinnedAddressTransport(resolver=resolver_for(PUBLIC), inner=inner), inner


# ------------------------------------------------------------- allowlist


@pytest.mark.parametrize("host", sorted(METADATA_HOSTS))
def test_each_known_provider_host_is_permitted(host):
    pinned, inner = transport()

    fetch_json(f"https://{host}/x", transport=pinned)

    assert inner.targets == [PUBLIC]


def test_an_unlisted_host_is_refused_before_connecting():
    pinned, inner = transport()

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://evil.example/works", transport=pinned)

    assert caught.value.reason == "host_not_allowed"
    assert inner.targets == [], "a refused host was still contacted"


def test_a_redirect_off_the_allowlist_is_refused():
    pinned, inner = transport(
        [httpx.Response(302, headers={"location": "https://evil.example/x"})]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.crossref.org/works/10.1", transport=pinned)

    assert caught.value.reason == "host_not_allowed"
    assert len(inner.targets) == 1, "the redirect was followed off the allowlist"


def test_a_redirect_within_the_allowlist_is_followed():
    pinned, inner = transport(
        [httpx.Response(302, headers={"location": "https://api.openalex.org/works"})]
    )

    fetch_json("https://api.crossref.org/works", transport=pinned)

    assert len(inner.targets) == 2


def test_http_is_refused():
    pinned, _ = transport()

    with pytest.raises(FetchRefused) as caught:
        fetch_json("http://api.crossref.org/works", transport=pinned)

    assert caught.value.reason == "not_https"


def test_a_private_address_is_refused_even_for_a_listed_host():
    pinned = PinnedAddressTransport(resolver=resolver_for(PRIVATE), inner=Recorder())

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.crossref.org/works", transport=pinned)

    assert caught.value.reason == "blocked_address"


# ------------------------------------------------------- body validation


def test_a_non_json_content_type_is_refused():
    pinned, _ = transport(
        [httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.crossref.org/works", transport=pinned)

    assert caught.value.reason == "unexpected_content_type"


def test_malformed_json_is_refused_not_raised_raw():
    pinned, _ = transport(
        [
            httpx.Response(
                200, headers={"content-type": "application/json"}, content=b'{"trunc'
            )
        ]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.crossref.org/works", transport=pinned)

    assert caught.value.reason == "malformed_json"


def test_an_oversized_body_is_aborted():
    pinned, _ = transport(
        [
            httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b"[" + b"0," * MAX_METADATA_BYTES,
            )
        ]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.crossref.org/works", transport=pinned)

    assert caught.value.reason == "too_large"


def test_atom_is_accepted_for_arxiv():
    pinned, _ = transport(
        [
            httpx.Response(
                200,
                headers={"content-type": "application/atom+xml"},
                content=b"<feed/>",
            )
        ]
    )

    assert fetch_text("https://export.arxiv.org/api/query", transport=pinned) == "<feed/>"


def test_json_is_refused_where_atom_is_expected():
    pinned, _ = transport(
        [httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}")]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_text("https://export.arxiv.org/api/query", transport=pinned)

    assert caught.value.reason == "unexpected_content_type"


# --------------------------------------------------------- rate limiting


@pytest.mark.parametrize("status", [429, 503])
def test_a_rate_limit_is_reported_not_retried(status):
    """One lookup, one attempt: retrying here would make a storm."""
    pinned, inner = transport(
        [httpx.Response(status, headers={"retry-after": "120"})]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.openalex.org/works", transport=pinned)

    assert caught.value.reason == "rate_limited"
    assert caught.value.details["retry_after"] == 120.0
    assert caught.value.details["status"] == status
    assert len(inner.targets) == 1, "the request was retried"


def test_a_rate_limit_without_retry_after_is_still_handled():
    pinned, _ = transport([httpx.Response(429)])

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.openalex.org/works", transport=pinned)

    assert caught.value.details["retry_after"] is None


def test_an_http_date_retry_after_does_not_crash():
    pinned, _ = transport(
        [httpx.Response(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})]
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.openalex.org/works", transport=pinned)

    assert caught.value.details["retry_after"] == 0.0


def test_a_denied_key_is_reported_as_access_denied():
    pinned, _ = transport([httpx.Response(401)])

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.openalex.org/works", transport=pinned)

    assert caught.value.reason == "access_denied"


def test_a_missing_record_is_distinguishable():
    pinned, _ = transport([httpx.Response(404)])

    with pytest.raises(FetchRefused) as caught:
        fetch_json("https://api.unpaywall.org/v2/10.1/x", transport=pinned)

    assert caught.value.reason == "not_found"


def test_a_timeout_is_refused_not_raised_raw():
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    pinned, _ = transport()

    with pytest.raises(FetchRefused) as caught:
        fetch_json(
            "https://api.crossref.org/works", transport=pinned, now=lambda: next(clock)
        )

    assert caught.value.reason == "timeout"


# ------------------------------------------------------------- redaction


def test_a_key_never_survives_into_a_printed_url():
    assert SECRET not in safe_url(f"https://api.openalex.org/works?api_key={SECRET}")
    assert PLACEHOLDER in safe_url(f"https://api.openalex.org/works?api_key={SECRET}")


def test_an_email_never_survives_into_a_printed_url():
    dirty = "https://api.unpaywall.org/v2/10.1/x?email=someone@university.edu"

    assert "someone@university.edu" not in safe_url(dirty)


def test_a_url_without_secrets_is_left_alone():
    clean = "https://api.crossref.org/works/10.1257/aer.20150572"

    assert safe_url(clean) == clean


def test_error_details_are_redacted_on_the_way_in():
    """A key must not escape through an exception that is logged or shown."""
    error = FetchRefused(
        "http_error", url=f"https://api.openalex.org/w?api_key={SECRET}", email="me@x.edu"
    )

    rendered = f"{error} {error.details}"

    assert SECRET not in rendered
    assert "me@x.edu" not in rendered


def test_an_authorization_header_is_never_rendered():
    cleaned = safe_details({"authorization": f"Bearer {SECRET}"})

    assert SECRET not in str(cleaned)


def test_the_key_never_reaches_the_url_when_sent_as_a_header():
    """The point of using a header: the key cannot end up in a log line."""
    pinned, inner = transport()

    fetch_json(
        "https://api.openalex.org/works?search=x",
        transport=pinned,
        headers={"Authorization": f"Bearer {SECRET}"},
    )

    sent = inner.requests[0]
    assert SECRET not in str(sent.url)
    assert sent.headers["Authorization"] == f"Bearer {SECRET}"
    assert SECRET not in safe_url(str(sent.url))
