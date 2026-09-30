"""One-off: mint a DIDA HomeKit controller pairing.

For an accessory already paired to another ADMIN controller (e.g. Home
Assistant), DIDA doesn't need a reset or the setup code — it adds itself as an
extra controller via the HAP `add_pairing` operation, authenticated with the
source admin pairing.

Input: DIDA_HK_SOURCE -> a JSON file mapping `alias -> source pairing_data`
(the admin controller's pairing, e.g. extracted from HA; carries the admin
secret and is used ONLY here). For each, DIDA generates its own ed25519 keypair,
registers it on the accessory, and stores its own pairing_data through the broker
(adapter_config, sealed). After this runs, delete the source file — DIDA
never needs the admin secret again.

    docker compose run --rm -v <dir>:/src \
        -e DIDA_HK_SOURCE=/src/hk_src.json adapter-homekit \
        python -m dida_adapter_homekit.pair
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from dida_core import Bus
from dida_core.broker import Broker

# Accessory identity (same for every controller); only the iOSDevice* differ.
_ACCESSORY_KEYS = (
    "AccessoryPairingID", "AccessoryLTPK", "AccessoryIP",
    "AccessoryIPs", "AccessoryPort", "Connection",
)


def _gen_keypair() -> tuple[str, str]:
    sk = ed25519.Ed25519PrivateKey.generate()
    ltsk = sk.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    ltpk = sk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return ltsk.hex(), ltpk.hex()


async def main() -> None:
    from aiohomekit import Controller
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

    src = json.loads(await asyncio.to_thread(Path(os.environ["DIDA_HK_SOURCE"]).read_text))
    bus = Bus(os.environ["DIDA_NATS_URL"], name="dida-homekit-pair", user="homekit")
    await bus.connect()
    broker = Broker(bus, "homekit")
    raw = await broker.call("stored", key="pairings")
    existing = json.loads(raw) if raw else {}

    zc = AsyncZeroconf()
    browser = AsyncServiceBrowser(
        zc.zeroconf, ["_hap._tcp.local.", "_hap._udp.local."], handlers=[lambda *a, **k: None]
    )
    controller = Controller(async_zeroconf_instance=zc)
    await controller.async_start()
    await asyncio.sleep(3)  # let the mDNS cache populate so the accessory resolves
    try:
        for alias, src_data in src.items():
            pairing = controller.load_pairing(f"_src_{alias}", dict(src_data))
            ltsk_hex, ltpk_hex = _gen_keypair()
            dida_id = str(uuid.uuid4())
            await pairing.add_pairing(dida_id, ltpk_hex, "User")
            dida_data = {k: src_data[k] for k in _ACCESSORY_KEYS if k in src_data}
            dida_data.update(
                {
                    "Connection": src_data.get("Connection", "IP"),
                    "iOSPairingId": dida_id,
                    "iOSDeviceLTSK": ltsk_hex,
                    "iOSDeviceLTPK": ltpk_hex,
                    "name": src_data.get("name") or alias,
                }
            )
            existing[alias] = dida_data
            print(f"added DIDA controller to '{alias}'")
            await pairing.close()
    finally:
        await browser.async_cancel()
        await controller.async_stop()
        await zc.async_close()

    await broker.call("store", key="pairings", value=json.dumps(existing))
    await bus.close()
    print(f"stored {len(existing)} pairing(s)")


if __name__ == "__main__":
    asyncio.run(main())
