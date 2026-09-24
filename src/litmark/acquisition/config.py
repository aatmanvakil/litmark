"""Which providers are enabled, and what they need to run.

Two things are read from two different places, deliberately:

* Enable flags and the contact address come from ``project.toml``. They are
  not secret, they describe the project, and they should travel with it.
* The OpenAlex API key comes only from the environment. ``project.toml``
  lives in the project directory — which for a real library means a synced
  folder, someone's backups, and possibly a git repository — so a key placed
  there is a key published. It is never written, never logged, and never put
  in a URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

#: The only place the OpenAlex key is read from.
OPENALEX_KEY_ENV = "LITMARK_OPENALEX_API_KEY"

CROSSREF = "crossref"
OPENALEX = "openalex"
UNPAYWALL = "unpaywall"
ARXIV = "arxiv"
PROVIDERS = (CROSSREF, OPENALEX, UNPAYWALL, ARXIV)

#: Providers that cannot run without a contact address.
NEEDS_MAILTO = frozenset({UNPAYWALL})


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    enabled: bool
    available: bool
    reason: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "available": self.available,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AcquisitionConfig:
    mailto: str | None = None
    enabled: frozenset[str] = frozenset()
    #: Present only in memory. Never serialised, never logged.
    openalex_key: str | None = None

    @classmethod
    def from_settings(
        cls, settings: dict[str, Any], environ: dict[str, str] | None = None
    ) -> AcquisitionConfig:
        section = settings.get("acquisition") or {}
        env = os.environ if environ is None else environ
        mailto = str(section.get("mailto") or "").strip() or None
        enabled = {name for name in PROVIDERS if bool(section.get(name, False))}
        key = (env.get(OPENALEX_KEY_ENV) or "").strip() or None
        return cls(mailto=mailto, enabled=frozenset(enabled), openalex_key=key)

    def status(self, name: str) -> ProviderStatus:
        enabled = name in self.enabled
        if not enabled:
            return ProviderStatus(name, False, False, "not enabled in project.toml")
        if name == OPENALEX and not self.openalex_key:
            # Not an error: OpenAlex sits out and the rest carry on.
            return ProviderStatus(
                name,
                True,
                False,
                f"no API key; set {OPENALEX_KEY_ENV} in the environment",
            )
        if name in NEEDS_MAILTO and not self.mailto:
            return ProviderStatus(
                name, True, False, "no contact address; set mailto in [acquisition]"
            )
        return ProviderStatus(name, True, True, None)

    def statuses(self) -> list[ProviderStatus]:
        return [self.status(name) for name in PROVIDERS]

    def available(self, name: str) -> bool:
        return self.status(name).available

    @property
    def any_available(self) -> bool:
        return any(status.available for status in self.statuses())

    def to_json(self) -> dict[str, Any]:
        """Safe to print. Reports whether a key is present, never its value."""
        return {
            # The address is configuration, not a secret, but it is personal
            # data, so diagnostics say only whether one is set.
            "mailto_configured": self.mailto is not None,
            "openalex_key_configured": self.openalex_key is not None,
            "providers": [status.to_json() for status in self.statuses()],
        }
