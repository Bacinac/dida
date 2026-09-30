"""Every app_settings key the code touches is declared in SETTING_REFERENCES.

The core writers refuse an undeclared key at runtime; this finds one before it
ships. A setting that names an entity and is missing from the registry is exactly
the reference a device rename used to leave behind."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from dida_core.references import (
    AREA_REFERENCES,
    SETTING_REFERENCES,
    path_references,
    rewrite_paths,
    rewrite_setting,
    setting_references,
)

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("core", "services", "adapters", "scripts")
ACCESSORS = {"app_setting", "cached_app_setting", "host_setting", "set_app_setting",
             "ensure_app_setting", "seed_app_setting", "clear_app_setting"}
_SQL_KEY = re.compile(
    r"app_settings\b.*?(?:key\s*(?:=|IN\s*\()\s*|VALUES\s*\(\s*)'([a-z0-9_]+)'",
    re.IGNORECASE | re.DOTALL)


def _python_files():
    for top in SOURCES:
        for path in (ROOT / top).rglob("*.py"):
            if "tests" not in path.parts and ".venv" not in path.parts:
                yield path


def _name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _loop_literals(tree: ast.AST) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.For) and isinstance(node.target, ast.Name)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            found.setdefault(node.target.id, []).extend(
                e.value for e in node.iter.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str))
    return found


def _keys_in(path: Path) -> set[tuple[str, str]]:
    text = path.read_text()
    tree = ast.parse(text)
    constants = {
        t.id: node.value.value
        for node in tree.body if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for t in node.targets if isinstance(t, ast.Name)
    }
    loops = _loop_literals(tree)
    keys: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _name(node.func) in ACCESSORS
                and len(node.args) >= 2):
            continue
        arg = node.args[1]
        where = f"{path.relative_to(ROOT)}:{node.lineno}"
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            keys.add((arg.value, where))
        elif isinstance(arg, ast.Name) and arg.id in constants:
            keys.add((constants[arg.id], where))
        elif isinstance(arg, ast.Name) and arg.id in loops:
            keys.update((k, where) for k in loops[arg.id])
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            keys.update((k, f"{path.relative_to(ROOT)}:{node.lineno}")
                        for k in _SQL_KEY.findall(node.value))
    return keys


def _sql_keys() -> set[tuple[str, str]]:
    keys = set()
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        for stmt in path.read_text().split(";"):
            if re.search(r"\b(INSERT\s+INTO|UPDATE)\s+app_settings\b", stmt, re.IGNORECASE):
                keys.update((k, path.name) for k in re.findall(r"\(\s*'([a-z0-9_]+)'", stmt))
    return keys


def test_every_setting_key_in_the_code_is_declared():
    used = set().union(*(_keys_in(p) for p in _python_files()), _sql_keys())
    undeclared = sorted(f"{k} ({where})" for k, where in used if k not in SETTING_REFERENCES)
    assert not undeclared, "declare these in SETTING_REFERENCES: " + ", ".join(undeclared)


def test_the_scan_sees_the_known_writers():
    used = {k for p in _python_files() for k, _ in _keys_in(p)}
    assert {"heating", "entry_controls", "energy_config", "lan_ip", "contacts_last_sync",
            "fcm_service_account", "anthropic_api_key"} <= used


def test_seeded_keys_are_declared():
    from dida_api.seed import PLAIN, SECRET

    assert not (set(PLAIN) | set(SECRET)) - SETTING_REFERENCES.keys()


def test_setting_paths_find_and_rewrite_ids():
    mapping = {"tuya:old_lock": "tuya:new_lock", "iammeter:old": "iammeter:new"}
    entry = '{"car": "virtual:car_gate", "door": "tuya:old_lock", "notify_on_open": true}'
    assert setting_references("entry_controls", entry) == {"virtual:car_gate", "tuya:old_lock"}
    assert '"door": "tuya:new_lock"' in rewrite_setting("entry_controls", entry, mapping)

    energy = '{"iammeter:old": "grid_import"}'
    assert setting_references("energy_config", energy) == {"iammeter:old"}
    assert rewrite_setting("energy_config", energy, mapping) == '{"iammeter:new": "grid_import"}'

    assert setting_references("radio_player", "tuya:old_lock") == {"tuya:old_lock"}
    assert rewrite_setting("radio_player", "tuya:old_lock", mapping) == "tuya:new_lock"
    assert setting_references("lan_ip", "192.168.1.100") == set()


def test_area_paths_reach_every_field_that_names_an_entity():
    media = {"sources": [{"key": "music", "player": "opus:dac", "zone": "denon:zone2",
                          "avr": "denon:avr", "remote": "harmony:hub", "nowplaying": "virtual:np"}]}
    assert path_references(media, AREA_REFERENCES["media_config"]) == \
        {"opus:dac", "denon:zone2", "denon:avr", "harmony:hub", "virtual:np"}
    heating = {"sensor": "zigbee:t", "valves": ["zigbee:v1", "zigbee:v2"], "offset": -2.0}
    assert path_references(heating, AREA_REFERENCES["heating_config"]) == \
        {"zigbee:t", "zigbee:v1", "zigbee:v2"}
    sensors = {"excluded": ["ecowitt:ws:indoor:temperature"], "hidden": ["humidity"]}
    assert path_references(sensors, AREA_REFERENCES["sensor_config"]) == {"ecowitt:ws:indoor"}
    moved = rewrite_paths(sensors, AREA_REFERENCES["sensor_config"],
                          {"ecowitt:ws:indoor": "ecowitt:ws2:indoor"})
    assert moved == {"excluded": ["ecowitt:ws2:indoor:temperature"], "hidden": ["humidity"]}
