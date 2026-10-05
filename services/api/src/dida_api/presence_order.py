from __future__ import annotations

import contextlib
import json
import math
import time
from decimal import Decimal

from dida_core import StateUpdate
from fastapi import HTTPException


def timestamp_ns(value, *, milliseconds: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0 or (
        isinstance(value, float) and not math.isfinite(value)
    ):
        raise HTTPException(400, "observation timestamp required")
    scale = 1_000_000 if milliseconds else 1_000_000_000
    if value > (time.time_ns() + 300_000_000_000) / scale:
        raise HTTPException(400, "observation timestamp is in the future")
    ts = int(Decimal(str(value)) * scale)
    if ts > time.time_ns() + 300_000_000_000:
        raise HTTPException(400, "observation timestamp is in the future")
    return ts


def frame_timing(body: dict) -> dict:
    observed = timestamp_ns(body["tst_ms"], milliseconds=True) if "tst_ms" in body else timestamp_ns(body.get("tst"))
    sequence, reporter = body.get("seq"), body.get("reporter")
    if (sequence is not None or reporter is not None) and (
        isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence < 2**63
        or not isinstance(reporter, str) or not 1 <= len(reporter) <= 64
    ):
        raise HTTPException(400, "invalid reporter sequence")
    return {"observed_ns": observed, "sequence": sequence, "reporter": reporter}


async def publish_ordered(state, username: str, entity_id: str, name: str, observed_ns: int,
                          values: list[tuple], *, event: str | None = None, desc: str | None = None,
                          sequence: int | None = None, reporter: str | None = None) -> dict:
    async with state.pool.acquire() as conn, conn.transaction():
        user_id = await conn.fetchval("SELECT id FROM users WHERE username = $1 FOR UPDATE", username)
        if user_id is None:
            return {"accepted": False, "reason": "unknown_user"}
        previous = await conn.fetchrow("SELECT * FROM presence_reports WHERE user_id = $1", user_id)
        current = await conn.fetchrow(
            "SELECT value, ts_ns FROM current_state WHERE entity_id = $1 AND capability = 'location'", entity_id)
        projected_ns = max(previous["projected_ns"] if previous else 0, current["ts_ns"] if current else 0)
        head = previous["observed_ns"] if previous else 0
        location = previous["location"] if previous else None
        if current and (not previous or current["ts_ns"] > previous["projected_ns"]):
            head = max(head, current["ts_ns"])
            location = current["value"]
            if isinstance(location, str):
                with contextlib.suppress(ValueError):
                    location = json.loads(location)
        if observed_ns < head or (observed_ns == head and not (
            previous and reporter is not None and reporter == previous["reporter"]
            and sequence is not None and previous["sequence"] is not None and sequence > previous["sequence"]
        )):
            return {"accepted": False, "reason": "stale_report", "zone": location}
        accepted = event != "leave" or location == desc
        if accepted:
            projected_ns = max(observed_ns, projected_ns + 1)
            for capability, value in values:
                await state.bus.publish_state(StateUpdate(
                    entity_id=entity_id, capability=capability, value=value,
                    adapter="presence", ts_ns=projected_ns, name=name, diagnostic=False))
            await state.bus.nc.flush()
            location = values[0][1]
        await conn.execute(
            "INSERT INTO presence_reports (user_id, observed_ns, projected_ns, reporter, sequence, location) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (user_id) DO UPDATE SET "
            "observed_ns = EXCLUDED.observed_ns, projected_ns = EXCLUDED.projected_ns, "
            "reporter = EXCLUDED.reporter, sequence = EXCLUDED.sequence, location = EXCLUDED.location",
            user_id, observed_ns, projected_ns, reporter, sequence, location)
        return {"accepted": True, "zone": location} if accepted else {
            "accepted": False, "reason": "stale_leave", "zone": location}
