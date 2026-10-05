import asyncio
import json

from dida_netmgr import manager


async def main():
    manager.DOCKER_SOCK = "/socket/docker.sock"
    m = manager.NetManager(None)
    status, body = await m._docker("POST", "/networks/create", {"Name": "dida-control", "Driver": "bridge"})
    assert status == 201, body
    status, body = await m._docker("POST", "/containers/create?name=dida-netmgr", {
        "Image": "alpine:3", "Cmd": ["sleep", "600"], "HostConfig": {"NetworkMode": "dida-control"},
    })
    assert status == 201, body
    status, body = await m._docker("POST", "/containers/dida-netmgr/start")
    assert status == 204, body
    parent = "eth0"

    async def parent_nic(_):
        return parent

    async def desired():
        return {20}

    async def config():
        return {"20": {"mac": "02:00:00:00:00:20"}}

    async def noop(*_):
        return None

    async def interfaces():
        return set()

    async def iface(_):
        return ""

    manager.parent_nic = parent_nic
    m._desired, m._vlan_config = desired, config
    m._iface_names, m._iface_for = interfaces, iface
    m._ensure_dhcp, m._publish_status = noop, noop
    await m.reconcile()
    original = await m._network(20)
    assert original["Options"]["parent"] == "eth0.20"
    parent = "eth1"
    await m.reconcile()
    changed = await m._network(20)
    assert changed["Id"] != original["Id"]
    assert changed["Options"]["parent"] == "eth1.20"
    status, body = await m._docker("GET", "/containers/dida-netmgr/json")
    assert status == 200, body
    endpoint = json.loads(body)["NetworkSettings"]["Networks"][m._net(20)]
    assert endpoint["MacAddress"] == "02:00:00:00:00:20"
    await m.reconcile()
    assert (await m._network(20))["Id"] == changed["Id"]
    status, body = await m._docker("POST", f"/networks/{m._net(20)}/disconnect", {"Container": "dida-netmgr", "Force": True})
    assert status == 200, body
    assert await m._existing() == set()
    parent = "eth0"
    await m.reconcile()
    reattached = await m._network(20)
    assert reattached["Id"] != changed["Id"] and reattached["Options"]["parent"] == "eth0.20"
    status, body = await m._docker("POST", "/containers/create?name=foreign", {
        "Image": "alpine:3", "Cmd": ["sleep", "600"], "HostConfig": {"NetworkMode": "dida-control"},
    })
    assert status == 201, body
    await m._docker("POST", "/containers/foreign/start")
    status, body = await m._docker("POST", f"/networks/{m._net(20)}/connect", {"Container": "foreign"})
    assert status == 200, body
    parent = "eth1"
    try:
        await m.reconcile()
    except RuntimeError as error:
        assert "foreign endpoints" in str(error)
    else:
        raise AssertionError("Network with a foreign endpoint was replaced")
    assert (await m._network(20))["Id"] == reattached["Id"]
    print("PASS: real Docker macvlan parent replacement, fixed MAC, idempotency and foreign endpoint refusal")


asyncio.run(main())
