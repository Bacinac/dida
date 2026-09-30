"""Regression tests for the adapter-config secret path (dida_core.adapter_config).

Guards the fail-loud fix for the audit's MED finding: a decrypt failure (rotated
DIDA_SECRET_KEY / corrupt value) must be DISTINGUISHABLE from a genuinely empty
value, so the config backbone (AdapterConfig.load) can fail loud instead of
laundering the failure into '' — which esphome/tuya read as an EMPTY device list
and silently disconnect the whole estate. The graceful '' fallback stays for the
other callers (owntracks/jellyfin/UI) that carry their own None-guard.

Pure crypto round-trip (no DB): encrypt with one key, decrypt with another.
Run inside the api image (dida_core installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/api:latest \
      -c "python -m pytest tests/test_adapter_config.py"
"""
import pytest
from dida_core import ConfigDecryptError, decrypt_secret, encrypt_secret

KEY_A = "integration-secret-key-AAAAAAAAAAAAAAAA"
KEY_B = "integration-secret-key-BBBBBBBBBBBBBBBB"  # a DIFFERENT (rotated) key

# ── happy path: encrypt/decrypt round-trip with the SAME key ──
token = encrypt_secret(KEY_A, "s3cr3t-value")


def test_round_trip():
    assert decrypt_secret(KEY_A, token) == "s3cr3t-value", "round-trip decrypts to the plaintext"
    assert decrypt_secret(KEY_A, encrypt_secret(KEY_A, "")) == "", "a genuinely-empty encrypted value decrypts to ''"


def test_wrong_key():
    # wrong key (simulates a rotated DIDA_SECRET_KEY)
    # default (graceful) path: '' so the many None-guarding callers keep working
    assert decrypt_secret(KEY_B, token) == "", "wrong key, graceful default → '' (unchanged for owntracks/jellyfin/UI)"
    # config-backbone path: FAIL LOUD instead of laundering the failure into ''
    with pytest.raises(ConfigDecryptError):  # wrong key, raise_on_error → ConfigDecryptError (esphome/tuya won't silently drop devices)
        decrypt_secret(KEY_B, token, raise_on_error=True)


def test_corrupt_ciphertext():
    # corrupt ciphertext behaves the same as a bad key
    assert decrypt_secret(KEY_A, "not-a-valid-fernet-token") == "", "corrupt token, graceful → ''"
    with pytest.raises(ConfigDecryptError):  # corrupt token, raise_on_error → ConfigDecryptError
        decrypt_secret(KEY_A, "not-a-valid-fernet-token", raise_on_error=True)


def test_empty_secret_is_hard_misconfig():
    # an EMPTY/absent key is a hard misconfig, not a per-value hiccup: it must
    # raise (ValueError from derive_fernet) on BOTH paths, never key every install
    # identically off an empty secret
    with pytest.raises(ValueError):  # empty secret raises (graceful path too — hard misconfig)
        decrypt_secret("", token)
    with pytest.raises(ValueError):  # empty secret raises on the backbone path
        decrypt_secret("", token, raise_on_error=True)


# ── a typo in a numeric field must not be silent ──
#
# `AdapterConfig.int` is how ~20 adapters read ports, poll intervals and
# timeouts out of Settings → Adapters. The field is a text input, so "8O96" (a
# letter O for a zero) is a thing a person types. It used to fall back to the
# default without a word: the adapter then dials the wrong port, reports
# healthy, and nothing anywhere says why. Same class as the schedule date that
# turned into "today" — a stored value that parses to a default rather than to
# an error.


class _StubPool:
    async def fetch(self, sql, *a):
        return []


def _cfg(values):
    from dida_core.adapter_config import AdapterConfig

    c = AdapterConfig("midea", _StubPool(), secret=KEY_A)
    c._values = values
    return c


def test_a_valid_number_is_read():
    assert _cfg({"port": "6445"}).int("port", 6444) == 6445


def test_an_absent_key_uses_the_default_quietly(caplog):
    """Nothing is wrong here — the field was simply never filled in."""
    with caplog.at_level("WARNING"):
        assert _cfg({}).int("port", 6444) == 6444
    assert caplog.records == []


@pytest.mark.parametrize("typo", ["8O96", "6444 ports", "", "  ", "64.44", "-"])
def test_a_typo_falls_back_but_says_so(typo, caplog):
    """Still falls back — raising here would kill an adapter from inside a poll
    loop, which is worse than a working default. But it has to be visible."""
    with caplog.at_level("WARNING"):
        assert _cfg({"port": typo}).int("port", 6444) == 6444
    if typo.strip():
        assert any("port" in r.message and "midea" in r.message for r in caplog.records), \
            f"{typo!r} fell back in silence"


def test_the_warning_names_the_adapter_the_key_and_the_value(caplog):
    """"a config value is bad" in a log shared by 33 adapters is not actionable."""
    with caplog.at_level("WARNING"):
        _cfg({"port": "8O96"}).int("port", 6444)
    msg = caplog.records[0].getMessage()
    assert "midea" in msg and "port" in msg and "8O96" in msg and "6444" in msg


def test_it_warns_once_per_value_not_once_per_read(caplog):
    """Several of these are read inside loops that run every few seconds. A
    warning per iteration is a log nobody can read, which is the same as silence
    with extra disk cost."""
    c = _cfg({"port": "8O96"})
    with caplog.at_level("WARNING"):
        for _ in range(50):
            c.int("port", 6444)
    assert len(caplog.records) == 1


def test_a_corrected_value_warns_again_if_it_is_wrong_again(caplog):
    """The suppression is per VALUE, not per key — an admin fixing the typo and
    then making a different one must still be told."""
    c = _cfg({"port": "8O96"})
    with caplog.at_level("WARNING"):
        c.int("port", 6444)
        c._values["port"] = "64X4"
        c.int("port", 6444)
    assert len(caplog.records) == 2
