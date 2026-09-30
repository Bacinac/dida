"""DIDA presence adapter.

Turns GPS reports from a phone tracker (OwnTracks) into first-class presence
entities. OwnTracks publishes a person's position to MQTT under
`owntracks/<user>/<device>`; this adapter:

  * subscribes to that broker (DIDA's own mosquitto by default),
  * reads the zone definitions from the `zones` table (read-only reference
    data, refreshed periodically — same DB-as-device pattern as the virtual
    adapter),
  * resolves each position to the most specific containing zone (point in a
    centre+radius circle; "away" when outside every zone),
  * publishes `presence:<user>` onto the bus: `location` (the resolved zone
    name, the headline) plus `latitude` / `longitude` / `battery`
    (diagnostic — they drive the map marker).

So a person becomes an ordinary entity: validated at the boundary, rendered by
the capability-driven UI, usable in automations exactly like a device. The
adapter is read-only (presence has no commands) and fault-isolated: if it dies,
presence goes stale, the rest of the house is untouched, the runtime restarts it.

This is the HA-independent replacement for HA's person/device_tracker + zone
machinery. OwnTracks is the open standard; point its phones at DIDA's broker.
"""

from __future__ import annotations

from dida_adapter_presence.adapter import PresenceAdapter

__all__ = ["PresenceAdapter"]
