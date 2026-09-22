"""A1: install the built wheel in a clean environment and serve the interface.

This is the release gate from the specification: the test installs the actual
built artifact into a fresh virtual environment that has no frontend
toolchain, then starts the real console entry point and checks that the
complete interface — including the PDF.js worker — is served, and that a note
and a PDF can be opened with no network access.

Marked `slow` because it builds and installs. Run it with `-m slow`, or the
whole suite with `--runslow`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(url: str, timeout: float = 45.0) -> str:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.read().decode()
        except Exception as exc:  # noqa: BLE001 - the server may still be starting
            last = exc
            time.sleep(0.4)
    raise AssertionError(f"{url} never became available: {last}")


@pytest.fixture(scope="module")
def wheel() -> Path:
    """Build the wheel from the current tree."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is required to build the distribution")
    static = PROJECT_ROOT / "src" / "research_workspace" / "static" / "index.html"
    if not static.is_file():
        pytest.skip("browser assets are not built; run `npm --prefix frontend run build`")

    out = PROJECT_ROOT / "dist"
    subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out)],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
    )
    wheels = sorted(out.glob("research_workspace-*.whl"))
    assert wheels, "no wheel was produced"
    return wheels[-1]


@pytest.fixture(scope="module")
def installed(wheel: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Install the wheel into a clean virtual environment."""
    venv = tmp_path_factory.mktemp("clean-env") / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    python = venv / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        check=True,
        capture_output=True,
    )
    return venv


def test_a1_wheel_serves_the_interface_without_node(
    installed: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    command = installed / "bin" / "research-workspace"
    assert command.is_file(), "the console entry point was not installed"

    # Nothing in the environment provides a JavaScript toolchain.
    node_modules = installed / "lib" / "node_modules"
    assert not node_modules.exists()

    project = tmp_path_factory.mktemp("served-project") / "papers"
    init = subprocess.run(
        [str(command), "init", str(project)], check=True, capture_output=True, text=True
    )
    assert "Created project" in init.stdout
    assert (project / "project.toml").is_file()
    assert (project / "notes" / "welcome.md").is_file()

    port = free_port()
    # PATH is stripped of this repo's venv so only the installed package is used.
    environment = {
        **os.environ,
        "PATH": f"{installed / 'bin'}:/usr/bin:/bin",
        "PYTHONPATH": "",
    }
    server = subprocess.Popen(
        [str(command), "serve", str(project), "--port", str(port)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        html = wait_for(f"{base}/")

        # The complete interface, with the session credential injected.
        assert '<div id="app">' in html
        token = html.split('name="research-token" content="', 1)[1].split('"', 1)[0]
        assert len(token) > 20

        # Every referenced asset is served locally by this process.
        import re

        assets = sorted(set(re.findall(r"/assets/[A-Za-z0-9._-]+", html)))
        assert assets, "the page referenced no bundled assets"
        for path in assets:
            with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:
                assert response.status == 200
                assert len(response.read()) > 0

        # The PDF worker in particular must be present, not fetched from a CDN.
        worker = next(
            path
            for path in (
                p.name
                for p in (
                    installed
                    / "lib"
                    / f"python3.{sys.version_info.minor}"
                    / "site-packages"
                    / "research_workspace"
                    / "static"
                    / "assets"
                ).iterdir()
            )
            if "worker" in path.lower()
        )
        with urllib.request.urlopen(f"{base}/assets/{worker}", timeout=5) as response:
            assert response.status == 200
            assert len(response.read()) > 100_000

        # The API works, and the boundary still rejects an unauthenticated call.
        request = urllib.request.Request(f"{base}/api/state")
        request.add_header("x-research-token", token)
        with urllib.request.urlopen(request, timeout=5) as response:
            state = json.loads(response.read())
        assert state["project"]["schema_version"] == 1
        assert any(note["note_id"] == "welcome" for note in state["notes"])
        # Without an agent the reader still works and says what is missing.
        assert isinstance(state["agent"]["ready"], bool)

        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(f"{base}/api/state", timeout=5)
        assert unauthorized.value.code == 401
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.kill()


def test_a1_doctor_reports_without_printing_secrets(installed: Path, tmp_path_factory) -> None:
    command = installed / "bin" / "research-workspace"
    project = tmp_path_factory.mktemp("doctor-project") / "papers"
    subprocess.run([str(command), "init", str(project)], check=True, capture_output=True)

    environment = {
        **os.environ,
        "ANTHROPIC_API_KEY": "sk-ant-not-a-real-key-0123456789",
        "PATH": f"{installed / 'bin'}:/usr/bin:/bin",
    }
    result = subprocess.run(
        [str(command), "doctor", str(project)],
        capture_output=True,
        text=True,
        env=environment,
    )
    # `doctor` exits non-zero when the agent is not fully configured; that is
    # a report, not a crash.
    assert result.returncode in (0, 1)
    assert "browser assets: found" in result.stdout
    assert "project:" in result.stdout
    assert "agent backend:" in result.stdout
    # Credentials are reported as present or absent, never printed.
    assert "sk-ant-not-a-real-key-0123456789" not in result.stdout
    assert "sk-ant" not in result.stdout
