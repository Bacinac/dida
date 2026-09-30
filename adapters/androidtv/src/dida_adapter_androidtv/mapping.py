"""Android TV ↔ canonical-capability translation (ADB path).

Keeps the protocol-specific bits (entity_id slug, the nav-key cluster mapped to
python-androidtv methods, launchable-app discovery + friendly names) out of the
adapter's control flow.
"""

from __future__ import annotations

import json
import re

NAMESPACE = "androidtv"

_SLUG = re.compile(r"[^a-z0-9_-]+")


def entity_id(name: str) -> str:
    """Stable, subject-safe entity_id from the device name (e.g. 'Shield' →
    androidtv:shield)."""
    slug = _SLUG.sub("_", (name or "").strip().lower()).strip("_") or "shield"
    return f"{NAMESPACE}:{slug}"


# Navigation cluster exposed as momentary `press` buttons (device_type button),
# grouped under the same device card. suffix -> (python-androidtv method, label).
NAV_KEYS: dict[str, tuple[str, str]] = {
    "home": ("home", "Home"),
    "back": ("back", "Back"),
    "up": ("up", "Up"),
    "down": ("down", "Down"),
    "left": ("left", "Left"),
    "right": ("right", "Right"),
    "ok": ("enter", "Select"),
    "menu": ("menu", "Menu"),
}

# media_transport command -> python-androidtv method.
TRANSPORT_METHODS: dict[str, str] = {
    "play": "media_play",
    "pause": "media_pause",
    "play_pause": "media_play_pause",
    "stop": "media_stop",
    "next": "media_next_track",
    "previous": "media_previous_track",
}

# python-androidtv state string -> canonical media_transport value.
TRANSPORT_STATE: dict[str, str] = {
    "playing": "playing",
    "paused": "paused",
    "idle": "idle",
    "standby": "idle",
    "off": "idle",
}

# ADB shell queries that enumerate LAUNCHABLE apps (what shows on the TV home) —
# NOT `pm list packages`, which dumps ~150 system packages. Leanback is the TV
# home category; plain LAUNCHER catches sideloaded phone-style apps (e.g. Immich).
LEANBACK_QUERY = ("cmd package query-activities --brief -a android.intent.action.MAIN "
                  "-c android.intent.category.LEANBACK_LAUNCHER")
LAUNCHER_QUERY = ("cmd package query-activities --brief -a android.intent.action.MAIN "
                  "-c android.intent.category.LAUNCHER")

_ACT_LINE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_]+)+)/")


def parse_launchable(output: str) -> list[str]:
    """Package ids from a `cmd package query-activities --brief` dump — each line
    is `package/Activity`; we keep the package."""
    out = []
    for line in (output or "").splitlines():
        m = _ACT_LINE.match(line.strip())
        if m:
            out.append(m.group(1))
    return out


# Friendly names for common Android TV apps — a raw package id (e.g. a sideloaded
# app not in this table) is shown as-is until the user renames it. KNOWN_APPS also
# lets a well-known app resolve nicely on the first pull.
KNOWN_APPS: dict[str, str] = {
    "com.netflix.ninja": "Netflix",
    "com.google.android.youtube.tv": "YouTube",
    "com.google.android.youtube.tvmusic": "YouTube Music",
    "com.google.android.youtube.tvkids": "YouTube Kids",
    "com.disney.disneyplus": "Disney+",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "com.plexapp.android": "Plex",
    "org.xbmc.kodi": "Kodi",
    "com.spotify.tv.android": "Spotify",
    "com.wbd.stream": "Max",
    "tv.twitch.android.app": "Twitch",
    "org.videolan.vlc": "VLC",
    "org.jellyfin.androidtv": "Jellyfin",
    "tv.emby.embyatv": "Emby",
    "com.apple.atve.androidtv.appletv": "Apple TV",
    "com.google.android.play.games": "Play Games",
    "com.valvesoftware.steamlink": "Steam Link",
    "com.google.android.videos": "Google TV",
    "com.hbo.hbonow": "HBO",
    "nl.giejay.android.tv.immich": "Immich",
    "com.esaba.downloader": "Downloader",
    "com.nvidia.tegrazone3": "NVIDIA Games",
    "com.nvidia.geforcenow": "GeForce NOW",
    "hr.a1.android.tv.xploretv": "A1 Xplore TV",
    "com.uniqcast.uniqtv.eronet": "HOME.TV TO GO",
}

# Home-screen launchers (built-in + any third-party '*launcher*') — mean "no app".
_LAUNCHERS = {
    "com.google.android.tvlauncher",
    "com.google.android.apps.tv.launcherx",
    "com.google.android.leanbacklauncher",
}

# Substrings that mark a system surface we never surface as a launchable app —
# even the leanback launcher lists Settings / Store / vendor tools as tiles.
_SYSTEM_MARKERS = ("settings", "systemui", "packageinstaller", "inputmethod",
                   "provision", "setupwizard", "frameworkpackagestubs", "documentsui",
                   "vending", "inputviewer", "remotelocator", "com.wolf.google")


def is_launcher(app_id: str) -> bool:
    """True for a home-screen launcher (built-in or any third-party '*launcher*')."""
    app_id = str(app_id or "").lower()
    return app_id in _LAUNCHERS or "launcher" in app_id


def is_real_app(app_id: str) -> bool:
    """True for a package worth surfacing as a launchable app — a real app, not the
    launcher/home screen, an Android system package (com.android.*), or a vendor
    system surface (Settings, Store, input/locator tools)."""
    low = str(app_id or "").lower()
    if not low or "." not in low or is_launcher(low):
        return False
    if low.startswith("com.android."):
        return False
    return not any(m in low for m in _SYSTEM_MARKERS)


def parse_apps(raw: str) -> list[dict]:
    """Config apps JSON -> [{name, link, hidden}]. Empty -> [] (the list is filled
    by PULLING it from the device, not a generic seed)."""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [
        {"name": str(d["name"]), "link": str(d["link"]), "hidden": bool(d.get("hidden"))}
        for d in data
        if isinstance(d, dict) and d.get("name") and d.get("link")
    ]


def app_options(apps: list[dict]) -> str:
    """source_options JSON (visible app names) for the UI launch dropdown."""
    return json.dumps([a["name"] for a in apps if not a.get("hidden")], ensure_ascii=False)


def app_friendly(app_id: str, apps: list[dict]) -> str:
    """Foreground app id -> a friendly label. Launcher/home -> 'Home screen'; a
    configured/pulled app -> its (possibly renamed) name; a well-known app -> its
    brand name; otherwise the raw id."""
    app_id = str(app_id or "")
    if not app_id:
        return ""
    if is_launcher(app_id):
        return "Home screen"
    for a in apps:
        if a["link"] == app_id or a["name"].lower() == app_id.lower():
            return a["name"]
    return KNOWN_APPS.get(app_id, app_id)


def app_link(name: str, apps: list[dict]) -> str | None:
    """A friendly source name (or a raw package id) -> the package id to launch."""
    name = (name or "").strip()
    if not name:
        return None
    for a in apps:
        if a["name"] == name:
            return a["link"]
    return name  # allow launching by a raw package id


def merge_pulled(pulled: list[str], existing: list[dict]) -> list[dict]:
    """The device's launchable apps merged with the user's saved prefs: keep each
    app's rename + hidden flag (by package id), drop apps no longer installed, add
    newly-found ones with a friendly default. A saved name equal to the raw package
    id carries no user intent (the app just wasn't in KNOWN_APPS at pull time), so
    it upgrades to the friendly name once the table learns it. This is the whole
    list — pulled from the device, annotated by the user; no generic seed."""
    prev = {a["link"]: a for a in existing}
    out = []
    for pkg in sorted(set(pulled)):
        p = prev.get(pkg)
        out.append({
            "name": p["name"] if p and p["name"] != pkg else app_friendly(pkg, []),
            "link": pkg,
            "hidden": bool(p.get("hidden")) if p else False,
        })
    return out


# --- now-playing from `dumpsys media_session` ------------------------------------
# Apps that publish a MediaSession (YouTube, Netflix, Plex, Jellyfin, Kodi, …) expose
# the real title/artist + playback state; live-TV apps often publish none.
_PB_STATE = {"3": "playing", "2": "paused", "6": "buffering", "1": "stopped", "0": "idle", "7": "idle"}
_SESSION_SPLIT = re.compile(r"\n(?=\s*[\w.]+/\S* \(userId=)")


def parse_media_session(output: str) -> dict | None:
    """Best active session -> EVERYTHING the app exposes via dumpsys: title, artist,
    album, transport, position + duration (when carried), and an HTTP art URL. Skips
    `content://` art (lives on the device, unreachable from a browser). Prefers a
    session that actually carries a title. dumpsys prints the MediaMetadata's
    `description=<title>, <subtitle>, <description>` CharSequence join."""
    if not output:
        return None
    best: dict | None = None
    for block in _SESSION_SPLIT.split(output):
        ms = re.search(r"state=PlaybackState \{state=(\d+)", block)
        if not ms:
            continue
        transport = _PB_STATE.get(ms.group(1))
        if transport not in ("playing", "paused", "buffering"):
            continue  # only surface something actually on
        info: dict = {"transport": transport}
        md = re.search(r"description=(.+)", block)
        if md:
            raw = md.group(1).strip()
            if raw and raw != "null":
                for key, val in zip(("title", "artist", "album"), (p.strip() for p in raw.split(", ")), strict=False):
                    if val and val != "null":
                        info[key] = val
        mp = re.search(r"position=(\d+)", block)
        if mp:
            info["position_s"] = int(mp.group(1)) // 1000
        dur = re.search(r"(?:DURATION|duration)=(\d+)", block)
        if dur and int(dur.group(1)) > 0:
            info["duration_s"] = int(dur.group(1)) // 1000
        art = re.search(r"(?:ART_URI|ALBUM_ART_URI|DISPLAY_ICON_URI|iconUri)=(https?://[^\s,}]+)", block)
        if art:
            info["art"] = art.group(1)
        if info.get("title"):
            return info
        best = best or info
    return best


# Number keys as ADB keyevents (KEYCODE_0 = 7 … KEYCODE_9 = 16); ENTER = 66. Used to
# tune a channel by typing its number into a TV app (channel `set_channel`).
KEYCODE_ENTER = 66


def digit_keycodes(number: str) -> list[int]:
    """Keycodes for each digit of a channel number (non-digits ignored)."""
    return [7 + int(c) for c in str(number) if c.isdigit()]
