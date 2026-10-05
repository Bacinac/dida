"""planvision HTTP API — stateless. The DIDA api reads a floor's scaffold image
from /state and POSTs the raw bytes here; borders and rooms travel as % coords.

  POST /borders             body = image bytes                        → {"borders": [[x,y,w,h],…]}
  POST /rooms-from-borders  body = {"borders":…, "w":…, "h":…}        → {"rooms": [poly,…]}
  POST /erase-border        body = {"borders":…, "polys":[A,B], w,h}  → {"borders":…, "removed":…}
  GET  /healthz                                                       → {"ok": true}
"""

from __future__ import annotations

from contextlib import contextmanager
from threading import Lock

from fastapi import Body, FastAPI, HTTPException

from dida_planvision.pipeline import (
    _load_gray,
    erase_border_between,
    extract_borders,
    rooms_from_borders,
)

app = FastAPI(title="DIDA planvision")

MAX_CANVAS = 8192
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_pipeline_lock = Lock()


@contextmanager
def _pipeline():
    if not _pipeline_lock.acquire(blocking=False):
        raise HTTPException(503, "Floor plan processing is busy; retry after the current request")
    try:
        yield
    finally:
        _pipeline_lock.release()


def _dims(body: dict) -> tuple[int, int]:
    """Validated pixel canvas (w, h): both are REQUIRED and must be 1..MAX_CANVAS, else
    400. These endpoints carry no image to size from, so a missing/zero w or h has no
    sane default — coercing it (was `or 1000`) silently returns rooms for a canvas the
    caller never asked for. Fail loud at the boundary; also caps a huge w*h allocation."""
    if body.get("w") is None or body.get("h") is None:
        raise HTTPException(400, "w and h are required")
    if type(body["w"]) is not int or type(body["h"]) is not int:
        raise HTTPException(400, "w/h must be integers")
    w, h = body["w"], body["h"]
    if not (1 <= w <= MAX_CANVAS and 1 <= h <= MAX_CANVAS):
        raise HTTPException(400, f"w/h out of range (1..{MAX_CANVAS})")
    return w, h


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# The border/room handlers are CPU-bound (OpenCV). Declaring them plain `def` runs
# them in FastAPI's threadpool, so a long pipeline can't block the event loop (and
# starve /healthz). FastAPI populates the raw/JSON body params before calling.
@app.post("/borders")
def borders(data: bytes = Body(b"", media_type="application/octet-stream")) -> dict:
    """Phase 1: the border skeleton as [x, y, w, h] rectangles in % — the editable draft."""
    if not data:
        raise HTTPException(400, "empty body")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "image exceeds 20 MB")
    try:
        with _pipeline():
            gray, _, _ = _load_gray(data)
            return {"borders": extract_borders(gray)}
    except ValueError as e:
        raise HTTPException(415, str(e)) from e


@app.post("/rooms-from-borders")
def rooms_from_borders_ep(body: dict = Body(...)) -> dict:
    """Phase 2: rooms are the regions enclosed by the borders. Body = {"borders":
    [[x,y,w,h],…] in %, "w": px, "h": px}. No image — the borders are the whole truth."""
    w, h = _dims(body)
    with _pipeline():
        return {"rooms": rooms_from_borders(body.get("borders") or [], w, h)}


@app.post("/erase-border")
def erase_border_ep(body: dict = Body(...)) -> dict:
    """Merge two adjacent rooms by ERASING the border between them (borders = source of
    truth). Body = {"borders": [[x,y,w,h],…] %, "polys": [A, B] %, "w", "h"} → the
    thinned set + the erased pieces (removed == [] means the rooms are not adjacent)."""
    w, h = _dims(body)
    polys = body.get("polys") or []
    if len(polys) != 2:
        raise HTTPException(400, "merge needs exactly two areas")
    with _pipeline():
        kept, removed = erase_border_between(body.get("borders") or [], polys[0], polys[1], w, h)
    return {"borders": kept, "removed": removed}
