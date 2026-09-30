"""DIDA govee adapter.

Govee has no clean local protocol that covers all of its devices, so instead of
reimplementing a flaky vendor API, DIDA runs Wez Furlong's mature `govee2mqtt`
bridge as a sibling container (`dida-govee2mqtt`). The bridge talks to Govee over
the account/AWS-IoT path (realtime, no LAN needed), plus optional HTTP API key and
LAN, and republishes every device as Home Assistant MQTT Discovery onto the DIDA
mosquitto — where the generic `ha` adapter adopts it. No Home Assistant, no
external box: the whole Govee path lives inside the DIDA stack.

This adapter owns the whole Govee path in one `govee:` namespace — no Home
Assistant involved:

1. It manages the bridge: the Settings → Adapters form (email / password / API
   key) is written to the bridge's credential env and the container is restarted
   over the Docker socket (the same mechanism netmgr uses).
2. It consumes the HA MQTT Discovery the bridge publishes on our mosquitto and
   maps every Govee device to canonical capabilities as `govee:<id>` entities;
   DIDA commands become MQTT publishes back to the bridge.
"""

from __future__ import annotations

from dida_adapter_govee.adapter import GoveeAdapter

__all__ = ["GoveeAdapter"]
