"""Unit test — LLM API keys are Fernet-encrypted at rest (dida_api.common).
The keys ride out inside pg_dump config backups, so plaintext-at-rest is a real
billing-exposure leak; every peer secret is encrypted and these must match.
"""
import dida_api.common as common
from dida_core import encrypt_secret

SECRET = "test-secret-for-llm-key-encryption-0123456789"


async def test_llm_keys_encrypted_at_rest(monkeypatch, caplog):
    monkeypatch.setenv("DIDA_SECRET_KEY", SECRET)
    store: dict = {}

    async def fake_get(pool, key):
        return store.get(key)

    monkeypatch.setattr(common, "get_setting", fake_get)

    store["anthropic_api_key"] = encrypt_secret(SECRET, "sk-ant-live")
    assert store["anthropic_api_key"] != "sk-ant-live", "stored form is ciphertext"
    assert await common.stored_api_key(None, "anthropic_api_key") == "sk-ant-live", "reads back decrypted"

    # An unset key is a no-op, not a crash.
    assert await common.stored_api_key(None, "openai_api_key") is None, "unset key -> None"

    # A row this installation cannot decrypt (rotated key / corrupt value) must NOT
    # come back raw — that ciphertext would leave as an "API key" and return an
    # opaque 401 from the provider. None, and loudly logged.
    store["openai_api_key"] = encrypt_secret("a-different-secret-key-0123456789ab", "sk-other")
    assert await common.stored_api_key(None, "openai_api_key") is None, "undecryptable -> None, never raw"
    assert "undecryptable" in caplog.text, "and the operator is told why"
