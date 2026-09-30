from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from dida_core import Bus, attach_log_bus, pg_pool, run_service, setup_logging
from home_core.health import HealthMarker
from home_core.tasks import spawn

from dida_netmgr.manager import NetManager

setup_logging()
log = logging.getLogger("dida.netmgr")


async def main() -> None:
    pool = await pg_pool()

    spawn(HealthMarker("dida", "netmgr").run_loop(), log=log, name="health loop")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # graceful stop like the other services
        loop.add_signal_handler(sig, stop.set)
    # The bus is for the JOURNAL only, so it is best-effort by construction:
    # netmgr owns this installation's foot on the IoT VLAN and must come up on a
    # box whose NATS is down, not wait for it. No bus → no events, everything else
    # unchanged.
    bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-netmgr", user="netmgr")
    try:
        await bus.connect()
    except Exception as exc:
        log.warning("netmgr: no bus (%s) — VLAN events will not be journalled", exc, exc_info=True)
        bus = None
    else:
        attach_log_bus(bus, "netmgr")
    try:
        await NetManager(pool, bus).run(stop)
    finally:
        if bus is not None:
            with contextlib.suppress(Exception):
                await bus.close()
        await pool.close()


if __name__ == "__main__":
    run_service(main())
