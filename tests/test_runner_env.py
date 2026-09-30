"""The runner edits the installation's own .env — the file that also holds its
database password and its tunnel settings. These pin the one thing it may touch:
the COMPOSE_PROFILES line, and nothing else in the file."""

from __future__ import annotations

from dida_runner.runner import Runner

ENV = """\
POSTGRES_PASSWORD=hunter2
COMPOSE_PROFILES=dlna,cast
# a comment about media
DIDA_MEDIA_BASE_URL=http://192.0.2.10:8093
"""


def _runner(tmp_path, text: str = ENV) -> Runner:
    (tmp_path / ".env").write_text(text)
    return Runner(tmp_path)


def test_reads_enabled_sorted_and_deduped(tmp_path):
    r = _runner(tmp_path, "COMPOSE_PROFILES= cast , dlna,cast,,\n")
    assert r._read_enabled() == ["cast", "dlna"]


def test_missing_line_reads_as_nothing_enabled(tmp_path):
    r = _runner(tmp_path, "POSTGRES_PASSWORD=hunter2\n")
    assert r._read_enabled() == []


def test_write_touches_only_its_own_line(tmp_path):
    r = _runner(tmp_path)
    r._write_enabled({"dlna", "cast", "zigbee"})
    lines = (tmp_path / ".env").read_text().splitlines()
    assert lines == [
        "POSTGRES_PASSWORD=hunter2",
        "COMPOSE_PROFILES=cast,dlna,zigbee",
        "# a comment about media",
        "DIDA_MEDIA_BASE_URL=http://192.0.2.10:8093",
    ]


def test_write_appends_when_the_line_is_absent(tmp_path):
    r = _runner(tmp_path, "POSTGRES_PASSWORD=hunter2\n")
    r._write_enabled({"mqtt"})
    assert (tmp_path / ".env").read_text() == "POSTGRES_PASSWORD=hunter2\nCOMPOSE_PROFILES=mqtt\n"


def test_write_leaves_no_temp_file_behind(tmp_path):
    r = _runner(tmp_path)
    r._write_enabled(set())
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]
    # An installation with nothing switched on writes an empty value, not a stale set.
    assert "COMPOSE_PROFILES=\n" in (tmp_path / ".env").read_text()


def test_write_keeps_owner_and_mode(tmp_path):
    r = _runner(tmp_path)
    env = tmp_path / ".env"
    env.chmod(0o600)
    before = env.stat()
    r._write_enabled({"mqtt"})
    after = env.stat()
    # The runner runs as root; the installation's own file must not change hands.
    assert (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid)
    assert after.st_mode & 0o7777 == 0o600


def test_round_trip(tmp_path):
    r = _runner(tmp_path)
    r._write_enabled({*r._read_enabled(), "matter"})
    assert r._read_enabled() == ["cast", "dlna", "matter"]


# --- several seed lines at once ------------------------------------------------


def _env(tmp_path, extra=""):
    (tmp_path / ".env").write_text(
        "COMPOSE_PROFILES=zigbee\nDIDA_STATE_HOST=/x\n" + extra)
    return Runner(tmp_path)


def test_writing_several_vars_touches_only_those_lines(tmp_path):
    r = _env(tmp_path, "DIDA_A=one\nOTHER=keep\n")
    r._write_env_vars({
        "DIDA_A": "",
        "DIDA_B": "",
        "DIDA_C": "",
    })
    text = (tmp_path / ".env").read_text()
    assert "OTHER=keep" in text and "COMPOSE_PROFILES=zigbee" in text
    assert "DIDA_A=\n" in text
    assert "DIDA_C=\n" in text, "an absent line is appended, not dropped"


class _Msg:
    def __init__(self, data: bytes) -> None:
        self.data, self.reply = data, None


async def test_tunnel_subject_rolls_only_the_connectors(tmp_path):
    from dida_runner.runner import TUNNEL_CONNECTORS

    r = _runner(tmp_path)
    rolled: list[list[str]] = []

    async def restart(services):
        rolled.append(services)
        return {"ok": True}

    r.restart = restart
    await r._on_tunnel(_Msg(b'{"action": "roll", "services": ["api", "postgres"]}'))
    assert rolled == [TUNNEL_CONNECTORS]


async def test_tunnel_subject_refuses_anything_else(tmp_path):
    r = _runner(tmp_path)
    called = []

    async def restart(services):
        called.append(services)
        return {"ok": True}

    r.restart = restart
    for action in ("restart", "disable", "state"):
        await r._on_tunnel(_Msg(f'{{"action": "{action}", "services": ["api"]}}'.encode()))
    assert called == []
