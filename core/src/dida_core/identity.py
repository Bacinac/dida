"""Who a client is on the bus: a key only it and the api hold, and a bus login
only it and the NATS server hold.

An adapter talks to devices and clouds through third-party libraries — the most
exposed code in the house — so it holds neither database credentials nor the root
key. It asks the api for what it needs (its config, its secrets, the few house
facts it reads) through the broker, sealing each request with its own key. The api
derives every client's key from the root; a client cannot derive another's. The
Matter bridge is a client like an adapter, not one of them.

On the bus each adapter logs in as itself, and the server lets it publish only
under its own name (`dida.state.<name>.…`, `dida.journal.<name>` — see
dida_core.events) and command only the devices `COMMANDS` names. The core services
log in as CORE and may do anything. A compromised adapter can then lie only about
its own devices.

`python -m dida_core.identity` is the one-shot `keys` service: it writes each
client's key, each bus identity's password and each database role's password into
its own directory of the `dida-keys` volume, and each container mounts only its
own; the NATS server mounts the user list. Then it provisions the roles
(dida_core.db_roles). Deterministic, so rerunning it on every `up` changes nothing
and there is nothing to back up.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from dida_core.broker import subject as broker_subject
from dida_core.crypto import adapter_key, bus_password, db_password
from dida_core.db import pg_pool
from dida_core.db_roles import ROLES, provision
from dida_core.events import (
    CORE,
    ENGINE_EVENTS_SUBJECT,
    entity_subject,
    heartbeat_subject,
    journal_subject,
    logs_subject,
    namespace_commands,
    reachability_subject,
    state_subject,
)
from dida_core.health import status_subject

ADAPTERS: frozenset[str] = frozenset({
    "androidtv", "announce", "astro", "baba", "broadlink", "calendar", "cast",
    "cloudflare", "contacts", "denon", "dlna", "dreame", "ecowitt", "esphome",
    "frigate", "govee", "harmony", "heos", "homekit", "iammeter", "landroid",
    "midea", "mqtt", "notify", "opus", "panasonic", "peer", "presence", "roidmi",
    "samsungtv", "shelly", "smartthings", "solar", "tuya", "unifi", "virtual",
    "volumio",
})
SERVICES: frozenset[str] = frozenset({"matter-bridge"})
CLIENTS = ADAPTERS | SERVICES
# On the bus but not the broker's: netmgr journals the VLANs it brings up.
BUS_CLIENTS = CLIENTS | {"netmgr"}

# Whose devices a client may command ("*" = anyone's). Every other client commands
# nothing: a command reaches an adapter, it never sends one. announce speaks through
# the speakers that play a URL, denon presses the projector's IR and switches the
# Harmony activity, the Matter bridge does what a voice assistant asks.
COMMANDS: dict[str, frozenset[str]] = {
    "announce": frozenset({"cast", "dlna", "heos", "volumio"}),
    "denon": frozenset({"broadlink", "harmony"}),
    "matter-bridge": frozenset({"*"}),
}
# What a client asks of the core services beyond its broker.
ASKS: dict[str, tuple[str, ...]] = {
    "cloudflare": ("dida.runner.tunnel",),
    "dlna": ("dida.radio.stations",),
    "notify": ("dida.camera.snap",),
    "volumio": ("dida.radio.stations",),
}
# What a client hears beyond what is addressed to it by name.
HEARS: dict[str, tuple[str, ...]] = {
    "dreame": ("dida.vacuum.map", "dida.vacuum.live"),
    "matter-bridge": (ENGINE_EVENTS_SUBJECT,),
}
# Whose records a client reads through its broker beyond its own namespace and the
# devices it commands: solar follows the sun astro reports, calendar cuts its
# schedules by astro's configured place, notify and unifi place people.
READS: dict[str, frozenset[str]] = {
    "calendar": frozenset({"astro"}),
    "notify": frozenset({"presence"}),
    "solar": frozenset({"astro"}),
    "unifi": frozenset({"presence"}),
}


def readable(name: str) -> frozenset[str]:
    """The entity namespaces (and adapters' configs) client `name` may read."""
    return frozenset({name}) | COMMANDS.get(name, frozenset()) | READS.get(name, frozenset())

KEY_FILE = Path(os.environ.get("DIDA_KEY_FILE", "/run/dida-key/key"))

_OWNER = 1000  # the `dida` user every image runs as (docker/base.Dockerfile)


def own_key() -> str:
    """This adapter's key, from the file its container mounts. A missing file is a
    deployment error — the adapter must not start without an identity."""
    try:
        return KEY_FILE.read_text().strip()
    except OSError as exc:
        raise RuntimeError(f"adapter key not readable at {KEY_FILE} — is the keys service up?") from exc


def permissions(name: str) -> dict:
    """What bus client `name` may publish and hear: its own facts under its own
    name, requests to its broker, the commands `COMMANDS` grants; commands and
    requests addressed to it, and replies on its own inboxes."""
    publish = [
        state_subject(name, ">"), entity_subject(name, ">"),
        reachability_subject(name), heartbeat_subject(name),
        journal_subject(f"adapter:{name}"), logs_subject(f"adapter:{name}"),
        *([broker_subject(name)] if name in CLIENTS else []),
        *(namespace_commands(ns) for ns in sorted(COMMANDS.get(name, ()))),
        *ASKS.get(name, ()),
    ]
    subscribe = [
        namespace_commands(name), status_subject(name),
        f"dida.discover.{name}", f"dida.{name}.ctl", f"_INBOX.{name}.>",
        *HEARS.get(name, ()),
    ]
    return {"publish": {"allow": publish}, "subscribe": {"allow": subscribe},
            "allow_responses": True}


def nats_users(secret: str) -> str:
    """The NATS server's user list (docker/nats.conf includes it)."""
    users = [{"user": CORE, "password": bus_password(secret, CORE),
              "permissions": {"publish": {"allow": [">"]}, "subscribe": {"allow": [">"]}}}]
    users += [{"user": name, "password": bus_password(secret, name), "permissions": permissions(name)}
              for name in sorted(BUS_CLIENTS)]
    return "authorization " + json.dumps({"users": users}, indent=2) + "\n"


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(mode=0o700, exist_ok=True)
    os.chown(path.parent, _OWNER, _OWNER)
    # Replaced whole, never rewritten in place: the NATS server reloads the user
    # list when it changes, and must never read half of one.
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.write_text(value)
    os.chown(tmp, _OWNER, _OWNER)
    tmp.chmod(0o400)
    tmp.replace(path)


def write_keys(secret: str, out: Path) -> None:
    for name in sorted(CLIENTS):
        _write(out / name / "key", adapter_key(secret, name))
    for name in sorted(BUS_CLIENTS | {CORE}):
        _write(out / name / "nats", bus_password(secret, name))
    _write(out / "nats-server" / "users.conf", nats_users(secret))
    for role in ROLES:
        _write(out / f"db-{role}" / "password", db_password(secret, role))


async def _provision(secret: str) -> None:
    pool = await pg_pool()
    try:
        async with pool.acquire() as conn:
            await provision(conn, secret, os.environ.get("POSTGRES_DB", "dida"))
    finally:
        await pool.close()


def main() -> None:
    secret = os.environ.get("DIDA_SECRET_KEY", "").strip()
    write_keys(secret, Path(os.environ.get("DIDA_KEYS_OUT", "/keys")))
    asyncio.run(_provision(secret))


if __name__ == "__main__":
    main()
