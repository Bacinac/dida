"""DIDA Ecowitt adapter.

Reads an Ecowitt gateway (GW1100/GW2000…) over its LOCAL JSON API
(`/get_livedata_info`) by polling — read-only, so it coexists with whatever
else the gateway already pushes to (e.g. a Home Assistant webhook): we never
touch the gateway's upload config. The gateway's id-coded data points are
normalised to canonical capabilities here (the one place Ecowitt quirks live);
the engine stays protocol-blind.

Note: a console like the WS2900 may not expose `/get_livedata_info` — only true
gateways do. Hosts that don't answer are logged and retried, never fatal.
"""

from __future__ import annotations

from dida_adapter_ecowitt.adapter import EcowittAdapter

__all__ = ["EcowittAdapter"]
