"""The login limits and the assistant's, over home_core's buckets.

Forwarding headers count from any private or loopback peer unless
DIDA_TRUSTED_PROXIES names the proxies: the API is only ever reached through an
internal hop — the UI's `/api` proxy on the LAN, cloudflared on the tunnel — so
an internal peer IS a proxy and a public one is not. That trusts a LAN host
too, which is why the login also has a per-account cap.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException
from home_core.rate_limit import LoginLimits, TokenBucketLimiter, TrustedPeers

log = logging.getLogger("dida.api.ratelimit")

TRUSTED_PROXIES = TrustedPeers(os.environ.get("DIDA_TRUSTED_PROXIES") or "private")

LOGIN = LoginLimits(TRUSTED_PROXIES)


# 10 turns burst, 10/min sustained, per USER. One assistant turn is the most
# expensive thing a non-admin can ask this API to do — a tool loop of up to
# MAX_TOOL_ROUNDS model calls, each of which may spawn a second model call to
# author a rule — and it is billed to a real account. Keyed by username, not IP:
# the endpoint is authenticated, the cost follows the account, and the same person
# on phone and laptop should share one budget. A conversation never approaches 10
# turns a minute; a runaway client or a bored teenager does.
ASSISTANT_LIMITER = TokenBucketLimiter(capacity=10, refill_per_s=10 / 60.0)


def enforce_assistant_limit(username: str) -> None:
    if not ASSISTANT_LIMITER.take(username):
        log.warning("rate limit: assistant turns exhausted for %s", username)
        raise HTTPException(
            status_code=429,
            detail="too many assistant requests; wait a few seconds",
            headers={"Retry-After": "10"},
        )
