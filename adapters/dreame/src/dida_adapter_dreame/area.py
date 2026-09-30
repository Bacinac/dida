"""Sending the robot to a place on the FLOOR PLAN.

The house speaks one frame — the plan, in percent — and the robot speaks another:
millimetres around wherever it happened to start mapping. This module is the
translation, and it is deliberately a pure function of two ANCHORS:

The translation is not fitted from points any more: the map is DRAWN and laid over
the plan by hand, and where it lies is the whole answer. Anchors were the earlier
attempt and a poor one — two of them cannot tell a turned map from a mirrored one,
and nothing on the screen showed how badly they agreed.

Two matched points fix a rotation, a scale and an offset — no assumption about
which way their map is turned, which is the part that is never what it looks like
(ours reads as mirrored, or as 180° rotated, depending on which way you take their
y to grow; the arithmetic does not care).

A map the robot rebuilds moves its own origin, so the anchors are checked against
the dock the current map reports: if that moved, the calibration is stale and the
caller must refuse rather than send the robot to a guessed place.
"""

from __future__ import annotations

import base64
import json
import math
import struct
import zlib
from dataclasses import dataclass

MIN_ZONE_MM = 300.0      # the robot refuses anything under ~2 cells; keep clear of it
MAX_ZONE_MM = 8000.0


class MapError(Exception):
    """The map could not be read, or the calibration no longer fits it."""


# The room "name" is a TYPE the owner picks in the app; this is that vocabulary.
ROOM_TYPES: dict[int, str] = {
    0: "Room", 1: "Living Room", 2: "Primary Bedroom", 3: "Study", 4: "Kitchen",
    5: "Dining Hall", 6: "Bathroom", 7: "Balcony", 8: "Corridor", 9: "Utility Room",
    10: "Closet", 11: "Meeting Room", 12: "Office", 13: "Fitness Area",
    14: "Recreation Area", 15: "Secondary Bedroom",
}


@dataclass(frozen=True)
class Room:
    """One room of the robot's own partition, with its middle in map millimetres."""

    id: int
    name: str
    centre: tuple[float, float]
    area_m2: float


@dataclass(frozen=True)
class MapFrame:
    """What one map file says about where things are, in the robot's millimetres."""

    grid_mm: int
    width: int
    height: int
    origin: tuple[int, int]
    charger: tuple[int, int]
    rooms: tuple[Room, ...] = ()
    cells: bytes = b""     # the grid, CROPPED to what the robot has actually seen

    def room(self, room_id: int) -> Room | None:
        return next((r for r in self.rooms if r.id == room_id), None)


def _entry_charger(entry: dict) -> tuple[int, int] | None:
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(entry["map"] + "=" * (-len(entry["map"]) % 4)))
        return (struct.unpack_from("<h", raw, 11)[0], struct.unpack_from("<h", raw, 13)[0])
    except (KeyError, TypeError, ValueError, struct.error, zlib.error):
        return None


def map_key(entry: dict) -> str:
    """What an arrangement is filed under. The NAME the owner gave the map in the app,
    because the id beside it is the slot in a list and slots shuffle."""
    name = str(entry.get("name") or "").strip()
    return name or f"#{entry.get('id', 0)}"


def pick_map(blob: bytes, charger: tuple[int, int] | None = None) -> tuple[dict, str]:
    """The map the robot is standing on, and the key its arrangement is filed under.

    Not by the file's `curr_id`: with two storeys saved it reads 8 while the entries
    are numbered 1 and 0 — it is not their id. What does identify the map is the DOCK,
    which each map places in its own frame and which the live stream reports from
    wherever the robot actually is. Without a live position the first map is used and
    the choice corrects itself on the next frame."""
    try:
        env = json.loads(blob)
        maps = [m for m in (env.get("mapstr") or []) if m.get("map")]
        if not maps:
            raise MapError("the account has no saved map yet")
        entry = maps[0]
        if charger is not None:
            def distance(candidate: dict) -> float:
                own = _entry_charger(candidate)
                if own is None:
                    return float("inf")
                return ((own[0] - charger[0]) ** 2 + (own[1] - charger[1]) ** 2) ** 0.5
            nearest = min(maps, key=distance)
            if distance(nearest) < 1000:      # the dock never moves a metre on its own
                entry = nearest
    except MapError:
        raise
    except Exception as exc:
        raise MapError(f"unreadable map file: {exc}") from exc
    return entry, map_key(entry)


def decode_map(blob: bytes, charger: tuple[int, int] | None = None) -> MapFrame:
    """The saved-map file → the frame. The payload is base64url over zlib; the
    first 27 bytes are the header and the tail carries a JSON block whose origin
    and charger win over the header when present (the app trusts those)."""
    try:
        entry, _key = pick_map(blob, charger)
        raw = zlib.decompress(base64.urlsafe_b64decode(entry["map"] + "=" * (-len(entry["map"]) % 4)))
    except MapError:
        raise
    except Exception as exc:  # malformed download, empty map, changed envelope
        raise MapError(f"unreadable map file: {exc}") from exc
    if len(raw) < 27:
        raise MapError("map file has no header")

    def i16(off: int) -> int:
        return struct.unpack_from("<h", raw, off)[0]

    grid, width, height = i16(17), i16(19), i16(21)
    if grid <= 0 or width <= 0 or height <= 0:
        raise MapError("map has no grid yet — the robot has not mapped the house")
    origin = (i16(23), i16(25))
    charger = (i16(11), i16(13))

    extra: dict = {}
    tail = raw[27 + width * height:]
    try:
        text = tail.decode("utf-8", "replace")
        extra = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except ValueError:
        extra = {}
    origin = tuple(extra.get("origin") or origin)        # type: ignore[assignment]
    charger = tuple(extra.get("charger") or charger)     # type: ignore[assignment]

    grid_bytes = raw[27:27 + width * height]

    # CROP to what the robot has actually driven through. The robot's canvas is
    # bigger than the house — it leaves room to grow — and an overlay padded with
    # blank edges is impossible to line up by eye: you would be sizing the padding,
    # not the rooms.
    left_c, right_c, top_c, bottom_c = width, -1, height, -1
    for index, pixel in enumerate(grid_bytes):
        if not pixel:
            continue
        cx, cy = index % width, index // width
        left_c, right_c = min(left_c, cx), max(right_c, cx)
        top_c, bottom_c = min(top_c, cy), max(bottom_c, cy)
    if right_c < left_c or bottom_c < top_c:
        raise MapError("the map is empty — the robot has not driven anywhere yet")
    crop_w, crop_h = right_c - left_c + 1, bottom_c - top_c + 1
    cells = bytearray(crop_w * crop_h)
    for row in range(crop_h):
        start = (top_c + row) * width + left_c
        cells[row * crop_w:(row + 1) * crop_w] = grid_bytes[start:start + crop_w]
    origin = (origin[0] + left_c * grid, origin[1] + top_c * grid)

    # A cell carries its room in the low six bits; the seventh marks carpet and the
    # eighth a wall, and neither belongs to a room.
    sums: dict[int, list[float]] = {}
    for index, pixel in enumerate(cells):
        if pixel >> 7:
            continue
        room_id = pixel & 0x3F
        if not room_id:
            continue
        acc = sums.setdefault(room_id, [0.0, 0.0, 0.0])
        acc[0] += index % crop_w
        acc[1] += index // crop_w
        acc[2] += 1

    seg_inf = extra.get("seg_inf") or {}
    rooms = []
    for room_id, (sx, sy, n) in sorted(sums.items()):
        kind = int((seg_inf.get(str(room_id)) or {}).get("type", 0))
        rooms.append(Room(
            id=room_id,
            name=ROOM_TYPES.get(kind, "Room"),
            centre=(origin[0] + (sx / n) * grid, origin[1] + (sy / n) * grid),
            area_m2=round(n * (grid / 1000) ** 2, 1),
        ))
    return MapFrame(grid, crop_w, crop_h, origin, charger, tuple(rooms), bytes(cells))


@dataclass(frozen=True)
class Placement:
    """Where the robot's map lies on the floor plan: a box in plan percent, turned by
    an angle, optionally mirrored. This IS the calibration — a picture someone has
    dragged into place says everything a pair of anchor points was meant to say, and
    says it where it can be seen.

    Their frame is not merely turned relative to ours: read with our y it comes out
    mirrored, which no rotation can express. Hence the flag rather than an angle
    alone."""

    x: float
    y: float
    w: float
    rotation: float = 0.0
    mirrored: bool = False
    floor: str = ""        # which storey of OUR plan this map belongs to

    def height_on_plan(self, frame: MapFrame) -> float:
        return self.w * (frame.height / frame.width)

    def to_map(self, point: tuple[float, float], frame: MapFrame) -> tuple[float, float]:
        """A point on the plan (percent) → the robot's millimetres."""
        h = self.height_on_plan(frame)
        cx, cy = self.x + self.w / 2, self.y + h / 2
        angle = math.radians(-self.rotation)
        dx, dy = point[0] - cx, point[1] - cy
        rx = dx * math.cos(angle) - dy * math.sin(angle)
        ry = dx * math.sin(angle) + dy * math.cos(angle)
        u = (rx + self.w / 2) / self.w
        if self.mirrored:
            u = 1.0 - u
        v = (ry + h / 2) / h
        return (frame.origin[0] + u * frame.width * frame.grid_mm,
                frame.origin[1] + v * frame.height * frame.grid_mm)

    @staticmethod
    def load(raw: str, map_key: str = "") -> Placement | None:
        """One placement per MAP: each storey the robot knows lies over a different
        plan, and one arrangement cannot serve both."""
        if not raw.strip():
            return None
        try:
            store = json.loads(raw)
            d = store.get(map_key) if isinstance(store, dict) and "x" not in store else store
            if d is None:
                return None
            return Placement(float(d["x"]), float(d["y"]), float(d["w"]),
                             float(d.get("rotation", 0)), bool(d.get("mirrored", False)),
                             str(d.get("floor", "")))
        except Exception as exc:
            raise MapError(f"unreadable placement: {exc}") from exc



# ── the map as a picture ──────────────────────────────────────────────────────
#
# Drawn here rather than in the browser because the grid is the robot's, not the
# house's: 226 x 260 cells of room ids that mean nothing to anyone downstream. A
# PNG is written by hand — one zlib stream and three chunks — so the adapter needs
# no imaging library for what is, in the end, a palette lookup per cell.

_ROOM_COLOURS: tuple[tuple[int, int, int], ...] = (
    (56, 152, 236), (52, 199, 143), (233, 176, 61), (214, 106, 196),
    (231, 111, 81), (86, 160, 211), (144, 190, 109), (244, 162, 97),
)
_WALL = (38, 44, 56)


def _png(width: int, height: int, rgba: bytearray) -> bytes:
    """A minimal RGBA PNG: signature, header, one compressed image, end."""
    import struct as _struct

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (_struct.pack(">I", len(payload)) + kind + payload
                + _struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(height):
        raw.append(0)                                   # filter: none
        raw += rgba[y * width * 4:(y + 1) * width * 4]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", _struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def all_maps(blob: bytes) -> list[tuple[str, dict]]:
    """Every saved map, by key. A storey the robot is not standing on right now is
    still a storey whose picture someone may need to lay over its plan."""
    try:
        env = json.loads(blob)
        return [(map_key(m), m) for m in (env.get("mapstr") or []) if m.get("map")]
    except Exception as exc:
        raise MapError(f"unreadable map file: {exc}") from exc


def render_entry(entry: dict) -> tuple[bytes, MapFrame]:
    """One named map from the file, drawn."""
    return render(json.dumps({"mapstr": [entry], "curr_id": entry.get("id", 0)}).encode())


def render(blob: bytes, charger: tuple[int, int] | None = None) -> tuple[bytes, MapFrame]:
    """The saved map as a transparent PNG, one colour per room, walls darker.

    Cropped to what the robot has seen, so the picture's edges ARE the map's edges
    and laying it over a floor plan is a matter of matching rooms rather than
    guessing where the blank margin ends."""
    frame = decode_map(blob, charger)
    width, height = frame.width, frame.height
    pixels = bytearray(width * height * 4)
    for index, pixel in enumerate(frame.cells):
        at = index * 4
        if pixel >> 7:
            pixels[at:at + 4] = bytes((*_WALL, 255))
            continue
        room = pixel & 0x3F
        if not room:
            continue                                     # never seen: stays clear
        colour = _ROOM_COLOURS[(room - 1) % len(_ROOM_COLOURS)]
        pixels[at:at + 4] = bytes((*colour, 168))
    return _png(width, height, pixels), frame


def zone_for(placement: Placement, frame: MapFrame,
             corner_a: tuple[float, float], corner_b: tuple[float, float]) -> list[int]:
    """The area drawn on the plan, as [x1, y1, x2, y2] in the robot's millimetres.

    Both corners go through the placement and the result is normalised AFTERWARDS: a
    mirrored or turned map swaps which corner is which, and the robot wants the
    rectangle written left-top to right-bottom in ITS frame, not in the frame of the
    hand that drew it. A rectangle too small is grown about its own middle rather
    than refused — the robot rejects anything under a couple of cells, and a
    fingertip is smaller than that."""
    ax, ay = placement.to_map(corner_a, frame)
    bx, by = placement.to_map(corner_b, frame)
    x1, x2 = sorted((ax, bx))
    y1, y2 = sorted((ay, by))
    if x2 - x1 < MIN_ZONE_MM:
        mid = (x1 + x2) / 2
        x1, x2 = mid - MIN_ZONE_MM / 2, mid + MIN_ZONE_MM / 2
    if y2 - y1 < MIN_ZONE_MM:
        mid = (y1 + y2) / 2
        y1, y2 = mid - MIN_ZONE_MM / 2, mid + MIN_ZONE_MM / 2
    if x2 - x1 > MAX_ZONE_MM or y2 - y1 > MAX_ZONE_MM:
        raise MapError("that area is larger than the robot takes in one job — draw a "
                       "smaller one, or simply start a full clean")
    return [round(x1), round(y1), round(x2), round(y2)]
