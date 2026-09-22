"""Shared fixtures: a deterministic PDF corpus and an in-process API client.

The corpus is generated rather than checked in so geometry expectations are
computed from known drawing coordinates.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from litmark.api.app import TOKEN_HEADER, create_app
from litmark.services import Services
from litmark.workspace import Workspace

LETTER = (612.0, 792.0)


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="Also run the packaging tests, which build and install the wheel.",
    )


def pytest_configure(config) -> None:
    if config.getoption("--runslow"):
        # Replace the default "-m not slow" selection.
        config.option.markexpr = ""


# ------------------------------------------------------------------ fixtures


def make_pdf(pages: list[list[tuple[float, float, str]]], *, pagesize=LETTER) -> bytes:
    """Draw text at explicit PDF coordinates (origin bottom-left, points)."""
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=pagesize)
    for page in pages:
        pdf.setFont("Helvetica", 11)
        for x, y, text in page:
            pdf.drawString(x, y, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def rotate_and_crop(
    data: bytes,
    *,
    rotate: int = 0,
    crop: tuple[float, float, float, float] | None = None,
) -> bytes:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    writer = PdfWriter()
    for page in reader.pages:
        if rotate:
            page.rotate(rotate)
        if crop:
            page.cropbox.lower_left = (crop[0], crop[1])
            page.cropbox.upper_right = (crop[2], crop[3])
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def paper_one() -> bytes:
    """A two-page text PDF with a multiline passage and a repeated phrase."""
    return make_pdf(
        [
            [
                (72, 720, "Mobility Restrictions and Wage Growth"),
                (72, 690, "Alice Researcher and Bob Coauthor"),
                (72, 640, "We study how job mobility restrictions affect wage"),
                (72, 624, "growth using linked employer-employee records from"),
                (72, 608, "1998 to 2016. Identification relies on a mobility"),
                (72, 592, "restriction that varies across regions."),
                (72, 540, "The effect is economically meaningful."),
            ],
            [
                (72, 720, "Assumption 2 requires that unobserved ability is"),
                (72, 704, "orthogonal to the timing of the reform."),
                # Deliberately repeated on this page, so a quotation without
                # surrounding context is genuinely ambiguous (criterion A7).
                (72, 650, "The effect is economically meaningful."),
                (72, 600, "Table 1 reports the baseline estimates."),
                (72, 560, "The effect is economically meaningful."),
            ],
        ]
    )


@pytest.fixture
def paper_two() -> bytes:
    return make_pdf(
        [
            [
                (72, 720, "Search Frictions in Local Labour Markets"),
                (72, 690, "Carol Author"),
                (72, 640, "We estimate a search model with on-the-job search"),
                (72, 624, "and free mobility between firms, using survey data."),
                (72, 592, "Our identification assumes no mobility restriction."),
            ]
        ]
    )


@pytest.fixture
def rotated_paper(paper_one: bytes) -> bytes:
    """A rotated and asymmetrically cropped fixture for geometry checks."""
    return rotate_and_crop(paper_one, rotate=90, crop=(50, 100, 500, 700))


@pytest.fixture
def blank_paper() -> bytes:
    """A page with no text at all — extraction must flag it, not invent text."""
    return make_pdf([[]])


@pytest.fixture
def project(tmp_path: Path) -> Workspace:
    return Workspace.initialize(tmp_path / "project")


@pytest.fixture
def services(project: Workspace) -> Iterator[Services]:
    instance = Services(project, backend_name="fake")
    yield instance
    instance.db.close()


@pytest.fixture
def client(services: Services) -> Iterator[TestClient]:
    app = create_app(services)
    # A loopback base URL, because the server validates the Host header.
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.headers.update({TOKEN_HEADER: services.session_token})
        yield test_client


# ------------------------------------------------------------------ helpers


def upload(client: TestClient, name: str, data: bytes) -> dict[str, Any]:
    response = client.post(
        "/api/documents", files=[("files", (name, data, "application/pdf"))]
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_extraction(client: TestClient, document_id: str, timeout: float = 30.0) -> dict:
    """Extraction runs in a worker thread; poll until it settles."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get(f"/api/documents/{document_id}").json()
        if payload["extraction"]["status"] in ("ok", "failed"):
            return payload
        time.sleep(0.05)
    raise AssertionError(f"Extraction for {document_id} did not finish in {timeout}s")
