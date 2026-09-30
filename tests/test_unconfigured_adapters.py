"""An adapter with no device configured must register NOTHING.

Both metering adapters used to publish their whole catalog from start(),
"independent of first poll", so a fresh install carried a grid meter and a solar
inverter nobody owns — 23 entities for hardware that was never there. The catalog
is still published before the first reading (the card needs names and a header),
just not before there is a device to read.

These drive start(), not _poll(): the regression lived in start(), so a test that
only exercised the poll loop would have passed against the broken code.

Run inside the api image (both adapter packages are pure-python; PYTHONPATH picks
them up from the working tree):
    python -m pytest tests/test_unconfigured_adapters.py
"""
import asyncio

import pytest
from conftest import FakeBroker
from dida_adapter_iammeter import adapter as iammeter_mod
from dida_adapter_solar import adapter as solar_mod

MODULES = [iammeter_mod, solar_mod]
IDS = ["iammeter", "solar"]


class Captured:
    def __init__(self):
        self.entities = []

    async def publish_entity(self, info):
        self.entities.append(info.entity_id)

    async def publish_state(self, update):
        pass


class Cfg:
    """Stands in for AdapterConfig: the lookups start() and _poll() make."""

    def __init__(self, url=""):
        self._url = url

    def __call__(self, *_args, **_kwargs):  # constructed as AdapterConfig(ns, broker)
        return self

    async def load(self):
        pass

    async def poll_loop(self):
        await asyncio.sleep(3600)

    def get(self, key, default=None):
        return self._url if key == "url" else default

    def int(self, _key, default):
        return default


class Status:
    def __init__(self):
        self.idle_reasons = []

    def idle(self, reason):
        self.idle_reasons.append(reason)

    def ok(self, _detail):
        pass

    def error(self, _detail):
        pass


def _adapter(mod, url, monkeypatch):
    cls = next(
        v for k, v in vars(mod).items()
        if k.endswith("Adapter") and isinstance(v, type) and k != "Adapter"
    )
    monkeypatch.setattr(mod, "AdapterConfig", Cfg(url))
    a = cls()
    a.status = Status()  # the runner assigns both on the instance at boot
    a.broker = FakeBroker(state=[])
    return a


async def _start_briefly(adapter):
    """start() ends in the poll loop and never returns — run it long enough to get
    through announce-or-not, then cancel."""
    task = asyncio.ensure_future(adapter.start(Captured()))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await adapter.stop()


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
@pytest.mark.asyncio
async def test_no_device_configured_registers_nothing(mod, monkeypatch):
    a = _adapter(mod, url="", monkeypatch=monkeypatch)
    await _start_briefly(a)
    assert a._bus.entities == []


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
@pytest.mark.asyncio
async def test_unconfigured_says_so_instead_of_going_quiet(mod, monkeypatch):
    """Registering nothing must not read as "everything is fine" — the badge has to
    say the device is unconfigured, otherwise an empty card looks like a healthy one."""
    a = _adapter(mod, url="", monkeypatch=monkeypatch)
    await _start_briefly(a)
    assert a.status.idle_reasons, "an unconfigured adapter must state why it is idle"
    assert "not configured" in a.status.idle_reasons[0]


@pytest.mark.parametrize("mod", MODULES, ids=IDS)
@pytest.mark.asyncio
async def test_configured_device_still_gets_its_catalog(mod, monkeypatch):
    """The other half of the rule: once a device IS configured the catalog is
    published before the first reading, so the card has names straight away."""
    a = _adapter(mod, url="http://198.51.100.7/monitorjson", monkeypatch=monkeypatch)
    await _start_briefly(a)
    assert a._bus.entities, "a configured device must still announce its catalog"
