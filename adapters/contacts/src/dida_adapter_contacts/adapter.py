from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from dida_core import AdapterConfig, Bus, Command, EntityInfo, FailureGate, StateUpdate
from dida_core.broker import BrokerError
from dida_core.people import digest

from dida_adapter_contacts.google import read_book

log = logging.getLogger("dida.adapter.contacts")

NAMESPACE = "contacts"
BIRTHDAYS = f"{NAMESPACE}:birthdays"
TICK = 60.0


class ContactsAdapter:
    """The household's address book, and whose birthday it is.

    Reads the contacts book into `people` once a day and publishes two facets of
    `contacts:birthdays`:

      `binary` — somebody has a birthday inside the announce window. This is the
                 edge a rule triggers on.
      `text`   — `0=Ana:34;1=Marko:12`: who, how many days off, which age they turn.
                 Deliberately not a sentence — the wording is Croatian, and Croatian
                 belongs in the rule that speaks it, not in an adapter.

    Both are READ capabilities, not the writable `boolean`/`enum` a helper carries:
    this is computed, and a switch a user can flip but the next tick overwrites is
    a control that lies.

    Only people opted into `announce` reach those facets; the table holds the whole
    book, because a photo library asking "when was this person born" asks about
    anyone, while the kitchen speaker is for the household.

    A dead refresh token fails LOUD and keeps the last-good table: birthdays go on
    being announced from what was already read, and the badge says to reconnect.
    Implements `dida_core.Adapter`.
    """

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._announced = False
        self._last: dict[str, object] = {}   # capability -> last published value (dedupe)
        self._synced_on: str = ""            # local date of the last successful sync
        self._syncing = False

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig(NAMESPACE, self.broker)
        gate = FailureGate(self.status, threshold=3)
        log.info("contacts adapter up — owning %s", BIRTHDAYS)
        while True:
            try:
                await self._cfg.load()
                now = datetime.now(ZoneInfo((await self.broker.call("house"))["tz"]))
                # A read can also be ASKED for: correcting a name at the source and
                # then waiting until four in the morning to see it here is not an
                # answer. The request is a row somebody else sets and this clears —
                # the sync itself stays where it belongs.
                asked = await self.broker.call("take_setting", key="contacts_sync_requested")
                if asked or self._due(now):
                    await self._sync(now, forced=bool(asked))
                count = await self._publish_birthdays(now)
                total = (await self.broker.call("people"))["total"]
                if not await self._connected():
                    # Not an error: an installation reading exports has no token and
                    # is working exactly as intended.
                    self.status.idle(f"{total} people (not connected to Google)")
                    gate.reset()
                else:
                    gate.ok(f"{total} people, {count} announced")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("contacts: tick failed")
                gate.fail(str(exc) or "tick failed")
            await asyncio.sleep(TICK)

    async def handle_command(self, command: Command) -> None:
        return  # read-only source — the book is commanded in the book

    async def stop(self) -> None:
        self._bus = None

    # ── the daily read ────────────────────────────────────────────────────────

    def _due(self, now: datetime) -> bool:
        """Once a day, at the configured local hour. Also on the first tick after a
        start that finds no record of today's sync — a restart at 09:00 must not
        wait until tomorrow's 04:00 to notice a birth date added yesterday."""
        if self._syncing:
            return False
        return self._synced_on != now.date().isoformat() and now.hour >= self._cfg.int("sync_hour", 4)

    async def _connected(self) -> bool:
        return bool(await self._refresh_token())

    async def _refresh_token(self) -> str:
        """The stored token, '' when there is none — and a raised error when there
        is one that cannot be read. A rotated DIDA_SECRET_KEY leaves a connection
        that exists and does not work; reporting that as 'not connected' would read
        as an installation deliberately using export files."""
        try:
            row = await self.broker.call("stored", key="_oauth")
        except BrokerError as exc:
            raise RuntimeError(
                "the stored Google token cannot be decrypted (DIDA_SECRET_KEY rotated?) "
                "— reconnect under Settings → Adapters"
            ) from exc
        if not row:
            return ""
        try:
            blob = json.loads(row)
        except Exception as exc:
            raise RuntimeError(
                "the stored Google token cannot be decrypted (DIDA_SECRET_KEY rotated?) "
                "— reconnect under Settings → Adapters"
            ) from exc
        return str(blob.get("refresh_token") or "")

    async def _sync(self, now: datetime, *, forced: bool = False) -> None:
        token = await self._refresh_token()
        if not token:
            # Deliberately WITHOUT marking the day done. Doing that cost nothing on
            # an installation that reads export files, and everything on the one
            # that connects: consent granted at four in the afternoon left the day
            # already ticked off, so the first read waited until tomorrow's small
            # hours and the card said "Nobody here yet" for the rest of the day.
            # The retry it avoided is one small query a minute.
            return
        self._syncing = True
        try:
            book = await read_book(
                self._cfg.get("client_id"), self._cfg.get("client_secret"), token
            )
            # Prune: this read is complete (read_book pages to the end or raises),
            # so a contact missing from it is a contact deleted in the book.
            report = await self.broker.call("store_book", book=book, source="google", prune=True)
            self._synced_on = now.date().isoformat()
            report["at"] = now.isoformat(timespec="seconds")
            await self.broker.call("set_setting", key="contacts_last_sync",
                                   value=json.dumps(report, default=str))
            if report["no_year"]:
                # Reported, never invented — the year is the whole reason a birth
                # date is kept, and these have none.
                log.warning("contacts: %d birthday(s) with no year: %s",
                            len(report["no_year"]),
                            ", ".join(p["name"] for p in report["no_year"][:10]))
            log.info("contacts: synced%s — %s", " (asked for)" if forced else "",
                     {k: v for k, v in report.items() if k != "no_year"})
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Google People API: {exc}") from exc
        finally:
            self._syncing = False

    # ── what the house needs today ────────────────────────────────────────────

    async def _publish_birthdays(self, now: datetime) -> int:
        within = max(self._cfg.int("announce_days", 1), 0)
        rows = (await self.broker.call("people"))["announced"]
        value = digest([(r["name"], date.fromisoformat(r["born_on"])) for r in rows], now.date(), within)
        await self._publish("text", value)
        await self._publish("binary", bool(value))
        return len(value.split(";")) if value else 0

    async def _publish(self, capability: str, value) -> None:
        if self._bus is None:
            return
        await self._announce_catalog()
        if self._last.get(capability) == value:
            return
        self._last[capability] = value
        await self._bus.publish_state(StateUpdate(
            entity_id=BIRTHDAYS, capability=capability, value=value,
            adapter=NAMESPACE, ts_ns=time.time_ns(), name="Birthdays",
        ))

    async def _announce_catalog(self) -> None:
        """The catalog entry exists before any value does — an installation with
        nobody opted in still shows the entity, so a rule can be written against it
        before the first birthday comes round."""
        if self._announced or self._bus is None:
            return
        self._announced = True
        await self._bus.publish_entity(EntityInfo(
            entity_id=BIRTHDAYS, adapter=NAMESPACE, capabilities=["text", "binary"],
            name="Birthdays", device=BIRTHDAYS, device_name="Birthdays",
            device_type="sensor",
        ))
