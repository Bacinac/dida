"""DIDA virtual adapter.

The DIDA-native replacement for Home Assistant's "helpers" (input_boolean /
input_select / input_number). A helper is NOT a special subsystem here — it is
an ordinary entity whose owner is DIDA itself. This adapter:

  * reads helper definitions from the `virtual_entities` table,
  * seeds each one's initial state onto the bus (so it lands in current_state,
    the single source of truth — never duplicated in the definition table),
  * turns commands (turn_on/toggle/set_option/set_value) into validated state
    updates, exactly like a device adapter turns commands into protocol.

So a virtual entity is first-class on the bus: validated at the boundary,
rendered by the capability-driven UI, usable in automations as trigger /
condition / action — identical to a real device, with DIDA as the "device".

Note: device *modes* (e.g. a heater's Off/L1/L2/L3) are NOT virtual entities —
they are the same `enum` capability living on the device entity itself.
"""

from __future__ import annotations

from dida_adapter_virtual.adapter import VirtualAdapter

__all__ = ["VirtualAdapter"]
