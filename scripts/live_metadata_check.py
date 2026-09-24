#!/usr/bin/env python
"""A manual, opt-in check that the metadata providers actually answer.

This is the only code in the repository that contacts a provider, and it is
deliberately not a test: `pytest` must stay offline, and a check that fires
by accident is a check that hammers somebody's API from CI.

It downloads nothing. It performs at most one metadata request per provider
and prints what came back, with every URL redacted.

    export LITMARK_OPENALEX_API_KEY=...        # optional
    LITMARK_LIVE_CHECK=1 uv run python scripts/live_metadata_check.py \
        ./scratch-project 10.1257/aer.20150572

Use a scratch project, never a real library: the script does not write to
it, but nothing is gained by pointing it at work you care about.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

GATE = "LITMARK_LIVE_CHECK"


def main(argv: list[str]) -> int:
    if os.environ.get(GATE) != "1":
        print(
            f"Refusing to run: set {GATE}=1 to allow live provider calls.\n"
            "This is the only part of litmark that contacts a provider, and it\n"
            "is gated so it cannot run by accident.",
            file=sys.stderr,
        )
        return 2
    if len(argv) < 3:
        print(f"usage: {argv[0]} <project-path> <doi-or-title>", file=sys.stderr)
        return 2

    from litmark.acquisition.redact import safe_url
    from litmark.services import open_services

    project, query = Path(argv[1]), argv[2]
    services = open_services(project)
    try:
        for status in services.acquisition.statuses():
            state = "ready" if status.available else f"skipped — {status.reason}"
            print(f"{status.name}: {state}")
        if not services.acquisition.any_available:
            print("\nNothing to check. See `litmark doctor` for what is missing.")
            return 1

        print(f"\nLooking up {query!r} — this makes real requests.\n")
        resolution = services.resolve_paper(query)

        for warning in resolution.warnings:
            print(f"  warning: {warning}")
        if resolution.failed_providers:
            print(f"  did not answer: {', '.join(resolution.failed_providers)}")

        for index, candidate in enumerate(resolution.candidates, start=1):
            work = candidate.work
            print(f"\n{index}. {work.title or '(untitled)'}")
            print(f"   {work.authors_string() or 'unknown author'} · {work.year or 'n.d.'}")
            print(f"   {work.journal or 'no journal'} · {work.doi or 'no doi'}")
            for version in candidate.versions:
                mark = "download" if version.retrievable else f"no — {version.reason}"
                print(f"   - {version.version_type}: {safe_url(version.url or '')} [{mark}]")

        print(f"\n{len(resolution.candidates)} candidate(s). Nothing was downloaded.")
        return 0
    finally:
        services.db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
