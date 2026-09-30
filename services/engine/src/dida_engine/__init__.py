"""DIDA state engine.

Subscribes to all device state updates, validates each against the canonical
capability spec at the bus boundary, projects accepted values into Postgres
`current_state`, and republishes them on the engine events stream for the api
and (Phase 1) the automation engine.

The validation step is the robustness keystone: a malformed or out-of-range
update is dropped (logged + counted), never entering state — a buggy adapter
cannot corrupt the system.
"""
