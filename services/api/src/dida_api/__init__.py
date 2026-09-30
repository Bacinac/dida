"""DIDA API — REST snapshots from Postgres + live state over WebSocket.

Read-only against `current_state` for snapshots; subscribes to the engine's
validated events stream and fans it out to connected browsers. Commands and
auth (argon2 + JWT, BABA scheme) land in Phase 1.
"""
