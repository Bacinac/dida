"""DIDA SmartThings adapter.

Bridges Samsung appliances/AV (TV, soundbar, AC, washer/dryer, fridge, robot
vacuum) onto the DIDA bus via the SmartThings CLOUD — these devices expose no
local API, so the cloud is the only path (like `landroid`). Auth is OAuth 2.0:
SmartThings killed long-lived Personal Access Tokens (new PATs die in 24h), so
the only durable path is a self-registered OAuth client whose refresh token
*rolls* on every use — the adapter refreshes daily and keeps working forever.

The one-time "Connect" (authorization_code exchange) runs in the `api` service
(Settings → Adapters); it stores the encrypted token blob in `adapter_config`.
The adapter then owns the ongoing rolling refresh and persists each new token.

Each SmartThings device decomposes into DIDA capabilities at the boundary (its
own big capability taxonomy → our canonical one), the SAME validate→project→
persist path as any adapter, so a malformed reading is rejected, never corrupts
the core. Cloud failure stays contained here.
"""

from __future__ import annotations

from dida_adapter_smartthings.adapter import SmartThingsAdapter

__all__ = ["SmartThingsAdapter"]
