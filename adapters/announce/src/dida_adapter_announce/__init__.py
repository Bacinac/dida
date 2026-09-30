"""DIDA announce adapter.

Spoken announcements without Home Assistant: an automation fires a `say` command
on an `announce:<where>` entity and this adapter turns the text into speech and
plays it on a configured Cast speaker. It owns no device — it synthesises a TTS
audio URL (Google Translate TTS) and routes a `play_media` command over the bus
to the Cast adapter, which already plays media URLs. Clean separation: TTS +
routing here, playback in the cast adapter. Targets (where -> cast entity) are
configured in Settings → Adapters (DB); the adapter idles until at least one exists.
"""

from __future__ import annotations

from dida_adapter_announce.adapter import AnnounceAdapter

__all__ = ["AnnounceAdapter"]
