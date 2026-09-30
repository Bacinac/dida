"""DIDA notify adapter.

Push notifications without Home Assistant, over two channels converging on the
same `notify:<who>` entities (plus `notify:all` broadcast):

- **ntfy**: POST to a per-person topic on ntfy.sh / self-hosted ntfy; targets
  (who -> topic) configured in Settings → Adapters (DB).
- **Web Push**: RFC 8030 push straight to the DIDA PWA on the family's phones —
  no third-party app. Subscriptions are stored per user by the API
  (push_subscriptions); the VAPID signing key lives in app_settings. Dead
  subscriptions (push service returns 404/410) are pruned automatically.

An automation just fires a `notify` command; it doesn't know or care which
channel reaches the phone. The adapter idles until at least one target exists.
"""

from __future__ import annotations

from dida_adapter_notify.adapter import NotifyAdapter

__all__ = ["NotifyAdapter"]
