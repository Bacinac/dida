"""The upgrade path's two load-bearing claims, checked against the real migrations.

`deploy/upgrade.sh` restores the PREVIOUS images when health fails. That is only
safe while migrations are additive — old code ignores a new column. Across a
migration that drops a column or deletes rows it is the opposite of safe: the old
code asks for something that no longer exists, so the rollback turns one broken
deploy into two, at the worst possible moment.

The script used to simply ASSERT the safe case in a comment ("DIDA's migrations are
additive"). Five committed migrations disprove it. It now decides from the actual
diff, and this suite is what keeps that decision honest: the pattern is read OUT of
the script, so a weakened pattern fails here rather than on the night it matters.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
UPGRADE = ROOT / "deploy" / "upgrade.sh"
PROD = ROOT / "deploy" / "prod.sh"

def _code(src: str) -> str:
    """The script with its comments stripped.

    Anchoring on a bare substring is a trap this suite fell into itself: the
    comments EXPLAIN the wrong forms (`pg_restore --list`, `/dev/stdin`) and named
    them, so a check for their absence failed against a correct script. What must
    be absent is the COMMAND, not the discussion of it."""
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


# Migrations known to remove something an older image would still ask for.
KNOWN_DESTRUCTIVE = {
    "0003_consolidate_self_heal.sql",
    "0010_canonical_device_type.sql",
    "0018_schedules_calendar_only.sql",
    "0037_computed_helper_definition.sql",
    "0065_drop_cloudflare_routes.sql",
}


def _pattern() -> str:
    """The ERE the script actually greps with — not a copy of it."""
    src = UPGRADE.read_text()
    m = re.search(r"grep -qiE '([^']+)'", src)
    assert m, "upgrade.sh no longer greps migrations for destructive statements"
    return m.group(1)


def _is_destructive(path: pathlib.Path) -> bool:
    return subprocess.run(
        ["grep", "-qiE", _pattern(), str(path)], check=False
    ).returncode == 0


def test_every_known_destructive_migration_is_detected():
    """Miss one and the rollback happily puts old code on a schema without it."""
    missed = [n for n in sorted(KNOWN_DESTRUCTIVE) if not _is_destructive(ROOT / "db/migrations" / n)]
    assert not missed, f"destructive migrations the guard would wave through: {missed}"


def test_an_additive_migration_is_not_flagged():
    """A guard that flags everything disables rollback permanently, which is the
    same as not having one."""
    additive = ROOT / "db/migrations/0001_init.sql"
    assert additive.exists()
    assert not _is_destructive(additive)


def test_the_guard_does_not_flag_the_whole_migration_set():
    """Sanity on the ratio: if most files matched, the pattern is too loose."""
    files = sorted((ROOT / "db/migrations").glob("*.sql"))
    assert len(files) > 40, "migration set unexpectedly small — is the path right?"
    flagged = [f.name for f in files if _is_destructive(f)]
    assert set(flagged) >= KNOWN_DESTRUCTIVE
    assert len(flagged) < len(files) / 3, f"pattern too loose: {len(flagged)}/{len(files)} flagged"


def test_the_false_additive_claim_is_gone():
    """The comment that made the old rollback look safe."""
    for script in (UPGRADE, PROD):
        assert "migrations are forward-only, but dida's are additive" not in script.read_text().lower()


def test_the_backup_happens_before_anything_can_change():
    """Order is the whole point: a dump taken after the build is a dump of a schema
    the migrations may already have rewritten."""
    src = UPGRADE.read_text()
    # Anchor on the COMMAND, not the word: `pg_dump` is named in the comments above
    # it, and anchoring there let a version with the dump moved after the build pass.
    dump = src.index("docker compose exec -T postgres pg_dump")
    for later in ("git reset --hard", "docker compose --profile bases build", "docker compose up -d"):
        assert dump < src.index(later), f"the dump must be taken before `{later}`"


def test_a_failed_backup_stops_the_upgrade():
    """Logged-and-continued is how installations lose their only way back."""
    src = UPGRADE.read_text()
    assert "refusing to upgrade without a backup" in src
    block = src[src.index("== backing up"):src.index("== fetching")]
    assert block.count("exit 1") >= 2, "both the dump failure and the verify failure must exit"


def test_the_dump_is_verified_by_being_read_whole():
    """A file of the right shape is not proof it can be restored.

    `pg_restore --list` is the tempting check and it is too weak: a custom-format
    archive keeps its table of contents near the front, so a dump truncated halfway
    through the data lists perfectly (measured: rc=0). Only a full parse notices.
    This asserts the strong form is used and the weak one is not silently restored."""
    code = _code(UPGRADE.read_text())
    assert "pg_restore -f /dev/null" in code
    assert "pg_restore --list" not in code, "--list passes a truncated dump"


def test_the_dump_is_piped_not_named_as_a_path():
    """`pg_restore /dev/stdin` cannot seek and rejects a perfectly good archive —
    it would have aborted every upgrade at the backup step."""
    src = UPGRADE.read_text()
    block = _code(src[src.index("== backing up"):src.index("== fetching")])
    assert "/dev/stdin" not in block


def test_the_backup_size_is_reported_as_the_real_size():
    """`du -h` reports allocated blocks: on the NFS share the dumps live on, a
    freshly written 3 MB dump reports "512". The run is fine and the log reads like
    a 512-byte backup — which is how you learn to ignore the line that matters."""
    code = _code(UPGRADE.read_text())
    assert "du -h" not in code
    assert 'ls -lh "$DUMP"' in code


def test_the_destructive_branch_refuses_instead_of_restoring_images():
    src = UPGRADE.read_text()
    branch = src[src.index("== NOT rolling back"):]
    assert "docker tag" not in branch.split("exit 1")[0], \
        "the destructive branch must not restore images"


def test_no_container_is_signalled_through_docker_kill():
    """`docker kill` of ANY signal, a reload's HUP included, marks the container
    manually stopped, so `unless-stopped` leaves it down after the next reboot: the
    bus stayed down after mlaka's weekly reboot on 2026-09-27 and took the house
    with it. A reload is signalled from inside the container."""
    code = _code(UPGRADE.read_text())
    assert not re.search(r"docker (compose )?kill", code), \
        "signal from inside the container (docker compose exec … pkill), not docker kill"


def test_prod_and_operator_upgrade_are_the_same_script():
    """Two copies of the upgrade would let the operator's path rot unnoticed."""
    prod = PROD.read_text()
    assert "upgrade.sh" in prod
    assert "docker compose --profile bases build base" not in prod, \
        "prod.sh must not carry its own copy of the build sequence"


# --- the slow-link build -------------------------------------------------------


def test_the_build_is_retried():
    """The one failure this upgrade path had left open, and it happened for real.

    The remote installation has a 0.79 Mbit/s uplink. On 2026-08-08 its upgrade died
    with "Failed to download asyncpg==0.31.0" — one 3.3 MiB wheel — and took the
    whole `compose build` with it, leaving an operator to re-run by hand after the
    tree had already moved. Docker's layer cache makes a retry both cheap and
    progressive, so each attempt starts further along."""
    code = _code(UPGRADE.read_text())
    assert "build_retry" in code
    for cmd in ("docker compose --profile bases build base",
                'docker compose --profile "*" build'):
        assert f"build_retry {cmd}" in code, f"`{cmd}` is not retried"


def test_the_retry_is_BOUNDED():
    """An unbounded loop over a link that is genuinely down is a hang, not a retry —
    and a hang is the one failure mode this whole script exists to avoid."""
    src = UPGRADE.read_text()
    block = src[src.index("build_retry() {"):src.index("echo \"== building base ==\"")]
    assert "-ge 3" in block, "the retry has no attempt ceiling"
    assert "return 1" in block, "exhausting the retries must fail, not fall through"


def test_uv_is_given_a_timeout_that_fits_the_slow_link():
    """uv's 30 s default is a bandwidth assumption in disguise: a 3.3 MiB wheel needs
    ~35 s at 0.79 Mbit/s. Set in BOTH bases — planvision builds on its own and
    inherits nothing."""
    for path in (ROOT / "docker/base.Dockerfile",
                 ROOT / "services/planvision/Dockerfile"):
        text = path.read_text()
        assert "UV_HTTP_TIMEOUT" in text, f"{path.name} has no download timeout"
        value = int(re.search(r"UV_HTTP_TIMEOUT=(\d+)", text).group(1))
        assert value >= 120, f"{path.name}: {value}s is still a fast-link assumption"


# --- the external CI must build exactly what the gate requires -------------------
#
# `tests/run.sh` fails loud when a REQUIRED image is missing, which is the right
# behaviour and the reason the gate can be trusted. But CI kept its own hand-written
# copy of that list, and the copy drifted: eight images the gate requires were never
# built (announce, broadlink, ecowitt, frigate, govee, homekit, notify, netmgr) while
# `runner` was built for nothing. CI is workflow_dispatch, so the preflight that
# would have said so had not been run since. The list is now DERIVED; this keeps the
# derivation honest — a REQUIRED name whose Dockerfile does not sit where the mapping
# expects fails here rather than in a CI run nobody triggers.

CI_YML = ROOT / ".github/workflows/ci.yml"


def _required_images() -> list[str]:
    src = UPGRADE.parent.parent.joinpath("tests/run.sh").read_text()
    block = re.search(r'^REQUIRED="(.*?)"$', src, re.S | re.M)
    assert block, "tests/run.sh no longer declares REQUIRED"
    return [n for n in block.group(1).replace("\\", " ").split() if n]


def _dockerfile_for(name: str) -> pathlib.Path:
    if name.startswith("adapter-"):
        return ROOT / "adapters" / name[len("adapter-"):] / "Dockerfile"
    return ROOT / "services" / name / "Dockerfile"


def test_every_required_image_has_a_dockerfile_where_the_mapping_looks():
    missing = [n for n in _required_images()
               if n != "base" and not _dockerfile_for(n).is_file()]
    assert not missing, f"CI's derivation would fail to build: {missing}"


def test_ci_derives_the_build_list_instead_of_repeating_it():
    """A second copy of the list is the thing that drifted. If one reappears, this
    fails — the point is that there is nothing left to keep in sync."""
    ci = CI_YML.read_text()
    assert 'sed -n \'/^REQUIRED="/,/"$/p\' tests/run.sh' in ci, \
        "CI no longer derives the image list from the gate's REQUIRED set"
    for name in _required_images():
        if name == "base":
            continue
        assert f"{name}:services/" not in ci and f"{name}:adapters/" not in ci, \
            f"{name} is hard-coded in CI again"


def test_ci_still_builds_the_base_image_itself():
    """The derivation skips `base` because it is built from docker/base.Dockerfile in
    its own step — skipping it in both places would be a silent no-build."""
    ci = CI_YML.read_text()
    assert "docker build -f docker/base.Dockerfile -t dida/base:latest ." in ci


def test_ci_fails_loudly_when_a_dockerfile_is_absent():
    """Silently skipping an image would put the failure back where it was: a green
    CI run over images that were never built."""
    ci = CI_YML.read_text()
    assert 'no Dockerfile for dida/$name' in ci
    assert "exit 1" in ci
