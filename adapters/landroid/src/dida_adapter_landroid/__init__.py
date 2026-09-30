"""DIDA Landroid adapter.

Bridges a Worx Landroid robotic mower onto the DIDA bus via the Worx cloud
(pyworxcloud) — there is no local protocol for Landroid, so this is one of the
few genuinely cloud-only adapters. Credentials (email/password) are configured
in Settings → Adapters (DB). The mower decomposes into a
`mower` capability (state + start/pause/dock) plus `battery`; the engine stays
device-blind. Cloud failure stays contained here, like any adapter.
"""

from __future__ import annotations

from dida_adapter_landroid.adapter import LandroidAdapter

__all__ = ["LandroidAdapter"]
