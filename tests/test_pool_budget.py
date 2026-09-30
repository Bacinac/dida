"""Every Postgres pool together fits in the server's connections.

Each service and adapter is its own process with its own pool, so the sum is what
Postgres sees at once. It had reached 106 against a limit of 100: the last processes
to start would find the server full, a failure that shows only on a cold boot of the
whole stack. Every `pg_pool()` call site counts once — a process per site — at its
declared `max_size`.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POSTGRES_DEFAULT_MAX = 100
SUPERUSER_RESERVED = 3
# pg_dump before an upgrade, the scheduled backup, a restore and one psql session.
MAINTENANCE = 4


def _default_max_size() -> int:
    tree = ast.parse((ROOT / "core/src/dida_core/db.py").read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "pg_pool")
    return next(d.value for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True)
                if a.arg == "max_size")


def _pools() -> list[tuple[str, int]]:
    default = _default_max_size()
    found = []
    for top in ("core", "services", "adapters", "scripts"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if "tests" in path.parts or ".venv" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "pg_pool"):
                    continue
                size = next((k.value for k in node.keywords if k.arg == "max_size"), None)
                assert size is None or isinstance(size, ast.Constant), \
                    f"{path.relative_to(ROOT)}:{node.lineno}: max_size must be a literal to be counted"
                found.append((f"{path.relative_to(ROOT)}:{node.lineno}", default if size is None else size.value))
    return found


def _max_connections() -> int:
    m = re.search(r"max_connections=(\d+)", (ROOT / "docker-compose.yml").read_text())
    return int(m.group(1)) if m else POSTGRES_DEFAULT_MAX


def test_the_pools_fit_in_postgres():
    pools = _pools()
    total = sum(size for _, size in pools) + MAINTENANCE
    room = _max_connections() - SUPERUSER_RESERVED
    assert total <= room, (
        f"{total} connections wanted, {room} available; the biggest: "
        + ", ".join(f"{w}={s}" for w, s in sorted(pools, key=lambda p: -p[1])[:6]))


def test_the_scan_sees_every_service():
    where = {w.split(":")[0] for w, _ in _pools()}
    assert {"services/engine/src/dida_engine/__main__.py", "services/api/src/dida_api/app.py",
            "services/automation/src/dida_automation/__main__.py"} <= where


def test_no_adapter_opens_a_pool():
    """An adapter reaches the database only through the api's broker."""
    assert not [w for w, _ in _pools() if w.startswith("adapters/")]


def test_the_matter_bridge_has_no_database_driver():
    """The bridge reads through the broker; a driver back in its dependencies is a
    pool this scan cannot see."""
    pkg = json.loads((ROOT / "services/matter-bridge/package.json").read_text())
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert not {"pg", "postgres", "@types/pg"} & set(deps)
