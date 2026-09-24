"""Where each piece of acquisition configuration is allowed to come from.

The split is the point: flags and the contact address describe the project
and live in project.toml; the OpenAlex key does not, because project.toml
sits in a synced folder, in backups, and often in git.
"""

from __future__ import annotations

import json

from litmark.acquisition.config import (
    OPENALEX_KEY_ENV,
    PROVIDERS,
    AcquisitionConfig,
)

KEY = "sk-live-NEVER-REAL-9876543210"
ALL_ON = {
    "acquisition": {
        "mailto": "someone@example.edu",
        "crossref": True,
        "openalex": True,
        "unpaywall": True,
        "arxiv": True,
    }
}


def config(settings=None, **env):
    return AcquisitionConfig.from_settings(settings or ALL_ON, environ=env)


# ------------------------------------------------------------------ flags


def test_nothing_is_enabled_by_default():
    empty = AcquisitionConfig.from_settings({}, environ={})

    assert empty.enabled == frozenset()
    assert empty.any_available is False


def test_flags_come_from_project_toml():
    partial = AcquisitionConfig.from_settings(
        {"acquisition": {"crossref": True, "arxiv": False, "mailto": "a@b.edu"}},
        environ={},
    )

    assert partial.available("crossref")
    assert not partial.available("arxiv")


def test_every_provider_has_a_status():
    assert {status.name for status in config().statuses()} == set(PROVIDERS)


# -------------------------------------------------------------------- key


def test_the_key_is_read_only_from_the_environment():
    with_env = config(**{OPENALEX_KEY_ENV: KEY})

    assert with_env.openalex_key == KEY
    assert with_env.available("openalex")


def test_a_key_in_project_toml_is_ignored():
    """project.toml is synced and backed up; a key there is a key published."""
    smuggled = AcquisitionConfig.from_settings(
        {
            "acquisition": {
                "openalex": True,
                "mailto": "a@b.edu",
                "api_key": KEY,
                "openalex_api_key": KEY,
            }
        },
        environ={},
    )

    assert smuggled.openalex_key is None
    assert not smuggled.available("openalex")


def test_openalex_without_a_key_is_unavailable_not_an_error():
    without = config(**{})

    status = without.status("openalex")

    assert status.enabled is True
    assert status.available is False
    assert OPENALEX_KEY_ENV in status.reason


def test_the_others_still_work_with_openalex_unavailable():
    """Litmark must be usable with OpenAlex off."""
    without = config(**{})

    assert without.available("crossref")
    assert without.available("unpaywall")
    assert without.available("arxiv")
    assert without.any_available is True


def test_a_blank_key_counts_as_absent():
    assert config(**{OPENALEX_KEY_ENV: "   "}).openalex_key is None


# --------------------------------------------------------------- contact


def test_unpaywall_needs_a_contact_address():
    """Unpaywall rejects requests without one, so it is reported up front."""
    no_mail = AcquisitionConfig.from_settings(
        {"acquisition": {"unpaywall": True, "crossref": True}}, environ={}
    )

    assert not no_mail.available("unpaywall")
    assert "mailto" in no_mail.status("unpaywall").reason
    # Crossref only loses the polite pool, so it still runs.
    assert no_mail.available("crossref")


# ------------------------------------------------------------- diagnostics


def test_diagnostics_report_presence_never_the_value():
    rendered = json.dumps(config(**{OPENALEX_KEY_ENV: KEY}).to_json())

    assert KEY not in rendered
    assert '"openalex_key_configured": true' in rendered


def test_diagnostics_do_not_echo_the_contact_address():
    """Not a secret, but personal data; presence is all a diagnostic needs."""
    rendered = json.dumps(config(**{OPENALEX_KEY_ENV: KEY}).to_json())

    assert "someone@example.edu" not in rendered
    assert '"mailto_configured": true' in rendered


def test_the_key_is_not_in_the_repr_either():
    """A dataclass repr reaches logs and tracebacks by accident."""
    rendered = repr(config(**{OPENALEX_KEY_ENV: KEY}).to_json())

    assert KEY not in rendered


# ------------------------------------------------------------------- http


def test_doctor_reports_providers_without_the_key(client, monkeypatch):
    payload = client.get("/api/doctor").json()

    assert "acquisition" in payload
    assert {p["name"] for p in payload["acquisition"]["providers"]} == set(PROVIDERS)
    assert KEY not in json.dumps(payload)


def test_a_project_with_no_acquisition_section_is_fine(client):
    """The default project.toml has no such section; nothing should break."""
    payload = client.get("/api/doctor").json()

    assert payload["acquisition"]["providers"]
    assert all(not p["available"] for p in payload["acquisition"]["providers"])


# ----------------------------------------- the live check stays opt-in


def test_the_live_script_refuses_without_its_gate():
    """It is the only code that contacts a provider, so it must not fire."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).parent.parent / "scripts" / "live_metadata_check.py"
    environment = {"PATH": "/usr/bin:/bin"}

    result = subprocess.run(
        [sys.executable, str(script), "/tmp/nowhere", "10.1/x"],
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 2
    assert "LITMARK_LIVE_CHECK" in result.stderr
    # It refused before importing litmark, so nothing was opened or read.
    assert "Traceback" not in result.stderr


def test_the_live_script_is_not_collected_as_a_test():
    """pytest must stay offline; a collected script would not be."""
    from pathlib import Path

    script = Path(__file__).parent.parent / "scripts" / "live_metadata_check.py"

    assert script.exists()
    assert not script.name.startswith("test_")
    assert script.parent.name == "scripts"
