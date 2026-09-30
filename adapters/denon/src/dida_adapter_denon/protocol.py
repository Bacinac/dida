"""Denon/Marantz Telnet control-protocol parsing (port 23).

Pure functions over the line protocol so they're trivially testable and the
adapter stays about I/O. The protocol is line-based ASCII terminated by CR:
commands like ``MV40`` set, ``MV?`` queries, and the receiver also *pushes*
the same frames when you touch the unit/remote — so one persistent socket gives
both control and live state.
"""

from __future__ import annotations

NAMESPACE = "denon"

# We do NOT invent a percentage. The receiver reports master volume over Telnet on
# its own absolute 0–98 scale (0.5-dB steps) — the exact number the unit shows when
# Setup → Audio → Volume → Scale = "0-98", and the same scale its Limit / Power-On-Level
# settings use. So DIDA's volume value == the Marantz's own value, always. MVMAX carries
# the unit's Volume Limit (e.g. 80) and is used ONLY to clamp outgoing commands to the
# ceiling the receiver will accept. (The old code divided by MVMAX to make a percent,
# which ran DIDA's number ahead of the unit as soon as a Limit was set: raw 40 showed
# as 40/80·100 = 50 instead of the 40 on the receiver.)


def parse_volume(token: str) -> float | None:
    """``MV40`` → 40.0, ``MV405`` → 40.5 (3rd digit is the half-step)."""
    digits = "".join(c for c in token if c.isdigit())
    if not digits:
        return None
    return int(digits) / 10 if len(digits) == 3 else float(int(digits))


def vol_to_pct(raw: float) -> int:
    """Master volume on the receiver's native 0–98 scale, rounded to an int and
    capped at 100 — shown as-is, so DIDA's number equals what the Marantz displays."""
    return max(0, min(100, round(raw)))


def pct_to_raw(pct: int, max_raw: float) -> int:
    """The UI value already IS a point on the 0–98 scale; pass it straight through,
    clamped to MVMAX (the unit's Volume Limit) so we never send above the ceiling —
    a request past the limit snaps to the real max the receiver echoes back."""
    return max(0, min(int(max_raw), pct))


def build_sources(
    ssfun: dict[str, str], sssod: dict[str, str]
) -> tuple[list[str], dict[str, str], dict[str, str]]:
    """Combine the rename table (SSFUN: code→custom label) with the enabled set
    (SSSOD: code→USE/DEL) into:
      * option labels (only enabled inputs, in SSSOD order),
      * label→code (to send SI<code>),
      * code→label (to display the current SI<code>).
    Inputs enabled but not renamed fall back to their code as the label."""
    code_to_label: dict[str, str] = {}
    for code, status in sssod.items():
        if status.upper() == "USE":
            code_to_label[code] = ssfun.get(code) or code
    # Renamed inputs the firmware didn't list in SSSOD at all are kept; ones it
    # explicitly marked DEL are NOT re-added (they're disabled on the unit).
    for code, label in ssfun.items():
        if code not in sssod:
            code_to_label.setdefault(code, label)
    label_to_code = {label: code for code, label in code_to_label.items()}
    return list(code_to_label.values()), label_to_code, code_to_label
