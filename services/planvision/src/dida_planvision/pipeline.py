"""Raster floor plan → editable border skeleton + enclosed room polygons, on CPU,
deterministically. No model weights, no torch — OpenCV + NumPy (Apache/BSD).

The plan is drawn as thick BLACK walls on a (usually transparent → white) background.
Two clean stages, and the bitmap is used ONLY in the first:

  1. BORDERS  black pixels (BORDER_DARK) → thickness-open drops tile grout / text /
              fixtures → length filter drops floating blobs → ONE clean rectangle per
              straight run (`_border_rects`). The plan's drawn walls seed the border
              layer; the user then edits it freely — a patio or garden edge is a border
              too, not every border is a wall.
  2. ROOMS    a room is simply a region ENCLOSED BY THE BORDERS — nothing else.
              Rasterize the border rects and flood-fill; every enclosed region is a
              room, tiled out to the border centreline so rooms share edges (`_rooms`).
              No bitmap here at all: the (user-shaped) borders are the whole truth.

extract_borders → the editable draft: [x, y, w, h] rectangles in % (drag/add/delete in
the UI). rooms_from_borders → rooms from the CONFIRMED borders. erase_border_between →
merge two rooms by deleting exactly the border stretch that separates them. Polygons
are [[x, y], …] in % (0..100), the FloorPoly the UI stores in areas.fp_poly.
"""

from __future__ import annotations

import cv2
import numpy as np

# ── tunables (fractions of the plan's larger side / area unless noted) ───────
MAX_DIM = 1024          # work resolution cap
BORDER_DARK = 50        # a wall pixel is at least this dark (walls are black; floors lighter)
BORDER_THICK = 0.006    # thickness open (frac of big): drops tile grout / text / fixtures
BORDER_KEEP_LEN = 0.04  # a wall stroke spans ≥ this — shorter = floating blob, dropped
SEG_MINLEN = 0.018      # min length of a directional (v/h) stroke segment
BORDER_UNIFORM = 0.008  # ONE uniform thickness for every border → junctions meet flush
BORDER_TOL = 0.02       # two runs are the SAME line within this
BORDER_MERGE = 0.30     # merge colinear pieces into ONE whole border across gaps up to this
                        # (closes doors/windows/wide openings; only a room-width gap stays)
MIN_ROOM = 0.004        # drop enclosed regions smaller than this fraction of the plan
POLY_EPS = 0.012        # Douglas-Peucker simplification (frac of big) — plans are rectilinear


def _load_gray(data: bytes) -> tuple[np.ndarray, int, int]:
    """Decode to grayscale at bounded resolution. Returns (gray, W, H).

    Floor-plan PNGs routinely carry an ALPHA channel — the area OUTSIDE the building is
    TRANSPARENT, not white. Decoding straight to gray silently drops alpha and renders
    every transparent pixel BLACK, which then merges with the (also black) walls and
    wrecks all downstream thresholding (the exterior reads as ~35-50 % "wall"). So
    composite over WHITE first: transparent → white background, walls stay black, and a
    plain black threshold isolates the walls exactly."""
    raw = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError("undecodable image")
    # 16-bit PNGs (uint16) would make the black-threshold read garbage (BORDER_DARK is
    # an 8-bit level). Down-shift to 8-bit BEFORE the alpha/gray branch so every path
    # below works on uint8 as intended.
    if raw.dtype == np.uint16:
        raw = (raw >> 8).astype(np.uint8)
    # Bound resolution BEFORE the alpha composite: that step allocates several
    # full-res float32 temporaries, which on a large RGBA plan (the normal case)
    # would exceed the 512m container limit and OOM-kill the service. Downscale the
    # raw uint8 raster first, so every float32 allocation below is O(MAX_DIM^2).
    h0, w0 = raw.shape[:2]
    scale = min(1.0, MAX_DIM / max(w0, h0))
    if scale < 1.0:
        raw = cv2.resize(raw, (max(1, round(w0 * scale)), max(1, round(h0 * scale))),
                         interpolation=cv2.INTER_AREA)
    if raw.ndim == 3 and raw.shape[2] == 4:
        a = raw[:, :, 3:4].astype(np.float32) / 255.0
        comp = (raw[:, :, :3].astype(np.float32) * a + 255.0 * (1.0 - a)).astype(np.uint8)
        img = cv2.cvtColor(comp, cv2.COLOR_BGR2GRAY)
    elif raw.ndim == 3:
        img = cv2.cvtColor(raw[:, :, :3], cv2.COLOR_BGR2GRAY)
    else:
        img = raw
    h, w = img.shape[:2]
    return img, w, h


def _stroke_mask(gray: np.ndarray) -> np.ndarray:
    """The plan's drawn walls, and nothing else (255 = wall stroke). Black threshold →
    thickness open (drops thin tile grout / text / furniture) → length filter (blobs)."""
    h, w = gray.shape
    big = max(w, h)
    black = (gray < BORDER_DARK).astype(np.uint8) * 255
    kt = max(3, int(big * BORDER_THICK))
    thick = cv2.morphologyEx(black, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kt, kt)))
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(thick, 8)
    keep = np.zeros_like(thick)
    for i in range(1, n):
        if max(stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]) >= BORDER_KEEP_LEN * big:
            keep[lbl == i] = 255
    return keep


def _segments(keep: np.ndarray, big: int) -> dict[str, list[tuple[float, float, float, float]]]:
    """Straight strokes as (line_pos, start, end, thickness): vertical strokes
    (line_pos = x) and horizontal strokes (line_pos = y), via a directional opening."""
    minlen = max(5, int(big * SEG_MINLEN))
    out: dict[str, list[tuple[float, float, float, float]]] = {"v": [], "h": []}
    v = cv2.morphologyEx(keep, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, minlen)))
    n, _, st, _ = cv2.connectedComponentsWithStats(v, 8)
    for i in range(1, n):
        x, y, ww, hh = (st[i, cv2.CC_STAT_LEFT], st[i, cv2.CC_STAT_TOP],
                        st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT])
        out["v"].append((x + ww / 2, y, y + hh, ww))
    hm = cv2.morphologyEx(keep, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (minlen, 1)))
    n, _, st, _ = cv2.connectedComponentsWithStats(hm, 8)
    for i in range(1, n):
        x, y, ww, hh = (st[i, cv2.CC_STAT_LEFT], st[i, cv2.CC_STAT_TOP],
                        st[i, cv2.CC_STAT_WIDTH], st[i, cv2.CC_STAT_HEIGHT])
        out["h"].append((y + hh / 2, x, x + ww, hh))
    return out


def _border_rects(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """ONE clean rectangle per straight wall run: the pieces on the same line merge
    (and the door/window gaps between them close) into a WHOLE border of ONE uniform
    thickness, so junctions are flush and the union reads as a single smooth solid."""
    h, w = gray.shape
    big = max(w, h)
    keep = _stroke_mask(gray)
    segs = _segments(keep, big)
    tk = max(3, int(big * BORDER_UNIFORM))
    tol = max(3, int(big * BORDER_TOL))
    maxgap = int(big * BORDER_MERGE)
    rects: list[tuple[int, int, int, int]] = []
    for orient in ("v", "h"):
        items = sorted(segs[orient], key=lambda s: s[0])
        groups: list[list[tuple[float, float, float]]] = []
        for lp, a, b, _t in items:
            if groups and lp - groups[-1][-1][0] <= tol:
                groups[-1].append((lp, a, b))
            else:
                groups.append([(lp, a, b)])
        for g in groups:
            line = round(sum(p[0] for p in g) / len(g))
            ivals = sorted((int(p[1]), int(p[2])) for p in g)
            cs, ce = ivals[0]
            runs = []
            for s, e in ivals[1:]:
                if s - ce <= maxgap:          # a door/window/opening → fill it, one whole run
                    ce = max(ce, e)
                else:                          # a real break (room-width) → separate border
                    runs.append((cs, ce))
                    cs, ce = s, e
            runs.append((cs, ce))
            for s, e in runs:
                rects.append((line - tk // 2, s, tk, e - s) if orient == "v"
                             else (s, line - tk // 2, e - s, tk))
    return rects


def extract_borders(gray: np.ndarray) -> list[list[float]]:
    """PHASE 1 — the editable border skeleton as [x, y, w, h] rectangles in % (0..100).
    Each rect is one straight border; the user drags/adds/deletes them, and Phase 2
    derives rooms from the confirmed set."""
    h, w = gray.shape
    rects = _border_rects(gray)
    return [[round(x / w * 1000) / 10, round(y / h * 1000) / 10,
             round(ww / w * 1000) / 10, round(hh / h * 1000) / 10] for x, y, ww, hh in rects]


def _rasterize(rects_pct: list[list[float]], w: int, h: int) -> np.ndarray:
    """Draw [x, y, w, h] %-rects (edited borders) back to a px mask."""
    mask = np.zeros((h, w), np.uint8)
    for x, y, ww, hh in rects_pct:
        x0, y0 = round(x / 100 * w), round(y / 100 * h)
        cv2.rectangle(mask, (x0, y0), (x0 + round(ww / 100 * w), y0 + round(hh / 100 * h)), 255, -1)
    return mask


def _room_labels(border_mask: np.ndarray) -> tuple[int, np.ndarray, np.ndarray]:
    """Enclosed rooms = free space NOT reachable from the image border. Returns
    (n_labels, labels, stats) from connected components of the enclosed free space."""
    h, w = border_mask.shape
    free = (border_mask == 0).astype(np.uint8)
    ff = free.copy()
    ffmask = np.zeros((h + 2, w + 2), np.uint8)
    for x in range(0, w, 2):
        if ff[0, x]:
            cv2.floodFill(ff, ffmask, (x, 0), 2)
        if ff[h - 1, x]:
            cv2.floodFill(ff, ffmask, (x, h - 1), 2)
    for y in range(0, h, 2):
        if ff[y, 0]:
            cv2.floodFill(ff, ffmask, (0, y), 2)
        if ff[y, w - 1]:
            cv2.floodFill(ff, ffmask, (w - 1, y), 2)
    enclosed = (ff == 1).astype(np.uint8)
    return cv2.connectedComponentsWithStats(enclosed, 8)


def _mask_to_poly(comp: np.ndarray, w: int, h: int) -> list[list[float]] | None:
    """One room-component mask → a simplified rectilinear polygon in % (0..100)."""
    cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    eps = max(2.0, POLY_EPS * max(w, h))
    approx = cv2.approxPolyDP(cnt, eps, True).reshape(-1, 2)
    if len(approx) < 3:
        return None
    return [[round(x / w * 1000) / 10, round(y / h * 1000) / 10] for x, y in approx]


def _rooms(border_mask: np.ndarray) -> list[list[list[float]]]:
    """Rooms = the regions ENCLOSED BY THE BORDERS, grown out to the border centreline
    so they TILE with no gaps. Borders are one uniform thickness, so each enclosed
    region grown by HALF that thickness meets its neighbours exactly on the centreline —
    clean rectilinear tiles, purely from the border mask (no bitmap anywhere)."""
    h, w = border_mask.shape
    big = max(w, h)
    grow = max(1, int(big * BORDER_UNIFORM) // 2 + 1)
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (2 * grow + 1, 2 * grow + 1))
    n, lbl, st, _ = _room_labels(border_mask)
    rooms = []
    for i in range(1, n):
        if st[i, cv2.CC_STAT_AREA] < MIN_ROOM * w * h:
            continue
        poly = _mask_to_poly(cv2.dilate((lbl == i).astype(np.uint8), k), w, h)
        if poly:
            rooms.append(poly)
    return rooms


def rooms_from_borders(borders: list[list[float]], w: int, h: int) -> list[list[list[float]]]:
    """PHASE 2 — rooms are simply the regions ENCLOSED BY THE BORDERS. Rasterize the
    border rects and flood-fill: every enclosed region is one room. No bitmap — the
    (user-edited) borders are the whole truth."""
    return _rooms(_rasterize(borders, w, h))


def _poly_mask(poly: list[list[float]], w: int, h: int) -> np.ndarray:
    """A filled %-polygon as a px mask (255 inside)."""
    m = np.zeros((h, w), np.uint8)
    pts = np.array([[round(x / 100 * w), round(y / 100 * h)] for x, y in poly], np.int32)
    cv2.fillPoly(m, [pts], 255)
    return m


def _true_runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Maximal [start, end) runs of True in a 1-D bool array."""
    d = np.flatnonzero(np.diff(np.concatenate(([0], mask.astype(np.int8), [0]))))
    return list(zip(d[::2].tolist(), d[1::2].tolist(), strict=False))


def _split_between(rect: list[float], ma: np.ndarray, mb: np.ndarray,
                   tk: int) -> tuple[list[list[float]], list[list[float]]]:
    """Split ONE border %-rect against the EXACT 'between A and B' criterion: a stretch
    of border separates A from B iff A lies on one flank and B on the other (sampled
    from the two room masks just past each face). Returns (kept, removed) %-rects.

    This is the literal definition of "the border between two rooms": a border along a
    THIRD room has that room on one flank — the criterion is false by construction, so
    it structurally CANNOT be nicked (no proximity band, no thresholds to tune)."""
    H, W = ma.shape
    x, y, ww, hh = rect
    vertical = hh >= ww
    if vertical:
        c = (x + ww / 2) / 100 * W
        p0 = max(0, round(y / 100 * H))
        p1 = min(H, round((y + hh) / 100 * H))
        denom = H
    else:
        c = (y + hh / 2) / 100 * H
        p0 = max(0, round(x / 100 * W))
        p1 = min(W, round((x + ww) / 100 * W))
        denom = W
    n = p1 - p0
    if n <= 0:
        return [rect], []
    pos = np.arange(p0, p1)
    a_neg = np.zeros(n, bool)
    a_pos = np.zeros(n, bool)
    b_neg = np.zeros(n, bool)
    b_pos = np.zeros(n, bool)
    # Two flank depths: rooms tile to the centreline, so the near sample sits just
    # inside the neighbouring room; the deeper one rides out polygon simplification.
    for off in (tk // 2 + 2, tk + 4):
        lo = int(np.clip(round(c - off), 0, (W if vertical else H) - 1))
        hi = int(np.clip(round(c + off), 0, (W if vertical else H) - 1))
        if vertical:
            a_neg |= ma[pos, lo] > 0
            a_pos |= ma[pos, hi] > 0
            b_neg |= mb[pos, lo] > 0
            b_pos |= mb[pos, hi] > 0
        else:
            a_neg |= ma[lo, pos] > 0
            a_pos |= ma[hi, pos] > 0
            b_neg |= mb[lo, pos] > 0
            b_pos |= mb[hi, pos] > 0
    between = (a_neg & b_pos) | (a_pos & b_neg)
    big = max(W, H)
    min_gap = max(3, int(0.004 * big))    # ignore single-pixel noise in the criterion
    min_keep = max(2, int(0.008 * big))   # drop leftover slivers
    erase = [(s, e) for s, e in _true_runs(between) if e - s >= min_gap]
    if not erase:
        return [rect], []

    def sub(s: int, e: int) -> list[float]:
        lo_pct, hi_pct = (p0 + s) / denom * 100, (p0 + e) / denom * 100
        return ([x, round(lo_pct, 1), ww, round(hi_pct - lo_pct, 1)] if vertical
                else [round(lo_pct, 1), y, round(hi_pct - lo_pct, 1), hh])

    kept: list[list[float]] = []
    prev = 0
    for s, e in erase:
        if s - prev >= min_keep:
            kept.append(sub(prev, s))
        prev = e
    if n - prev >= min_keep:
        kept.append(sub(prev, n))
    return kept, [sub(s, e) for s, e in erase]


def erase_border_between(borders: list[list[float]], poly_a: list[list[float]],
                         poly_b: list[list[float]], w: int,
                         h: int) -> tuple[list[list[float]], list[list[float]]]:
    """Merge two adjacent rooms by ERASING the border between them — borders are the
    source of truth, so two rooms become one by removing their divider, never by
    unioning polygons. Returns (kept, removed): the thinned border set plus the erased
    pieces (the UI previews those in red before the user confirms). removed == [] means
    the rooms are not adjacent."""
    big = max(w, h)
    tk = max(3, int(big * BORDER_UNIFORM))
    ma, mb = _poly_mask(poly_a, w, h), _poly_mask(poly_b, w, h)
    kept: list[list[float]] = []
    removed: list[list[float]] = []
    for rect in borders:
        k, r = _split_between(rect, ma, mb, tk)
        kept.extend(k)
        removed.extend(r)
    return kept, removed


if __name__ == "__main__":
    # Debug CLI: draw borders (red) + rooms (fill) over the source to eyeball them.
    #   python -m dida_planvision.pipeline <in.png> <overlay.png>
    import sys

    src, dst = sys.argv[1], sys.argv[2]
    with open(src, "rb") as _f:
        gray, W, H = _load_gray(_f.read())
    borders = extract_borders(gray)
    rooms = rooms_from_borders(borders, W, H)
    base = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    over = base.copy()
    palette = [(0, 200, 255), (0, 255, 120), (255, 120, 0), (200, 0, 255), (0, 160, 255),
               (255, 0, 120), (120, 255, 0), (255, 200, 0), (0, 255, 255), (160, 80, 255)]
    for idx, poly in enumerate(rooms):
        pts = np.array([[round(x / 100 * W), round(y / 100 * H)] for x, y in poly], np.int32)
        cv2.fillPoly(over, [pts], palette[idx % len(palette)])
    out = cv2.addWeighted(over, 0.4, base, 0.6, 0)
    for x, y, w, h in borders:
        x0, y0 = round(x / 100 * W), round(y / 100 * H)
        cv2.rectangle(out, (x0, y0), (x0 + round(w / 100 * W), y0 + round(h / 100 * H)), (0, 0, 200), -1)
    cv2.imwrite(dst, out)
    print(f"borders={len(borders)} rooms={len(rooms)} → {dst}")
