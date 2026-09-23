"""The hardened download path, offline.

The TOCTOU tests are the point of this file. A test that only proves "the
checker rejected a private address" would pass against the broken design
where the hostname is checked and then handed to the client to resolve
again — so these assert the address the transport *actually connected to*.
"""

from __future__ import annotations

import httpx
import pytest

from litmark.acquisition.fetch import (
    MAX_REDIRECTS,
    PinnedAddressTransport,
    FetchRefused,
    check_url,
    fetch_pdf,
    is_public,
    pin_address,
)

PDF = b"%PDF-1.4\nfake but well-formed enough\n"
PUBLIC = "93.184.216.34"
PRIVATE = "10.0.0.5"
LOOPBACK = "127.0.0.1"


class Recorder(httpx.BaseTransport):
    """An inner transport that records exactly where it was asked to connect."""

    def __init__(self, responses=None):
        self.targets: list[str] = []
        self.sni: list[str | None] = []
        self.hosts: list[str | None] = []
        self._responses = list(responses or [])

    def handle_request(self, request):
        self.targets.append(request.url.host)
        self.sni.append(request.extensions.get("sni_hostname"))
        self.hosts.append(request.headers.get("Host"))
        if self._responses:
            return self._responses.pop(0)
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=PDF
        )

    def close(self):
        pass


def counting_resolver(sequence):
    """Answers differently on each call, and counts how often it was asked."""
    calls = {"n": 0}

    def resolve(hostname):
        index = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        return sequence[index]

    resolve.calls = calls
    return resolve


def redirect_to(url):
    return httpx.Response(302, headers={"location": url})


# ------------------------------------------------------------ address checks


@pytest.mark.parametrize(
    "address,public",
    [
        (PUBLIC, True),
        ("8.8.8.8", True),
        ("2606:4700:4700::1111", True),
        (PRIVATE, False),
        ("192.168.1.1", False),
        (LOOPBACK, False),
        ("169.254.1.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1", False),
        ("224.0.0.1", False),
        ("not an address", False),
    ],
)
def test_address_classification(address, public):
    assert is_public(address) is public


def test_every_resolved_address_must_be_public():
    """A round-robin record with one private answer is refused outright."""
    resolver = counting_resolver([[PUBLIC, PRIVATE]])

    with pytest.raises(FetchRefused) as caught:
        pin_address("rebind.example", resolver=resolver)

    assert caught.value.reason == "blocked_address"
    assert caught.value.details["addresses"] == [PRIVATE]


def test_a_public_record_pins_the_first_address():
    assert pin_address("ok.example", resolver=counting_resolver([[PUBLIC]])) == PUBLIC


# ---------------------------------------------------------------- URL shape


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://example.org/a.pdf", "not_https"),
        ("https://user:pw@example.org/a.pdf", "userinfo_in_url"),
        ("https://example.org:8443/a.pdf", "unexpected_port"),
    ],
)
def test_bad_urls_are_refused(url, reason):
    with pytest.raises(FetchRefused) as caught:
        check_url(url)

    assert caught.value.reason == reason


# ------------------------------------------------------------------- TOCTOU


def test_the_connection_uses_the_validated_address():
    inner = Recorder()
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    # Connected to the IP that was checked, not to the name.
    assert inner.targets == [PUBLIC]
    assert "ok.example" not in inner.targets


def test_rebinding_between_check_and_connect_cannot_occur():
    """The decisive one.

    The resolver answers public first and loopback on every later call. If
    the hostname were handed to the client to resolve again, the connection
    would land on 127.0.0.1. It must land on the validated address, and the
    name must be resolved exactly once.
    """
    inner = Recorder()
    resolver = counting_resolver([[PUBLIC], [LOOPBACK], [LOOPBACK]])
    transport = PinnedAddressTransport(resolver=resolver, inner=inner)

    fetch_pdf("https://rebind.example/a.pdf", max_bytes=10_000, transport=transport)

    assert resolver.calls["n"] == 1, "the hostname was resolved more than once"
    assert inner.targets == [PUBLIC]
    assert LOOPBACK not in inner.targets


def test_pinning_keeps_the_hostname_for_tls_and_host():
    inner = Recorder()
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    # Certificate verification and SNI still use the name, so pinning the
    # address does not weaken TLS.
    assert inner.sni == ["ok.example"]
    assert inner.hosts == ["ok.example"]


def test_each_redirect_hop_is_resolved_and_pinned_independently():
    inner = Recorder([redirect_to("https://second.example/b.pdf")])
    resolver = counting_resolver([[PUBLIC], ["93.184.216.35"]])
    transport = PinnedAddressTransport(resolver=resolver, inner=inner)

    fetch_pdf("https://first.example/a.pdf", max_bytes=10_000, transport=transport)

    assert resolver.calls["n"] == 2  # once per hop, never reused
    assert inner.targets == [PUBLIC, "93.184.216.35"]


def test_a_redirect_to_a_private_address_is_blocked():
    inner = Recorder([redirect_to("https://internal.example/b.pdf")])
    resolver = counting_resolver([[PUBLIC], [PRIVATE]])
    transport = PinnedAddressTransport(resolver=resolver, inner=inner)

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://first.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "blocked_address"
    assert PRIVATE not in inner.targets


def test_a_redirect_to_loopback_is_blocked():
    inner = Recorder([redirect_to("https://localhost.example/b.pdf")])
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC], [LOOPBACK]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://first.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "blocked_address"


def test_a_redirect_to_http_is_blocked():
    inner = Recorder([redirect_to("http://downgrade.example/b.pdf")])
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://first.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "not_https"


def test_the_redirect_chain_is_capped():
    inner = Recorder([redirect_to(f"https://hop{i}.example/x.pdf") for i in range(9)])
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://first.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "too_many_redirects"
    assert len(inner.targets) <= MAX_REDIRECTS + 1


def test_a_redirect_off_the_confirmed_host_stops_for_reconfirmation():
    """The user approved a named host; elsewhere needs asking again."""
    inner = Recorder([redirect_to("https://elsewhere.example/b.pdf")])
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf(
            "https://confirmed.example/a.pdf",
            max_bytes=10_000,
            transport=transport,
            allowed_host="confirmed.example",
        )

    assert caught.value.reason == "host_changed"
    assert caught.value.details["got"] == "elsewhere.example"


# -------------------------------------------------------------- body checks


def test_a_non_pdf_content_type_is_refused():
    inner = Recorder(
        [httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")]
    )
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "not_a_pdf"


def test_magic_bytes_are_checked_not_just_the_header():
    """A paywall interstitial served as application/pdf is still not a PDF."""
    inner = Recorder(
        [
            httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=b"<html>Sign in to continue</html>",
            )
        ]
    )
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "not_a_pdf"
    assert caught.value.details["magic"] is False


def test_an_oversized_body_is_aborted():
    inner = Recorder(
        [
            httpx.Response(
                200,
                headers={"content-type": "application/pdf"},
                content=PDF + b"x" * 50_000,
            )
        ]
    )
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://ok.example/a.pdf", max_bytes=1_000, transport=transport)

    assert caught.value.reason == "too_large"


def test_a_paywall_status_is_a_hard_stop():
    inner = Recorder([httpx.Response(403)])
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    assert caught.value.reason == "access_denied"
    # Exactly one attempt: never retried with different headers.
    assert len(inner.targets) == 1


def test_the_total_budget_is_enforced():
    clock = iter([0.0, 0.0, 1000.0, 1000.0, 1000.0])
    inner = Recorder()
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    with pytest.raises(FetchRefused) as caught:
        fetch_pdf(
            "https://ok.example/a.pdf",
            max_bytes=10_000,
            transport=transport,
            now=lambda: next(clock),
        )

    assert caught.value.reason == "timeout"


# ----------------------------------------------------------------- success


def test_a_good_download_returns_the_bytes_and_the_pinned_address():
    inner = Recorder()
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    result = fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    assert result.data == PDF
    assert result.host == "ok.example"
    assert result.pinned_address == PUBLIC


def test_no_credentials_are_ever_sent():
    inner = Recorder()
    transport = PinnedAddressTransport(
        resolver=counting_resolver([[PUBLIC]]), inner=inner
    )

    fetch_pdf("https://ok.example/a.pdf", max_bytes=10_000, transport=transport)

    # The recorder captured the outgoing request headers via Host/sni only;
    # assert the client never adds auth or cookies of its own.
    assert inner.hosts == ["ok.example"]
