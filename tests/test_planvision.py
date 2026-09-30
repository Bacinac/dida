"""Border/room extraction tests (dida_planvision.pipeline) on synthetic rasters.

The pipeline is deterministic classical CV (OpenCV + NumPy, no weights): a floor-plan
raster (black walls on white) → an editable border skeleton (`extract_borders`) → the
regions ENCLOSED by those borders (`rooms_from_borders`). We drive it on tiny synthetic
rasters where the ground truth is known exactly:

  * a black rectangular wall outline on white  → one enclosed room (a quad)
  * the same outline split by a central wall    → two rooms
  * a hand-authored border frame (no bitmap)    → one room (the Phase-2-only path)

Wall thickness is kept below the directional segment-opening kernel so the four sides
separate into distinct strokes (a thick full outline stays connected and collapses to a
degenerate cross — an input artefact, not a pipeline bug).

Run in an image with numpy+opencv (planvision or api):
    docker run --rm -v /mnt/docker/dida:/w -w /w \
      -e PYTHONPATH=/w/core/src:/w/services/planvision/src \
      --entrypoint sh dida/planvision:latest -c "python -m pytest tests/test_planvision.py"
"""
import cv2
import numpy as np
from dida_planvision.pipeline import extract_borders, rooms_from_borders

W = H = 500      # canvas
MARGIN = 60      # wall outline inset
THICK = 5        # wall thickness (< the segment-open kernel → sides stay separable)


def _frame_raster(divider: bool = False) -> np.ndarray:
    """White canvas with a black rectangular wall outline; optionally a central wall."""
    img = np.full((H, W), 255, np.uint8)
    cv2.rectangle(img, (MARGIN, MARGIN), (W - MARGIN, H - MARGIN), 0, THICK)
    if divider:
        cv2.line(img, (W // 2, MARGIN), (W // 2, H - MARGIN), 0, THICK)
    return img


def _valid_rect(r) -> bool:
    x, y, w, h = r
    return len(r) == 4 and 0 <= x <= 100 and 0 <= y <= 100 and 0 < w <= 100 and 0 < h <= 100


def _in_pct(poly) -> bool:
    return all(len(p) == 2 and 0 <= p[0] <= 100 and 0 <= p[1] <= 100 for p in poly)


def test_extract_borders_finds_the_four_walls():
    borders = extract_borders(_frame_raster())
    # A rectangular outline is four straight runs (two vertical, two horizontal).
    assert len(borders) == 4, f"expected 4 wall borders, got {len(borders)}"
    assert all(_valid_rect(b) for b in borders), f"borders out of the 0..100 %-range: {borders}"


def test_one_enclosed_room_from_extracted_borders():
    borders = extract_borders(_frame_raster())
    rooms = rooms_from_borders(borders, W, H)
    assert len(rooms) == 1, f"a single wall outline encloses exactly one room, got {len(rooms)}"
    poly = rooms[0]
    assert 4 <= len(poly) <= 8, f"a rectangular room simplifies to ~4 vertices, got {len(poly)}"
    assert _in_pct(poly)
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    # The room is the large central interior: near the frame on every side.
    assert min(xs) < 20 and min(ys) < 20, f"room top-left not near the frame: {poly}"
    assert max(xs) > 80 and max(ys) > 80, f"room bottom-right not near the frame: {poly}"


def test_hand_authored_border_frame_gives_one_room():
    # Phase-2-only path: rooms straight from a border set, no bitmap at all.
    frame = [[10, 10, 80, 2], [10, 88, 80, 2], [10, 10, 2, 80], [88, 10, 2, 80]]
    rooms = rooms_from_borders(frame, W, H)
    assert len(rooms) == 1, f"the frame encloses one room, got {len(rooms)}"
    assert _in_pct(rooms[0])


def test_central_divider_splits_into_two_rooms():
    borders = extract_borders(_frame_raster(divider=True))
    rooms = rooms_from_borders(borders, W, H)
    assert len(rooms) == 2, f"a divided outline encloses two rooms, got {len(rooms)}"
    assert all(_in_pct(p) and len(p) >= 4 for p in rooms)


def test_no_borders_means_no_rooms():
    # No enclosing walls → the whole canvas is reachable from the edge → nothing enclosed.
    assert rooms_from_borders([], W, H) == []
