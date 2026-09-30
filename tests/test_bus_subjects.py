"""Regression tests for the pure subject-builders + credential redaction used by
the NATS bus (dida_core.bus, dida_core.events, dida_core.health).

The subject builders (dida_core.events, dida_core.health) are plain f-string
builders — no I/O. A fact's subject names its publisher, and `vouched` is what
every consumer holds a payload's claim against. _redact_url is the one pure helper
DEFINED in bus.py: it masks the password in a NATS connection URL (including
comma-separated server lists) before it's logged.

Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_bus_subjects.py"
"""
import pytest
from dida_core.bus import _redact_url
from dida_core.events import (
    CORE,
    command_subject,
    entity_subject,
    heartbeat_subject,
    journal_subject,
    logs_subject,
    namespace_commands,
    publisher,
    reachability_subject,
    state_subject,
    vouched,
)
from dida_core.health import status_subject


def test_subject_builders():
    assert state_subject("mqtt", "mqtt:kitchen_light") == "dida.state.mqtt.mqtt:kitchen_light", \
        "a state's subject names its publisher before the entity"
    assert entity_subject("mqtt", "mqtt:kitchen_light") == "dida.entity.mqtt.mqtt:kitchen_light"
    assert state_subject("unifi", "presence:marko") == "dida.state.unifi.presence:marko", \
        "the publisher, not the entity's namespace: unifi writes presence entities"
    assert reachability_subject("esphome") == "dida.reachability.esphome"
    assert heartbeat_subject("esphome") == "dida.heartbeat.esphome"
    assert command_subject("mqtt:kitchen_light") == "dida.command.mqtt.mqtt:kitchen_light", \
        "a command's subject carries its namespace, so an adapter can listen to its own"
    assert namespace_commands("mqtt") == "dida.command.mqtt.>"
    assert status_subject("mqtt") == "dida.status.mqtt", \
        "status_subject prefixes with dida.status."


def test_journal_and_logs_name_the_adapter_not_its_label():
    # An adapter's journal source and log service read "adapter:<name>"; the subject
    # carries the bare name, the one the server grants it.
    assert journal_subject("adapter:mqtt") == "dida.journal.mqtt"
    assert logs_subject("adapter:mqtt") == "dida.logs.mqtt"
    assert journal_subject("engine") == "dida.journal.engine"
    assert journal_subject("") == f"dida.journal.{CORE}", "a core event with no source is core's"


@pytest.mark.parametrize("claim", ["a.b", "a b", "*", ">", "x>"])
def test_a_claim_that_would_break_the_subject_is_refused(claim):
    with pytest.raises(ValueError):
        publisher(claim)


def test_vouched_holds_the_claim_against_the_subject():
    assert vouched("dida.state.mqtt.mqtt:x", "mqtt")
    assert vouched("dida.journal.mqtt", "adapter:mqtt")
    assert not vouched("dida.state.mqtt.shelly:x", "shelly"), "the subject says mqtt sent it"
    assert not vouched("dida.logs.mqtt", "adapter:shelly")
    assert not vouched("dida.logs.mqtt", "a.b"), "an unencodable claim is never vouched for"


def test_redact_url_masks_single_credential():
    assert _redact_url("nats://user:pass@host:4222") == "nats://user:***@host:4222", \
        "password is replaced with ***, user/host/port untouched"


def test_redact_url_masks_comma_separated_server_list():
    assert (
        _redact_url("nats://u1:p1@h1:4222,nats://u2:p2@h2:4222")
        == "nats://u1:***@h1:4222,nats://u2:***@h2:4222"
    ), "every server in a comma-separated list gets its password masked"


def test_redact_url_no_credentials_is_unchanged():
    assert _redact_url("nats://host:4222") == "nats://host:4222", \
        "a URL with no user:pass@ is passed through unchanged"
    assert _redact_url("localhost:4222") == "localhost:4222", \
        "a bare host:port with no scheme is passed through unchanged"
