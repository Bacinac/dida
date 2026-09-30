"""Who holds the keys to the host and to the bus, read from docker-compose.yml.

The Docker API is root on the host, and the core bus login drives every device and
the runner. Both are handed out in the compose file, one service at a time, and
a copied service block carries them along silently. This pins the holders.
"""

from __future__ import annotations

import re
from pathlib import Path

from dida_core import db_roles
from dida_core.events import CORE
from dida_core.identity import ADAPTERS, SERVICES

COMPOSE = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
PRIVILEGED = {"engine", "automation", "journal", "runner", "api"}
NARROW_ROLES = {"netmgr": db_roles.NETMGR, "lanprobe": db_roles.LANPROBE}


def _services() -> dict[str, str]:
    body = COMPOSE.split("\nservices:\n", 1)[1]
    body = re.split(r"\n(?=[a-z])", body, maxsplit=1)[0]
    blocks: dict[str, str] = {}
    name = None
    for line in body.splitlines():
        m = re.match(r"^  ([a-z0-9-]+):\s*$", line)
        if m:
            name = m.group(1)
            blocks[name] = ""
        elif name:
            blocks[name] += line + "\n"
    return blocks


def _proxies() -> set[str]:
    return {s for s in _services() if s.startswith("docker-proxy-")}


def test_the_scan_sees_the_services():
    assert {"engine", "api", "runner", "docker-proxy-netmgr"} <= set(_services())
    assert len(_services()) > 40


def test_only_the_runner_and_the_filtering_proxies_hold_the_docker_socket():
    holders = {s for s, block in _services().items() if "/var/run/docker.sock" in block}
    assert holders == {"runner"} | _proxies()
    for proxy in _proxies():
        assert "/var/run/docker.sock:/var/run/docker.sock:ro" in _services()[proxy]


def test_the_proxies_are_off_every_network():
    anchor = COMPOSE.split("x-docker-proxy: &docker-proxy\n", 1)[1].split("\n\n", 1)[0]
    assert "network_mode: none" in anchor
    for proxy in _proxies():
        assert "<<: *docker-proxy" in _services()[proxy]
        assert "network_mode" not in _services()[proxy] and "networks:" not in _services()[proxy]


def test_each_filtered_socket_reaches_its_proxy_and_one_consumer():
    for proxy in _proxies():
        volume = f"docker_api_{proxy.removeprefix('docker-proxy-')}"
        mounted_by = {s for s, block in _services().items() if re.search(rf"- {volume}:", block)}
        assert proxy in mounted_by and len(mounted_by) == 2, f"{volume}: {sorted(mounted_by)}"


def test_only_the_core_services_take_the_core_bus_login():
    holders = {s for s, block in _services().items() if re.search(rf"subpath: {CORE}\n", block)}
    assert holders == PRIVILEGED


def test_every_bus_login_is_a_key_file_never_a_url():
    anchor = COMPOSE.split("x-dida-env: &dida-env\n", 1)[1].split("\n\n", 1)[0]
    assert "DIDA_NATS_PASSWORD_FILE: /run/dida-key/nats\n" in anchor
    assert not re.search(r"nats://[^\s/]*@", COMPOSE), "a bus password in a URL is one every reader of the compose file holds"
    keys = [p for p in re.findall(r"subpath: (\S+)", _services()["netmgr"]) if not p.startswith("db-")]
    assert keys == ["netmgr"], "netmgr logs in as another"


def test_the_bus_server_reads_the_logins_the_keys_service_wrote():
    nats = _services()["nats"]
    assert re.search(r"target: /etc/nats/auth\n\s+read_only: true\n\s+volume:\n\s+subpath: nats-server\n", nats)
    assert re.search(r"keys:\n\s+condition: service_completed_successfully", nats)
    assert "nats-run.sh" in nats, "without the watcher a new adapter's login waits for a server restart"


def _adapters() -> dict[str, str]:
    return {s.removeprefix("adapter-"): b for s, b in _services().items() if s.startswith("adapter-")}


def test_every_adapter_has_an_identity_and_nothing_else_does():
    root = Path(__file__).resolve().parent.parent
    on_disk = {p.name for p in (root / "adapters").iterdir() if (p / "Dockerfile").exists()}
    assert set(_adapters()) == on_disk == ADAPTERS, \
        "an adapter without a key cannot start; a key without an adapter is one more to leak"


def test_no_adapter_holds_the_database_or_the_root_key():
    """The root key signs every login and opens every stored secret; the database
    role owns every table. An adapter parses whatever a device sends it."""
    anchor = COMPOSE.split("x-dida-env: &dida-env\n", 1)[1].split("\n\n", 1)[0]
    assert "POSTGRES_" not in anchor and "DIDA_SECRET_KEY" not in anchor
    for name, block in _adapters().items():
        assert "POSTGRES_" not in block and "*dida-pg" not in block, f"adapter-{name} reaches the database"
        assert "DIDA_SECRET_KEY" not in block, f"adapter-{name} holds the root key"


def test_the_root_key_stays_with_the_api_and_the_keys_service():
    holders = {s for s, block in _services().items() if re.search(r"^\s+DIDA_SECRET_KEY:", block, re.M)}
    assert holders == {"api", "keys"}


def test_each_adapter_mounts_its_own_key_and_only_that():
    for name, block in _adapters().items():
        assert re.findall(r"subpath: (\S+)", block) == [name], f"adapter-{name} mounts another's key"
        assert "source: dida-keys" in block and "target: /run/dida-key" in block
        assert "read_only: true" in block
        assert re.search(r"keys:\n\s+condition: service_completed_successfully", block), \
            f"adapter-{name} may start before its key is written"
        assert re.search(r"api:\n\s+condition: service_healthy", block), \
            f"adapter-{name} may start before the broker that configures it"


def test_only_postgres_and_the_keys_service_hold_the_superuser():
    holders = {s for s, block in _services().items() if re.search(r"^\s+POSTGRES_PASSWORD:", block, re.M)}
    assert holders == {"postgres", "keys"}
    anchor = COMPOSE.split("x-dida-pg: &dida-pg\n", 1)[1].split("\n\n", 1)[0]
    assert f"POSTGRES_USER: {db_roles.APP}\n" in anchor
    assert "POSTGRES_PASSWORD_FILE: /run/dida-db/password\n" in anchor
    assert "image: dida/api:latest" in _services()["keys"], \
        "the gate provisions the roles from the api image; the base has no database driver"


def test_each_database_service_mounts_its_own_role_and_only_that():
    users = {s: b for s, b in _services().items() if "*dida-pg" in b}
    assert {"engine", "automation", "api", "netmgr", "lanprobe"} <= set(users)
    for name, block in users.items():
        role = NARROW_ROLES.get(name, db_roles.APP)
        if name in NARROW_ROLES:
            assert f"POSTGRES_USER: {role}\n" in block, f"{name} logs in as the app role"
        else:
            assert "POSTGRES_USER:" not in block, f"{name} overrides the app role"
        assert re.findall(r"subpath: (db-\S+)", block) == [f"db-{role}"], f"{name} mounts another role's password"
        assert "target: /run/dida-db" in block
        assert re.search(r"keys:\n\s+condition: service_completed_successfully", block), \
            f"{name} may start before its role exists"


def test_the_matter_bridge_is_a_broker_client_like_an_adapter():
    blocks = _services()
    for name in SERVICES:
        block = blocks[name]
        assert "POSTGRES_" not in block and "*dida-pg" not in block, f"{name} reaches the database"
        assert "DIDA_SECRET_KEY" not in block, f"{name} holds the root key"
        assert re.findall(r"subpath: (\S+)", block) == [name], f"{name} mounts another's key"
        assert "target: /run/dida-key" in block and "read_only: true" in block
        assert re.search(r"keys:\n\s+condition: service_completed_successfully", block)
        assert re.search(r"api:\n\s+condition: service_healthy", block)
