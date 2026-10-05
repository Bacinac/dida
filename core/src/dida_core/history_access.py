from __future__ import annotations

from contextlib import asynccontextmanager

HISTORY_LOCK = 0x4449444148495354
RECOVERY_KEY = "history_restore_recovery"


class HistoryBusyError(RuntimeError):
    pass


class HistoryRecoveryRequiredError(RuntimeError):
    pass


@asynccontextmanager
async def history_access(pool, *, exclusive: bool = False, recovery: bool = False):
    suffix = "" if exclusive else "_shared"
    async with pool.acquire() as conn:
        if not await conn.fetchval(f"SELECT pg_try_advisory_lock{suffix}($1)", HISTORY_LOCK):
            raise HistoryBusyError("History maintenance is in progress")
        try:
            if not recovery and await conn.fetchval(
                "SELECT value FROM app_settings WHERE key = $1", RECOVERY_KEY
            ):
                raise HistoryRecoveryRequiredError("History restore requires recovery before writes can resume")
            yield conn
        finally:
            await conn.execute(f"SELECT pg_advisory_unlock{suffix}($1)", HISTORY_LOCK)
