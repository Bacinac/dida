from io import BytesIO

import cv2
import numpy as np
import pytest
from dida_planvision import app
from dida_planvision.pipeline import (
    MAX_IMAGE_PIXELS,
    _load_gray,
    erase_border_between,
    rooms_from_borders,
)
from fastapi import HTTPException
from PIL import Image

FRAME = [[10, 10, 80, 1], [10, 89, 80, 1], [10, 10, 1, 80], [89, 10, 1, 80]]


def test_large_canvas_retains_percent_geometry_at_bounded_resolution():
    assert rooms_from_borders(FRAME, 8192, 8192) == rooms_from_borders(FRAME, 1024, 1024)
    borders = [*FRAME, [50, 10, 1, 80]]
    left = [[10, 10], [50, 10], [50, 90], [10, 90]]
    right = [[50, 10], [90, 10], [90, 90], [50, 90]]
    assert erase_border_between(borders, left, right, 8192, 8192) == erase_border_between(borders, left, right, 1024, 1024)


def test_oversized_decode_is_rejected_before_opencv(monkeypatch):
    stream = BytesIO()
    Image.new("1", (8192, 8192)).save(stream, format="PNG")
    monkeypatch.setattr(cv2, "imdecode", lambda *_: pytest.fail("oversized image reached the decoder"))
    with pytest.raises(ValueError, match=str(MAX_IMAGE_PIXELS)):
        _load_gray(stream.getvalue())


def test_16bit_rgba_keeps_alpha_and_bounded_gray():
    raw = np.zeros((1024, 2048, 4), dtype=np.uint16)
    raw[400:600, 900:1100, 3] = 65535
    ok, encoded = cv2.imencode(".png", raw)
    assert ok
    gray, w, h = _load_gray(encoded.tobytes())
    assert (w, h) == (1024, 512)
    assert gray.dtype == np.uint8
    assert gray[0, 0] == 255
    assert gray[250, 500] == 0


@pytest.mark.parametrize("w", [True, 3.2, "1024"])
def test_dimensions_do_not_coerce_non_integers(w):
    with pytest.raises(HTTPException) as error:
        app._dims({"w": w, "h": 1024})
    assert error.value.status_code == 400


def test_concurrent_processing_fails_visibly_and_releases_after_failure():
    with app._pipeline(), pytest.raises(HTTPException) as error:
        app.rooms_from_borders_ep({"w": 8192, "h": 8192, "borders": FRAME})
    assert error.value.status_code == 503
    assert len(app.rooms_from_borders_ep({"w": 8192, "h": 8192, "borders": FRAME})["rooms"]) == 1
