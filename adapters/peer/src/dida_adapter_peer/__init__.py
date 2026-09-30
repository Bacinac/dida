"""DIDA peer adapter — a second DIDA installation seen from this one.

Another house runs its own full DIDA: its own bus, database, automations and
autonomy when the link is down. This adapter mirrors a CURATED set of its
entities into this installation so the remote grid meter, climate and locks are
visible and controllable from home, without either side depending on the other
to keep working.

It talks to the peer over its ordinary HTTPS surface as a logged-in user — no
NATS is exposed, no new authentication scheme exists, and the remote can revoke
the link by disabling that one user.
"""

from __future__ import annotations

from dida_adapter_peer.adapter import PeerAdapter

__all__ = ["PeerAdapter"]
