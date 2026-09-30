"""The adapter's side of the broker: everything it used to read from Postgres.

An adapter asks on `dida.cfg.<its name>` and the api answers (dida_api.broker).
Both directions are sealed with the adapter's own key (dida_core.identity), so a
request cannot be forged in another adapter's name and a reply — its secrets —
cannot be read by anyone else on the bus. Fernet carries a timestamp; the api
refuses a request older than `TTL`, which bounds a replayed one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Any

log = logging.getLogger("dida.broker")

SUBJECT = "dida.cfg.{}"
TTL = 60
PATIENCE = 30.0


class BrokerError(Exception):
    """The api answered and refused: an unknown op, one not open to this adapter,
    or the handler failed. Retrying will not help."""


def _jsonable(v: Any) -> Any:
    if isinstance(v, dt.date):  # datetime included
        return v.isoformat()
    raise TypeError(f"{type(v).__name__} is not JSON serializable")


def subject(adapter: str) -> str:
    return SUBJECT.format(adapter)


class Broker:
    def __init__(self, bus, adapter: str, key: str | None = None) -> None:
        from cryptography.fernet import Fernet

        from dida_core.identity import own_key

        self._bus = bus
        self._adapter = adapter
        self._fernet = Fernet(key or own_key())

    @property
    def adapter(self) -> str:
        return self._adapter

    def _seal(self, op: str, args: dict) -> bytes:
        return self._fernet.encrypt(json.dumps({"op": op, "args": args}, default=_jsonable).encode())

    async def call(self, op: str, *, patience: float = PATIENCE, **args: Any) -> Any:
        """Ask the api. Waits out an api restart (no responder / no answer) for up to
        `patience` seconds — a deploy restarts the api under running adapters, and a
        doorbell push in that window must still find its recipients."""
        from nats.errors import NoRespondersError
        from nats.errors import TimeoutError as NatsTimeout

        body = self._seal(op, args)
        deadline = time.monotonic() + patience
        delay = 0.5
        while True:
            try:
                msg = await self._bus.nc.request(subject(self._adapter), body, timeout=10)
                break
            except (NoRespondersError, NatsTimeout):
                if time.monotonic() + delay > deadline:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                # A retried request is re-sealed: the first seal may age past TTL.
                body = self._seal(op, args)
        reply = json.loads(self._fernet.decrypt(msg.data))
        if "error" in reply:
            raise BrokerError(f"{op}: {reply['error']}")
        return reply["result"]
