"""DIDA's language models: the roles they play and the key sealed with DIDA's
own secret, over home_core.llm.

`chat` answers people — the assistant's turns, explaining a rule, translating
the interface. `author` writes for the house — automation rules and the
address book's spelling. Each is a model chosen from the provider's live list
on Settings → AI; no model is named in code.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import asyncpg
from anthropic import AsyncAnthropic
from dida_core import ConfigDecryptError, decrypt_secret, encrypt_secret
from fastapi import Request
from home_core.llm import THINKS, AiSettings, Role, claude_options

log = logging.getLogger("dida.api.ai")

PROVIDERS = ("anthropic",)
# Every DIDA call is shaped by claude_options and most ask for a JSON schema.
ROLES = (
    Role("chat", THINKS | {"structured_outputs"}),
    Role("author", THINKS | {"structured_outputs"}),
)


def _seal(key: str) -> bytes:
    return encrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), key).encode()


def _open(sealed: bytes) -> str | None:
    try:
        return decrypt_secret(os.environ.get("DIDA_SECRET_KEY", ""), sealed.decode(), raise_on_error=True)
    except ConfigDecryptError:
        log.error("the stored AI key is undecryptable (key rotated or value corrupt) — re-enter it in Settings → AI")
        return None


def ai_settings(pool: asyncpg.Pool) -> AiSettings:
    return AiSettings(pool, providers=PROVIDERS, roles=ROLES, encrypt=_seal, decrypt=_open)


def settings_of(request: Request) -> AiSettings:
    return ai_settings(request.app.state.pool)


@dataclass(frozen=True, slots=True)
class Llm:
    """A role's model on DIDA's Anthropic client."""

    client: AsyncAnthropic
    model: str
    ai: AiSettings | None = None

    async def create(self, *, think: bool, effort: str = "low", schema: dict[str, Any] | None = None,
                     **kwargs: Any) -> Any:
        resp = await self.client.messages.create(
            model=self.model, **kwargs, **claude_options(self.model, think=think, effort=effort, schema=schema)
        )
        if self.ai is not None:
            await self.ai.touch("anthropic")
        return resp


async def llm_for(state: Any, role: str) -> Llm:
    """The model `role` is set to, on a client rebuilt when the key changes so a
    key replaced on Settings → AI takes effect at once. Raises AiNotConfigured,
    which the api answers as 503 with the reason."""
    ai = ai_settings(state.pool)
    pick = await ai.pick(role)
    if getattr(state, "anthropic_key", None) != pick.api_key:
        state.anthropic_client = AsyncAnthropic(api_key=pick.api_key)
        state.anthropic_key = pick.api_key
    return Llm(state.anthropic_client, pick.model, ai)
