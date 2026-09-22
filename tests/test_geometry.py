"""Geometry fixtures: rotation and crop-box behaviour must stay aligned.

Acceptance criterion A6 requires a rotated/cropped fixture whose coordinates
remain correct. These tests pin the conversion so a future change to the
extraction layer cannot silently move every highlight.
"""

from __future__ import annotations

import pathlib

import pytest
from conftest import make_pdf, rotate_and_crop

from litmark.extraction import extract_pdf
from litmark.references import locate_quote

# Asymmetric crop in PDF user space (bottom-left origin) on a 612x792 page.
CROP = (60.0, 150.0, 520.0, 690.0)
MARKER = "MARKER"


def _marker_pdf() -> bytes:
    """One marker word in the upper-left quadrant of an otherwise empty page."""
    return make_pdf([[(100, 650, MARKER)]])


def _write(tmp_path: pathlib.Path, data: bytes, name: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _marker_rect(path: pathlib.Path) -> list[float]:
    page = extract_pdf(path).pages[0]
    match = locate_quote(page, MARKER)
    assert match.ok, match.message
    assert len(match.rects) == 1, match.rects
    return match.rects[0]


def _inverse_rotate(rect: list[float], rotation: int) -> list[float]:
    """Undo a display rotation on a normalized rectangle."""
    x0, y0, x1, y1 = rect
    if rotation == 90:
        out = (y0, 1 - x1, y1, 1 - x0)
    elif rotation == 180:
        out = (1 - x1, 1 - y1, 1 - x0, 1 - y0)
    elif rotation == 270:
        out = (1 - y1, x0, 1 - y0, x1)
    else:
        out = (x0, y0, x1, y1)
    return [min(out[0], out[2]), min(out[1], out[3]), max(out[0], out[2]), max(out[1], out[3])]


def test_marker_position_matches_drawn_coordinates(tmp_path):
    """The plain page's rectangle must match the reportlab draw coordinates."""
    rect = _marker_rect(_write(tmp_path, _marker_pdf(), "plain.pdf"))
    # Drawn at x=100 on a 612pt-wide page; 12pt Helvetica baseline at y=650.
    assert rect[0] == pytest.approx(100 / 612, abs=0.005)
    # Top of the glyph sits just above the baseline: 792 - (650 + capheight).
    assert rect[1] == pytest.approx((792 - 650 - 8.6) / 792, abs=0.01)
    assert rect[3] == pytest.approx((792 - 650 + 3.0) / 792, abs=0.01)


def test_cropping_rescales_coordinates(tmp_path):
    """After cropping, the same text sits at a different relative position."""
    plain = _marker_rect(_write(tmp_path, _marker_pdf(), "plain.pdf"))
    cropped = _marker_rect(
        _write(tmp_path, rotate_and_crop(_marker_pdf(), crop=CROP), "cropped.pdf")
    )

    crop_width = CROP[2] - CROP[0]
    crop_height = CROP[3] - CROP[1]
    # Expected: re-express the plain page's absolute position inside the crop box.
    expected_x0 = (plain[0] * 612 - CROP[0]) / crop_width
    expected_y0 = (plain[1] * 792 - (792 - CROP[3])) / crop_height
    assert cropped[0] == pytest.approx(expected_x0, abs=0.004)
    assert cropped[1] == pytest.approx(expected_y0, abs=0.004)
    assert cropped != plain


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotation_is_consistent_with_the_crop_box(tmp_path, rotation):
    """Undoing the rotation must recover the unrotated cropped rectangle.

    pdfplumber rotates character coordinates but reports ``page.cropbox`` in a
    different convention at 180 and 270 degrees; this test is what catches a
    regression back to trusting it.
    """
    reference = _marker_rect(
        _write(tmp_path, rotate_and_crop(_marker_pdf(), crop=CROP), "base.pdf")
    )
    rotated = _marker_rect(
        _write(
            tmp_path,
            rotate_and_crop(_marker_pdf(), rotate=rotation, crop=CROP),
            f"rot{rotation}.pdf",
        )
    )
    recovered = _inverse_rotate(rotated, rotation)
    assert recovered == pytest.approx(reference, abs=0.01), (
        f"rotation {rotation}: {recovered} != {reference}"
    )


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rectangles_stay_inside_the_unit_square(tmp_path, rotation):
    rect = _marker_rect(
        _write(
            tmp_path,
            rotate_and_crop(_marker_pdf(), rotate=rotation, crop=CROP),
            f"unit{rotation}.pdf",
        )
    )
    assert all(0.0 <= value <= 1.0 for value in rect), rect
    assert rect[2] > rect[0] and rect[3] > rect[1], rect


def test_multiline_quote_produces_one_rectangle_per_line(tmp_path):
    data = make_pdf(
        [
            [
                (72, 700, "Identification relies on a mobility"),
                (72, 684, "restriction that varies across regions."),
            ]
        ]
    )
    page = extract_pdf(_write(tmp_path, data, "multiline.pdf")).pages[0]
    match = locate_quote(page, "a mobility restriction that varies")
    assert match.ok, match.message
    assert len(match.rects) == 2, match.rects
    first, second = match.rects
    assert first[3] <= second[1] + 0.01, "line rectangles should not overlap vertically"
