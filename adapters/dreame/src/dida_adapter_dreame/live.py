"""The robot while it works: where it is, where it has been, what it was sent to do.

None of this is in the saved map — that file reports the robot at 0,0 whatever it
is doing. The live picture arrives on the maker's own broker, which pushes a map
frame every couple of seconds during a job: position and heading in the header, the
newest piece of track and the area being cleaned in the tail.

Kept in memory and handed over on request rather than published as state: a moving
robot would otherwise write a row of history per second for the rest of its life,
and nobody wants to query where it stood last March.
"""

from __future__ import annotations

import base64
import json
import re
import ssl
import struct
import zlib
from dataclasses import dataclass, field

MAP_PROPERTY = (6, 1)          # the live map frame
_TRACK_POINT = re.compile(r"([-\d]+),([-\d]+)")
_MAX_TRACK = 2000              # points; a long job is a few hundred


@dataclass
class Live:
    """What the last frames said. Empty until the robot moves."""

    robot: tuple[int, int] | None = None
    heading: int = 0
    charger: tuple[int, int] | None = None
    areas: list[list[int]] = field(default_factory=list)
    track: list[tuple[int, int]] = field(default_factory=list)
    at_ms: int = 0

    def as_json(self) -> dict:
        return {
            "robot": list(self.robot) if self.robot else None,
            "heading": self.heading,
            "charger": list(self.charger) if self.charger else None,
            "areas": self.areas,
            "track": [list(p) for p in self.track],
            "at_ms": self.at_ms,
        }

    def clear_track(self) -> None:
        self.track.clear()


def client_id(uid: str, host: str, agent: str) -> str:
    """The shape their broker expects. Anything else is refused as unauthorized —
    the credentials are fine, the NAME is what it checks."""
    return f"p_{uid}_{agent}_{host}"


def tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def apply_frame(live: Live, payload: bytes) -> bool:
    """Fold one pushed message into the live picture. Returns whether it carried a
    map frame at all — the same topic also pushes ordinary property changes."""
    try:
        body = json.loads(payload)
        data = body.get("data") if isinstance(body, dict) else None
        params = (data.get("params") if isinstance(data, dict) else None) or []
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False

    changed = False
    for prop in params if isinstance(params, list) else []:
        # The same topic carries plain acknowledgements too, where a parameter is a
        # bare string rather than a property. One of those used to end the whole
        # subscription and cost a reconnect every few seconds.
        if not isinstance(prop, dict) or (prop.get("siid"), prop.get("piid")) != MAP_PROPERTY:
            continue
        value = prop.get("value")
        if not isinstance(value, str) or not value:
            continue
        try:
            raw = zlib.decompress(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        except (ValueError, zlib.error):
            continue
        if len(raw) < 27:
            continue

        def i16(off: int, frame: bytes = raw) -> int:
            return struct.unpack_from("<h", frame, off)[0]

        live.robot = (i16(5), i16(7))
        live.heading = i16(9)
        live.charger = (i16(11), i16(13))
        width, height = i16(19), i16(21)

        tail = raw[27 + max(0, width) * max(0, height):]
        try:
            text = tail.decode("utf-8", "replace")
            extra = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except ValueError:
            extra = {}
        live.at_ms = int(extra.get("timestamp_ms") or live.at_ms)
        areas = (extra.get("da2") or {}).get("areas")
        live.areas = [list(a[:4]) for a in areas] if isinstance(areas, list) else []
        # The track arrives a piece at a time — each frame adds where it has just
        # been, so the line is accumulated here rather than sent whole.
        for x, y in _TRACK_POINT.findall(str(extra.get("tr") or "")):
            point = (int(x), int(y))
            if not live.track or live.track[-1] != point:
                live.track.append(point)
        if len(live.track) > _MAX_TRACK:
            del live.track[:len(live.track) - _MAX_TRACK]
        changed = True
    return changed
