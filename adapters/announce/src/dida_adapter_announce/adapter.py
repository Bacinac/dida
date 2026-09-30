from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    StateUpdate,
    slug,
    validate_command,
)
from dida_core.identity import COMMANDS
from home_core.tasks import spawn

log = logging.getLogger("dida.adapter.announce")

NAMESPACE = "announce"
# Only adapters that actually implement `play_media` (play an arbitrary URL) can
# voice an announcement. A `media_transport` capability alone isn't enough — e.g.
# a SmartThings speaker reports transport but its cloud adapter can't play a URL,
# so offering it would be a dead "Test" button. The bus lets announce command these
# and no others, so the list lives with the bus permissions.
_SPEAKER_ADAPTERS = COMMANDS[NAMESPACE]
# Google Translate's TTS endpoint (client=tw-ob = the long-standing "translate
# widget" client HA's google_translate platform uses). One short phrase per call.
_TTS_URL = "https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl={lang}&q={q}"
_TTS_LIMIT = 180  # translate_tts rejects a q longer than ~200 chars with HTTP 400


def _tts_chunks(text: str) -> list[str]:
    """Split `text` into pieces of at most _TTS_LIMIT chars on word boundaries, so a
    long announcement can be spoken as several clips instead of silently failing the
    endpoint's length limit. A single over-long token is hard-split as a last resort."""
    chunks: list[str] = []
    cur = ""
    for word in text.split():
        if len(word) > _TTS_LIMIT:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(word), _TTS_LIMIT):
                chunks.append(word[i:i + _TTS_LIMIT])
            continue
        cand = f"{cur} {word}".strip()
        if len(cand) > _TTS_LIMIT:
            chunks.append(cur)
            cur = word
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks or [text[:_TTS_LIMIT]]


def _parse_targets(raw: str) -> dict[str, str]:
    """targets = "kitchen=cast:kitchen_speaker,bedroom=cast:bedroom_speaker"
    → {announce_entity_id: speaker_entity_id}. Uses '=' (speaker ids contain ':')."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if "=" in part:
            name, target = part.split("=", 1)
            if name.strip() and target.strip():
                out[f"{NAMESPACE}:{slug(name)}"] = target.strip()
    return out


class AnnounceAdapter:
    """Speaks `say` commands on a Cast speaker. Owns no device: it builds a TTS
    audio URL and publishes a `play_media` command to the target speaker entity
    (the cast adapter plays it). The readable value is the last spoken text, so
    the `announce:*` entity registers and shows up in the editor. Implements
    `dida_core.Adapter`."""

    name = NAMESPACE

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._cfg: AdapterConfig | None = None
        self._targets: dict[str, str] = {}       # merged: auto-discovered + explicit
        self._explicit: dict[str, str] = {}      # from the CSV config (aliases, override)
        self._names: dict[str, str] = {}         # announce entity_id -> friendly label
        self._lang = "hr"  # default; DB (app_settings.announce_lang) overrides via _config_loop
        self._spoken = False  # once True, stop re-seeding "" (would clobber state)
        self._seed_task: asyncio.Task | None = None
        self._config_task: asyncio.Task | None = None

    async def start(self, bus: Bus) -> None:
        self._bus = bus
        self._cfg = AdapterConfig("announce", self.broker)
        await self._cfg.load()
        # Every speaker that can play a URL is auto-offered as a target (+ any
        # explicit CSV aliases from Settings → Adapters).
        await self._refresh_targets()
        log.info("announce adapter up — %d speaker(s): %s", len(self._targets),
                 ", ".join(f"{k}->{v}" for k, v in self._targets.items()) or "(none)")
        self._seed_task = spawn(self._seed_loop(), log=log, name="announce seed loop")
        self._config_task = spawn(self._config_loop(), log=log, name="announce config loop")
        await asyncio.Event().wait()  # idle; announcements arrive as commands

    async def _discover_speakers(self) -> list[tuple[str, str, str]]:
        """Auto-offer EVERY media renderer that can play a URL (any entity with
        `media_transport` — cast + DLNA speakers) as an announce target, so the
        user never has to hand-map speakers. An explicit CSV alias for a speaker
        suppresses its auto entry (no duplicate for the same device)."""
        rows = await self.broker.call("entities", capability="media_transport")
        aliased = set(self._explicit.values())
        out: list[tuple[str, str, str]] = []  # (announce_id, speaker_id, friendly)
        for r in rows:
            speaker = r["entity_id"]
            if speaker.split(":", 1)[0] not in _SPEAKER_ADAPTERS:
                continue  # its adapter can't play a URL → a dead announce target
            if speaker in aliased:
                continue
            local = speaker.split(":", 1)[1] if ":" in speaker else speaker
            # Name the announce entity after the SPEAKER's friendly name, not its
            # raw id — so a UUID-keyed device (SmartThings) shows "Living Room",
            # not the UUID.
            friendly = (r["friendly"] or "").strip() or local.replace("_", " ").title()
            out.append((f"{NAMESPACE}:{slug(friendly)}", speaker, friendly))
        return out

    async def _refresh_targets(self) -> None:
        """Recompute the target set = auto-discovered speakers + explicit aliases
        (explicit wins on an id collision). Seed any newly-appeared target so it
        registers + shows in the TTS page without a restart."""
        self._explicit = _parse_targets(self._cfg.get("targets") or "") if self._cfg else {}
        merged: dict[str, str] = {}
        names: dict[str, str] = {}
        for aid, speaker, friendly in await self._discover_speakers():
            merged[aid] = speaker
            names[aid] = friendly
        for aid, speaker in self._explicit.items():  # aliases win
            merged[aid] = speaker
            names.setdefault(aid, aid.split(":", 1)[1].replace("_", " ").title())
        self._names = names
        for eid in set(merged) - set(self._targets):
            await self._publish_state(eid, "")
        self._targets = merged
        if self._targets:
            self.status.ok(f"{len(self._targets)} speaker(s)")
        else:
            self.status.idle("no speakers")

    async def _config_loop(self) -> None:
        # Poll UI config so changes apply live: the default language (Settings →
        # TTS) and the target speakers (Settings → Adapters).
        while True:
            try:
                if self._cfg is not None:
                    await self._cfg.load()
                    # Re-discover speakers + re-read aliases so a newly-added
                    # Cast/DLNA renderer shows up as a target without a restart.
                    await self._refresh_targets()
                v = await self.broker.call("setting", key="announce_lang")
                if v:
                    self._lang = str(v)
            except Exception as exc:
                log.warning("announce: config poll failed: %s", exc, exc_info=True)
            await asyncio.sleep(10)

    async def _seed_loop(self) -> None:
        # Seed each announce entity (value "") so it registers + shows in the
        # editor. Retry a few times to self-heal the boot race (the engine's
        # subscription may not be ready on our first publish); stop once a real
        # announcement has landed, so we never clobber it with "".
        for _ in range(6):
            if self._spoken:
                return
            for eid in self._targets:
                await self._publish_state(eid, "")
            await asyncio.sleep(3)

    async def _publish_state(self, eid: str, text: str) -> None:
        if self._bus is None:
            return
        name = self._names.get(eid) or eid.split(":", 1)[1].replace("_", " ").title()
        await self._bus.publish_state(
            StateUpdate(
                entity_id=eid, capability="announce", value=text,
                adapter=NAMESPACE, ts_ns=time.time_ns(), name=name,
            )
        )

    async def handle_command(self, command: Command) -> None:
        speaker = self._targets.get(command.entity_id)
        if speaker is None or self._bus is None:
            # Ours but unroutable — a typo'd automation otherwise loses the
            # notification with zero trace (worst case for an announce channel).
            raise CommandRejected(
                f"no target configured, dropping {str(command.args.get('text') or '')[:60]!r}")
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        if command.command != "say":
            return
        # `text` is canonical; `value` is accepted too so the generic rule editor
        # (which collects an args.value) can author a `say` action.
        text = str(command.args.get("text") or command.args.get("value") or "").strip()
        if not text:
            return
        lang = str(command.args.get("language") or self._lang)
        # translate_tts 400s on a q longer than ~200 chars, so a long announcement
        # used to SILENTLY never speak. Chunk on word boundaries; one chunk plays
        # inline (the common case, unchanged), several play in order in a background
        # task so the handler returns promptly.
        chunks = _tts_chunks(text)
        if len(chunks) == 1:
            await self._play(speaker, lang, chunks[0], command.source)
        else:
            spawn(self._play_sequence(speaker, lang, chunks, command.source),
                  log=log, name=f"announce sequence -> {speaker}")
        self._spoken = True
        await self._publish_state(command.entity_id, text)
        log.info("announce -> %s [%s] (%d chunk(s)): %r", speaker, lang, len(chunks), text[:60])

    async def _play(self, speaker: str, lang: str, chunk: str, source: str) -> None:
        # Route to the Cast adapter, which already plays media URLs. Propagate the
        # incoming source — the audit trail keeps the initiator, not the relay.
        url = _TTS_URL.format(lang=quote(lang), q=quote(chunk))
        await self._bus.publish_command(Command(
            entity_id=speaker, capability="media_transport", command="play_media",
            ts_ns=time.time_ns(), args={"uri": url, "mime": "audio/mpeg"},
            source=source,
        ))

    async def _play_sequence(self, speaker: str, lang: str, chunks: list[str], source: str) -> None:
        for i, chunk in enumerate(chunks):
            await self._play(speaker, lang, chunk, source)
            if i < len(chunks) - 1:
                # Rough spoken duration (~12 chars/s) so the next chunk doesn't clobber
                # this one before it finishes; a small floor covers very short chunks.
                await asyncio.sleep(max(2.0, len(chunk) / 12.0))

    async def stop(self) -> None:
        self._bus = None
        # Cancel the two background loops we own BEFORE closing the pool they use,
        # so stop() is self-contained (the runner's cancel_all_tasks() also reaps
        # them, but a direct stop() — e.g. in a test/host that doesn't route
        # through the runner — must not leave them running against a closed pool).
        tasks = [t for t in (self._seed_task, self._config_task) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._seed_task = self._config_task = None
