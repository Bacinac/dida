"""DIDA core — everything TWO OR MORE services must agree on.

That is the whole admission rule, and it is worth stating plainly because the
old wording ("the canonical capability model, the wire types, the bus, the
adapter protocol, the registry") listed the contents instead of the criterion —
which made two legitimate members, `heating` and `automations`, look like they
had wandered in. They had not: each is a typed model plus PURE decisions that the
api validates on write and the automation service executes on load. One
definition or the two drift, and there is no third home for it that would not
simply be this package under another name.

What the rule admits:
  * shared CONTRACTS — capabilities, wire/event types, the automation and heating
    models. Pure: types and functions over explicit inputs, never I/O.
  * shared PLUMBING — the bus, the adapter protocol and runner,
    db/migrations, health, logging. Infrastructure any service could need; a
    single consumer today is fine (ch_migrations has one, by design — the engine
    is the sole ClickHouse DDL writer).

What it does NOT admit, and the failure to watch for: a feature model only ONE
service uses. That is the step that turns a shared layer into a junk drawer, and
it is the reason the criterion is written down rather than inferred.

Core depends on NOTHING above it — no service, no adapter. tests/test_layering.py
holds that line, because it is the property everything else here rests on.
"""

from __future__ import annotations

from dida_core.adapter import Adapter, CommandRejected
from dida_core.adapter_config import (
    ADAPTER_CONFIG,
    AdapterConfig,
    ConfigDecryptError,
    ConfigField,
    decrypt_secret,
    encrypt_secret,
    fields_for,
    house_timezone,
    local_str,
)
from dida_core.adapter_runner import run_adapter
from dida_core.automations import (
    Action,
    AutomationDef,
    Condition,
    Trigger,
    compare,
    eval_condition,
    trigger_matches,
    validate_definition,
)
from dida_core.bus import Bus
from dida_core.capabilities import (
    CAPABILITIES,
    CHOICE_CAPABILITIES,
    DEVICE_TYPES,
    OPTION_CAPABILITIES,
    Access,
    CapabilityError,
    CapabilityKind,
    CapabilitySpec,
    ValueType,
    classify_device_type,
    command_vocabulary,
    readable_capabilities,
    resolve_device_type,
    setter_command,
    validate_command,
    validate_command_args,
    validate_runtime_options,
    validate_state,
)
from dida_core.ch import ch_client
from dida_core.ch_migrations import apply_ch_migrations
from dida_core.commands import prepare_command
from dida_core.crypto import derive_fernet
from dida_core.day_rollup import apply_house_day
from dida_core.db import (
    app_setting,
    clear_app_setting,
    ensure_app_setting,
    host_setting,
    jsonb_init,
    pg_pool,
    seed_app_setting,
    set_app_setting,
)
from dida_core.discovery import (
    answer_discover,
    local_ip,
    mdns_browse,
    probe_hosts,
    ssdp_msearch,
    subnet_hosts,
)
from dida_core.events import (
    CORE,
    ENGINE_EVENTS_SUBJECT,
    Command,
    EntityInfo,
    JournalEvent,
    LogRecord,
    ReachabilityEvent,
    StateUpdate,
    command_subject,
    entity_subject,
    state_subject,
)
from dida_core.geo import AWAY, MAX_ACCURACY_M, haversine_m, resolve_zone
from dida_core.health import StatusReporter, status_subject
from dida_core.ids import slug
from dida_core.journal import emit_journal
from dida_core.logbus import NatsLogHandler, attach_log_bus
from dida_core.logsetup import setup_logging
from dida_core.media import CODEC, icy_stream_title, opus_base, radio_probe, radio_stations
from dida_core.migrations import MigrationIntegrityError, apply_migrations
from dida_core.presence import PresenceReconciler, fetch_presence_locations
from dida_core.reachability import forget_reachable, set_reachable
from dida_core.references import (
    AREA_REFERENCES,
    ENTITY_KEYS,
    SETTING_REFERENCES,
    STARLARK_ENTITY_FUNCS,
    WALKED_COLUMNS,
    entity_references,
    path_references,
    rewrite_paths,
    rewrite_references,
    rewrite_setting,
    rewrite_starlark,
    setting_references,
    starlark_references,
)
from dida_core.retention import apply_retention
from dida_core.runtime import run_service
from dida_core.tasks import FailureGate

__all__ = [
    "ADAPTER_CONFIG",
    "AREA_REFERENCES",
    "AWAY",
    "CAPABILITIES",
    "CHOICE_CAPABILITIES",
    "CODEC",
    "CORE",
    "DEVICE_TYPES",
    "ENGINE_EVENTS_SUBJECT",
    "ENTITY_KEYS",
    "MAX_ACCURACY_M",
    "OPTION_CAPABILITIES",
    "SETTING_REFERENCES",
    "STARLARK_ENTITY_FUNCS",
    "WALKED_COLUMNS",
    "Access",
    "Action",
    "Adapter",
    "AdapterConfig",
    "AutomationDef",
    "Bus",
    "CapabilityError",
    "CapabilityKind",
    "CapabilitySpec",
    "Command",
    "CommandRejected",
    "Condition",
    "ConfigDecryptError",
    "ConfigField",
    "EntityInfo",
    "FailureGate",
    "JournalEvent",
    "LogRecord",
    "MigrationIntegrityError",
    "NatsLogHandler",
    "PresenceReconciler",
    "ReachabilityEvent",
    "StateUpdate",
    "StatusReporter",
    "Trigger",
    "ValueType",
    "answer_discover",
    "app_setting",
    "apply_ch_migrations",
    "apply_house_day",
    "apply_migrations",
    "apply_retention",
    "attach_log_bus",
    "ch_client",
    "classify_device_type",
    "clear_app_setting",
    "command_subject",
    "command_vocabulary",
    "compare",
    "decrypt_secret",
    "derive_fernet",
    "emit_journal",
    "encrypt_secret",
    "ensure_app_setting",
    "entity_references",
    "entity_subject",
    "eval_condition",
    "fetch_presence_locations",
    "fields_for",
    "forget_reachable",
    "haversine_m",
    "host_setting",
    "house_timezone",
    "icy_stream_title",
    "jsonb_init",
    "local_ip",
    "local_str",
    "mdns_browse",
    "opus_base",
    "path_references",
    "pg_pool",
    "prepare_command",
    "probe_hosts",
    "radio_probe",
    "radio_stations",
    "readable_capabilities",
    "resolve_device_type",
    "resolve_zone",
    "rewrite_paths",
    "rewrite_references",
    "rewrite_setting",
    "rewrite_starlark",
    "run_adapter",
    "run_service",
    "seed_app_setting",
    "set_app_setting",
    "set_reachable",
    "setter_command",
    "setting_references",
    "setup_logging",
    "slug",
    "ssdp_msearch",
    "starlark_references",
    "state_subject",
    "status_subject",
    "subnet_hosts",
    "trigger_matches",
    "validate_command",
    "validate_command_args",
    "validate_definition",
    "validate_runtime_options",
    "validate_state",
]
