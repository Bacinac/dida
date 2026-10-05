from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from dida_core import Bus, attach_log_bus, pg_pool, run_service, setup_logging
from home_core.tasks import spawn

from dida_netmgr.manager import NetManager

setup_logging()
log = logging.getLogger("dida.netmgr")


async def journal_connection(manager: NetManager, stop: asyncio.Event) -> None:
    while not stop.is_set():
        bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-netmgr", user="netmgr")
        handler = None
        try:
            await bus.connect()
            handler = attach_log_bus(bus, "netmgr")
            manager.set_journal_bus(bus)
            while not stop.is_set() and not bus.nc.is_closed:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("journal connection failed (%s); retrying without blocking network management", exc, exc_info=True)
        finally:
            manager.set_journal_bus(None)
            if handler is not None:
                logging.getLogger().removeHandler(handler)
                handler.close()
            with contextlib.suppress(Exception):
                await bus.close()
        if not stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=2)


async def main() -> None:
    pool = await pg_pool()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # graceful stop like the other services
        loop.add_signal_handler(sig, stop.set)
    manager = NetManager(pool)
    journal = spawn(journal_connection(manager, stop), log=log, name="journal connection")
    try:
        await manager.run(stop)
    finally:
        stop.set()
        journal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await journal
        await pool.close()


if __name__ == "__main__":
    run_service(main())
