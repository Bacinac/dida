"""Display-name translations — localisation of DYNAMIC, adapter-generated
descriptors (entity facet names + device-card headers).

Adapters emit a stable ENGLISH descriptor as the name; the UI localises it through
this table for the selected language, falling back to the English key. Identity is
always `entity_id`/`device_key` (see automations/floor-plan/permissions) — this is
a pure display dictionary keyed on the source string, never on identity, so
translating (or renaming) can't break a reference.

Fixed UI chrome stays in the frontend i18n catalogs (`t()`); only data-driven
descriptors live here, so a new adapter needs no frontend change.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dida_api.assistant import CONTROL_MODEL, ONE_SHOT_THINKING
from dida_api.auth import AuthUser, current_user, require_admin
from dida_api.common import resolve_assistant_client

router = APIRouter(tags=["translations"])


# A hostname is an ADDRESS, not prose: translating `backup.example.com` produces
# a name that resolves to nothing and buries the strings that do need a translator
# (the tunnel's ingress list alone contributes ~50 of them). Recognised by shape
# rather than by adapter, so any source of link names is covered: a scheme prefix,
# or a lowercase dotted DNS name. LOWERCASE is the discriminator that keeps human
# labels which happen to contain a dot — `44.1kHz`, `Temp. outside` — translatable,
# since DNS names in this catalog are always lowercase.
_LINK_NAME_RE = r"^([a-z][a-z0-9+.-]*://|[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$)"

# The translatable universe: every display string currently exposed (entity
# descriptors + device headers), every seeded/edited string, and the user's own
# volume-preset labels — deriving the last one here means a new preset enters the
# universe with no hand-seeding. Shared by the management list and the autofill so
# the two can never drift into disagreeing about what is translatable.
_KEYS_CTE = f"""
    WITH keys AS (
        SELECT DISTINCT name AS key FROM entities WHERE name IS NOT NULL AND name <> ''
        UNION
        SELECT DISTINCT name AS key FROM devices  WHERE name IS NOT NULL AND name <> ''
        UNION
        SELECT DISTINCT key FROM translations
        UNION
        SELECT DISTINCT je->>'label' AS key
        FROM (SELECT value FROM app_settings WHERE key = 'volume_presets' AND value LIKE '[%') vp
             CROSS JOIN LATERAL jsonb_array_elements(vp.value::jsonb) je
        WHERE je->>'label' IS NOT NULL AND je->>'label' <> ''
    ), translatable AS (
        SELECT key FROM keys WHERE key IS NOT NULL AND key <> '' AND key !~ '{_LINK_NAME_RE}'
    )
"""  # noqa: S608


@router.get("/translations")
async def get_translations(lang: str, request: Request,
                           _user: AuthUser = Depends(current_user)) -> dict[str, str]:
    """The {key -> localised value} map for one language — the UI applies it to
    every display name (falling back to the English key when absent)."""
    rows = await request.app.state.pool.fetch(
        "SELECT key, value FROM translations WHERE lang = $1", lang,
    )
    return {r["key"]: r["value"] for r in rows}


@router.get("/translations/keys")
async def list_translatable(request: Request, _admin: AuthUser = Depends(require_admin)) -> list[dict]:
    """Every distinct display string currently exposed (entity descriptors + device
    headers) plus every seeded string (adapter config labels/help, status details,
    remote-button labels, API error messages) with its translations — the Settings →
    Prijevodi management list. The English key IS the base; each row carries whatever
    non-English values exist. Link names are excluded — see _LINK_NAME_RE."""
    pool = request.app.state.pool
    rows = await pool.fetch(
        _KEYS_CTE + """
        SELECT k.key,
               COALESCE(jsonb_object_agg(t.lang, t.value) FILTER (WHERE t.lang IS NOT NULL), '{}'::jsonb) AS langs
        FROM translatable k
        LEFT JOIN translations t ON t.key = k.key
        GROUP BY k.key
        ORDER BY k.key
        """  # noqa: S608
    )
    return [{"key": r["key"], "langs": r["langs"]} for r in rows]


class TranslationIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=500)
    lang: str = Field(..., min_length=2, max_length=8)
    value: str = Field(default="", max_length=500)


@router.put("/translations")
async def put_translation(body: TranslationIn, request: Request,
                          _admin: AuthUser = Depends(require_admin)) -> dict:
    """Set (or clear) one translation. An empty value deletes the row — so the
    display falls back to the English key again."""
    pool = request.app.state.pool
    if body.value.strip():
        await pool.execute(
            "INSERT INTO translations (key, lang, value) VALUES ($1, $2, $3) "
            "ON CONFLICT (key, lang) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            body.key, body.lang, body.value.strip(),
        )
    else:
        await pool.execute("DELETE FROM translations WHERE key = $1 AND lang = $2", body.key, body.lang)
    return {"ok": True}


# --- machine-translate the still-missing descriptors ----------------------------

# The assistant LLM localises the English descriptors in bulk. The admin stays the
# final curator: autofill only touches EMPTY rows (never a hand-curated one), and
# the fills land in the same list to review/override. This is a display dictionary,
# so a wrong fill can only mistranslate a label — never break an entity_id reference.

_LANG_NAMES = {"hr": "Croatian", "en": "English"}
_CHUNK = 25          # strings per LLM call (some help texts are long)
_CONCURRENCY = 5     # bound in-flight calls so a big backlog can't trip rate limits

# System prompt is templated on the target language. The preserve-verbatim block is
# the real quality guard — these strings are dense with identifiers/units/protocol
# names that a naive translation would mangle.
_AUTOFILL_SYSTEM = """You are a professional translator localising a smart-home \
control app's UI strings from English into {language}.

Translate each string into natural, FORMAL/standard {language} as used in serious \
professional software (labels, help text, error messages, sensor/device \
descriptors) — never slang or casual forms.

Preserve VERBATIM (do NOT translate or reorder):
- Identifiers and bus/subject patterns — anything with ':' , '_' or a dotted path \
(denon:<name>_main, baba.events.*, dida.status.<name>, androidtv:<name>).
- Angle-bracket placeholders: <name>, <id>, <naziv> …
- Units and parenthetical hints: (s), (m), (ms), (Hz), (V), (PV1), (− export).
- Numeric/hex literals: 0x649b, 44.1kHz/16bit.
- Protocol / product / brand names and acronyms: MQTT, NATS, JetStream, DHCP, \
WebRTC, HLS, SSDP, mDNS, ESPHome, HEOS, DLNA, OwnTracks, go2rtc, VLAN, JSON, CSV, \
API, TTS, IP, LAN, VAPID, OAuth, Broadlink, Denon, Marantz, Volumio, SmartThings, \
NVIDIA Shield, Nest Hub, BABA, DIDA.
- Config field names referenced literally: name, host, noise_psk, password.

Keep the source punctuation, capitalisation style and any dashes/arrows (—, →). If a \
term is used as-is in {language} (e.g. "Status", "API"), keep it. Return ONLY the \
exact keys given — never invent, split, merge, or drop a key."""

_AUTOFILL_SCHEMA = {
    "type": "object",
    "properties": {
        "translations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"key": {"type": "string"}, "value": {"type": "string"}},
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["translations"],
    "additionalProperties": False,
}


@router.post("/translations/autofill")
async def autofill_translations(lang: str, request: Request,
                                _admin: AuthUser = Depends(require_admin)) -> dict:
    """Machine-translate every still-missing descriptor into `lang` with the
    assistant LLM, then upsert the results. Only fills EMPTY entries — never
    overwrites a curated row — so the admin reviews/edits the fills afterwards.
    Idempotent: re-running touches only whatever is still missing."""
    pool = request.app.state.pool
    client = await resolve_assistant_client(request.app.state)
    if client is None:
        raise HTTPException(503, "assistant is not configured — add an Anthropic key in System → Settings")

    # Same universe as the management list (/translations/keys), narrowed to the
    # keys that have no value for this language yet.
    rows = await pool.fetch(
        _KEYS_CTE + """
        SELECT k.key FROM translatable k
        LEFT JOIN translations t ON t.key = k.key AND t.lang = $1
        WHERE t.value IS NULL OR t.value = ''
        ORDER BY k.key
        """,  # noqa: S608
        lang,
    )
    missing = [r["key"] for r in rows]
    if not missing:
        return {"requested": 0, "filled": 0, "errors": 0}

    system = _AUTOFILL_SYSTEM.format(language=_LANG_NAMES.get(lang, lang))
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def translate(chunk: list[str]) -> dict[str, str]:
        async with sem:
            resp = await client.messages.create(
                model=CONTROL_MODEL,
                max_tokens=8192,
                # Explicit: on the control model an OMITTED thinking field means
                # adaptive, and thinking would spend the budget this bulk JSON
                # response needs. Translation is deterministic — nothing to reason about.
                thinking=ONE_SHOT_THINKING,
                system=system,
                output_config={"format": {"type": "json_schema", "schema": _AUTOFILL_SCHEMA}},
                messages=[{"role": "user",
                           "content": "Translate these UI strings:\n"
                           + json.dumps(chunk, ensure_ascii=False)}],
            )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        wanted = set(chunk)
        out: dict[str, str] = {}
        for item in json.loads(text).get("translations", []):
            key, value = item.get("key", ""), (item.get("value") or "").strip()
            if key in wanted and value:  # ignore invented keys / empty fills
                out[key] = value
        return out

    chunks = [missing[i:i + _CHUNK] for i in range(0, len(missing), _CHUNK)]
    results = await asyncio.gather(*(translate(c) for c in chunks), return_exceptions=True)

    filled: dict[str, str] = {}
    errors = 0
    for r in results:
        if isinstance(r, BaseException):
            errors += 1
        else:
            filled.update(r)

    if filled:
        await pool.executemany(
            "INSERT INTO translations (key, lang, value) VALUES ($1, $2, $3) "
            "ON CONFLICT (key, lang) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            [(key, lang, value) for key, value in filled.items()],
        )
    return {"requested": len(missing), "filled": len(filled), "errors": errors}
