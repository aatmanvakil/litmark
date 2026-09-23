"""The one place this application fetches something from the internet.

Everything a download must satisfy is enforced here rather than spread across
providers, so there is a single thing to audit.

The subtle part is the address. Resolving a hostname, checking that it is
public, and then handing the *hostname* to an HTTP client is not enough: the
client resolves it again, and a DNS record with a short TTL can answer the
check with a public address and the connection with a private one. The check
and the connection must therefore use the same address, so the name is
resolved exactly once, **every** returned address is validated, and the
connection is pinned to the one that was checked — while the hostname is kept
for the Host header and for TLS, so pinning weakens neither SNI nor
certificate verification.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
TOTAL_BUDGET = 60.0
MAX_REDIRECTS = 3
CHUNK = 64 * 1024

PDF_MAGIC = b"%PDF-"
PDF_TYPES = ("application/pdf", "application/x-pdf")

USER_AGENT = "litmark/0.1.0 (+https://github.com/tlamadon/litmark)"


class FetchRefused(Exception):
    """The request was not made, or was stopped, for a stated reason."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


Resolver = Callable[[str], list[str]]


def system_resolver(hostname: str) -> list[str]:
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return list(dict.fromkeys(info[4][0] for info in infos))


def is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def pin_address(hostname: str, *, resolver: Resolver = system_resolver) -> str:
    """Resolve once, validate every answer, return the address to connect to.

    Every address is checked, not just the one about to be used: a
    round-robin record containing one private address would otherwise slip
    past a first-address-only check on a later attempt.
    """
    try:
        addresses = resolver(hostname)
    except OSError as exc:
        raise FetchRefused("dns_failed", hostname=hostname, error=str(exc)) from exc
    if not addresses:
        raise FetchRefused("dns_empty", hostname=hostname)
    bad = [address for address in addresses if not is_public(address)]
    if bad:
        raise FetchRefused("blocked_address", hostname=hostname, addresses=bad)
    return addresses[0]


def check_url(url: str) -> tuple[str, str]:
    """Validate a URL's shape. Returns ``(hostname, url)``."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise FetchRefused("not_https", url=url)
    if parsed.username or parsed.password:
        raise FetchRefused("userinfo_in_url", url=url)
    if parsed.port not in (None, 443):
        raise FetchRefused("unexpected_port", url=url, port=parsed.port)
    if not parsed.hostname:
        raise FetchRefused("no_hostname", url=url)
    return parsed.hostname, url


class PinnedAddressTransport(httpx.BaseTransport):
    """Connect to a validated address, keeping the hostname for TLS.

    The inner transport is injectable so tests can assert the address that was
    actually connected to, rather than merely that a check ran.
    """

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        inner: httpx.BaseTransport | None = None,
    ) -> None:
        self._resolve = resolver
        self._inner = inner or httpx.HTTPTransport(retries=0)
        self.pinned: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        hostname, _ = check_url(str(request.url))
        address = pin_address(hostname, resolver=self._resolve)
        self.pinned.append(address)

        literal = f"[{address}]" if ":" in address else address
        pinned_request = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=literal),
            headers=request.headers,
            stream=request.stream,
            # SNI and certificate hostname verification keep using the name,
            # so pinning the address does not weaken TLS.
            extensions={**request.extensions, "sni_hostname": hostname},
        )
        pinned_request.headers["Host"] = hostname
        response = self._inner.handle_request(pinned_request)
        response.extensions = {**response.extensions, "pinned_address": address}
        return response

    def close(self) -> None:
        self._inner.close()


@dataclass
class Fetched:
    data: bytes
    url: str
    host: str
    content_type: str
    pinned_address: str | None


def fetch_pdf(
    url: str,
    *,
    max_bytes: int,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = system_resolver,
    allowed_host: str | None = None,
    now: Callable[[], float] = time.monotonic,
) -> Fetched:
    """Download one PDF, or refuse with a stated reason.

    Redirects are followed manually so each hop is re-validated: a hop is a
    fresh hostname, and reusing an earlier hop's decision is the hole this
    exists to close.
    """
    deadline = now() + TOTAL_BUDGET
    pinned = transport or PinnedAddressTransport(resolver=resolver)
    owned = transport is None
    client = httpx.Client(
        transport=pinned,
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/pdf"},
    )
    current = url
    try:
        for hop in range(MAX_REDIRECTS + 1):
            if now() > deadline:
                raise FetchRefused("timeout", stage="before_request", url=current)
            hostname, current = check_url(current)
            if allowed_host is not None and hostname != allowed_host:
                # The user confirmed a named host; a hop elsewhere needs a new
                # confirmation rather than being followed silently.
                raise FetchRefused("host_changed", expected=allowed_host, got=hostname)

            with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location or hop == MAX_REDIRECTS:
                        raise FetchRefused("too_many_redirects", url=current)
                    # allowed_host deliberately survives the hop: the check
                    # exists to catch a redirect leaving the host the user
                    # approved, so clearing it here would disable it.
                    current = str(httpx.URL(current).join(location))
                    continue
                if response.status_code in (401, 403):
                    raise FetchRefused("access_denied", status=response.status_code)
                if response.status_code >= 400:
                    raise FetchRefused("http_error", status=response.status_code)

                content_type = (response.headers.get("content-type") or "").split(";")[0]
                if content_type.strip().lower() not in PDF_TYPES:
                    raise FetchRefused("not_a_pdf", content_type=content_type)

                body = bytearray()
                for chunk in response.iter_bytes(CHUNK):
                    if now() > deadline:
                        raise FetchRefused("timeout", stage="streaming", url=current)
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        # Aborted mid-stream rather than buffered and rejected.
                        raise FetchRefused("too_large", limit=max_bytes)
                data = bytes(body)

            # Content-Type is a claim; the magic bytes are the evidence.
            if not data.lstrip()[:5].startswith(PDF_MAGIC):
                raise FetchRefused("not_a_pdf", content_type=content_type, magic=False)
            return Fetched(
                data=data,
                url=current,
                host=hostname,
                content_type=content_type,
                pinned_address=getattr(pinned, "pinned", [None])[-1]
                if getattr(pinned, "pinned", None)
                else None,
            )
        raise FetchRefused("too_many_redirects", url=current)
    except httpx.TimeoutException as exc:
        raise FetchRefused("timeout", error=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise FetchRefused("transport_error", error=str(exc)) from exc
    finally:
        client.close()
        if owned:
            pinned.close()
