"""Android TV (ADB) <-> canonical-capability mapping tests.

Run inside the androidtv adapter image (dida_adapter_androidtv installed):
    docker run --rm -v /mnt/docker/dida:/w -w /w --entrypoint sh dida/adapter-androidtv:latest \
      -c "python -m pytest tests/test_androidtv_mapping.py"

Covers entity_id slugging, the launchable-app discovery parser + classification
(is_launcher/is_real_app), the configured-apps JSON round-trip (parse_apps ->
app_options/app_friendly/app_link), merge_pulled's rename/hidden-preserving merge
across added/dropped packages, the `dumpsys media_session` now-playing parser
(session picking + the title-carrying-session preference), and the numeric-keypad
keycodes used for channel entry.
"""
import json

from dida_adapter_androidtv.mapping import (
    KEYCODE_ENTER,
    KNOWN_APPS,
    NAV_KEYS,
    TRANSPORT_STATE,
    app_friendly,
    app_link,
    app_options,
    digit_keycodes,
    entity_id,
    is_launcher,
    is_real_app,
    merge_pulled,
    parse_apps,
    parse_launchable,
    parse_media_session,
)


# --- entity_id ---------------------------------------------------------------
def test_entity_id():
    assert entity_id("Shield") == "androidtv:shield", "simple name -> lower-cased slug"
    assert entity_id("Living Room TV") == "androidtv:living_room_tv", "spaces -> underscores"
    assert entity_id("") == "androidtv:shield", "empty name falls back to the 'shield' default slug"
    assert entity_id(None) == "androidtv:shield", "None name falls back the same way as empty"


# --- lookup tables: the quirks the comments call out -------------------------
def test_lookup_tables():
    # standby/off both collapse to idle — a device with no active session isn't
    # "standby", it's just idle to the UI.
    assert TRANSPORT_STATE["standby"] == "idle", "standby collapses to idle"
    assert TRANSPORT_STATE["off"] == "idle", "off collapses to idle"
    assert TRANSPORT_STATE["playing"] == "playing", "playing passes through"
    # the 'ok' nav key drives python-androidtv's `enter` method, not an 'ok' method
    assert NAV_KEYS["ok"] == ("enter", "Select"), "ok nav key -> enter method / Select label"
    assert KEYCODE_ENTER == 66, "ADB KEYCODE_ENTER is 66"


# --- parse_launchable: `cmd package query-activities --brief` dump ----------
def test_parse_launchable():
    output = "\n".join([
        "com.netflix.ninja/com.netflix.ninja.MainActivity",
        "  com.plexapp.android/com.plexapp.android.activity.SplashActivity  ",
        "not a valid app entry",
        "",
        "123.bad.start/Activity",
    ])
    assert parse_launchable(output) == ["com.netflix.ninja", "com.plexapp.android"], \
        "keeps only package ids off well-formed 'package/Activity' lines"
    assert parse_launchable("") == [], "empty dump -> no apps"
    assert parse_launchable(None) == [], "None dump doesn't raise"


# --- is_launcher / is_real_app: system-surface filtering ---------------------
def test_app_classification():
    assert is_launcher("com.google.android.tvlauncher") is True, "known built-in launcher"
    assert is_launcher("com.some.customlauncher") is True, "any '*launcher*' id counts as a launcher"
    assert is_launcher("com.netflix.ninja") is False, "a real app is not a launcher"

    assert is_real_app("com.netflix.ninja") is True, "a normal third-party app is real"
    assert is_real_app("com.android.settings") is False, "com.android.* system packages excluded"
    assert is_real_app("com.google.android.tvlauncher") is False, "the launcher itself is excluded"
    assert is_real_app("com.google.android.gms.setupwizard") is False, "system-surface marker (setupwizard) excluded"
    assert is_real_app("nodothere") is False, "an id without a dot can't be a package"
    assert is_real_app("") is False, "empty id is never a real app"
    assert is_real_app(None) is False, "None id is never a real app"


# --- parse_apps: config JSON -> validated app list ---------------------------
def test_parse_apps():
    assert parse_apps("") == [], "empty config -> no apps"
    assert parse_apps("   ") == [], "whitespace-only config -> no apps"
    assert parse_apps("not json{") == [], "invalid JSON is swallowed, not raised"
    assert parse_apps(json.dumps({"name": "not-a-list"})) == [], "a JSON object (not a list) -> no apps"

    raw = json.dumps([
        {"name": "Netflix", "link": "com.netflix.ninja", "hidden": False},
        {"name": "Kids App", "link": "com.kids.app", "hidden": 1},
        {"name": "", "link": "com.noname.app"},
        {"link": "com.nolink.app.only"},
        {"notadict": True},
        "just a string",
    ])
    assert parse_apps(raw) == [
        {"name": "Netflix", "link": "com.netflix.ninja", "hidden": False},
        {"name": "Kids App", "link": "com.kids.app", "hidden": True},
    ], "entries missing name/link, non-dict entries, and raw strings are all dropped; hidden is bool-cast"


# --- app_options: visible-app-name dropdown source ---------------------------
def test_app_options():
    apps = [
        {"name": "Netflix", "link": "x", "hidden": False},
        {"name": "Hidden App", "link": "y", "hidden": True},
        {"name": "Z Show", "link": "z", "hidden": False},
    ]
    assert json.loads(app_options(apps)) == ["Netflix", "Z Show"], "hidden apps excluded from the dropdown"
    assert app_options([]) == "[]", "no apps -> empty JSON array"


# --- app_friendly: foreground app id -> display label -----------------------
def test_app_friendly():
    assert app_friendly("", []) == "", "empty app id -> empty label"
    assert app_friendly(None, []) == "", "None app id -> empty label"
    assert app_friendly("com.google.android.tvlauncher", []) == "Home screen", "any launcher id -> Home screen"
    assert app_friendly("com.netflix.ninja", []) == "Netflix", "unconfigured but well-known id -> KNOWN_APPS name"

    # a user's saved/renamed app takes precedence over the KNOWN_APPS brand name
    apps = [{"name": "My Netflix Alias", "link": "com.netflix.ninja", "hidden": False}]
    assert app_friendly("com.netflix.ninja", apps) == "My Netflix Alias", \
        "configured apps list wins over the built-in KNOWN_APPS table"

    # name match is case-insensitive
    apps2 = [{"name": "CustomApp", "link": "com.other.thing", "hidden": False}]
    assert app_friendly("customapp", apps2) == "CustomApp", "app_id matching an app's name (any case) resolves it"

    assert app_friendly("com.totally.unknown.pkg", []) == "com.totally.unknown.pkg", \
        "an unrecognised raw package id passes through as-is"


# --- app_link: friendly source name -> package id to launch ------------------
def test_app_link():
    assert app_link("", []) is None, "empty name -> nothing to launch"
    assert app_link(None, []) is None, "None name -> nothing to launch"
    assert app_link("   ", []) is None, "whitespace-only name -> nothing to launch"

    apps = [{"name": "Netflix", "link": "com.netflix.ninja", "hidden": False}]
    assert app_link("Netflix", apps) == "com.netflix.ninja", "known name -> its package id"
    assert app_link(" Netflix ", apps) == "com.netflix.ninja", "name is stripped before matching"
    assert app_link("com.raw.pkg.id", []) == "com.raw.pkg.id", "unrecognised name passes through (raw package id)"


# --- merge_pulled: device apps + saved prefs ---------------------------------
def test_merge_pulled():
    existing = [
        {"name": "Custom Tool (renamed)", "link": "com.custom.tool", "hidden": True},
        {"name": "Old Removed App", "link": "com.old.removed.app", "hidden": False},
    ]
    pulled = ["com.netflix.ninja", "com.custom.tool", "com.netflix.ninja"]  # dupe collapses via set()

    out = merge_pulled(pulled, existing)

    assert [a["link"] for a in out] == ["com.custom.tool", "com.netflix.ninja"], \
        "deduped + sorted by package id; apps no longer pulled from the device are dropped"
    assert out[0] == {"name": "Custom Tool (renamed)", "link": "com.custom.tool", "hidden": True}, \
        "an existing app keeps its rename + hidden flag"
    assert out[1] == {"name": "Netflix", "link": "com.netflix.ninja", "hidden": False}, \
        "a newly-pulled app gets a friendly default (KNOWN_APPS) and hidden=False"
    assert "com.old.removed.app" not in [a["link"] for a in out], \
        "an app no longer installed on the device is dropped from the merged list"


def test_merge_pulled_upgrades_raw_name_when_known():
    # Saved before the package entered KNOWN_APPS -> name == raw package id.
    existing = [{"name": "com.uniqcast.uniqtv.eronet", "link": "com.uniqcast.uniqtv.eronet", "hidden": False}]

    out = merge_pulled(["com.uniqcast.uniqtv.eronet"], existing)

    assert out == [{"name": "HOME.TV TO GO", "link": "com.uniqcast.uniqtv.eronet", "hidden": False}], \
        "a raw-package name carries no user rename intent -> upgrades once KNOWN_APPS learns it"


# --- parse_media_session: dumpsys media_session -> now-playing --------------
def test_parse_media_session():
    assert parse_media_session("") is None, "empty dump -> no session"
    assert parse_media_session(None) is None, "None dump -> no session"

    idle_only = (
        "com.tv.launcher/MediaSession@4444 (userId=0)\n"
        "  state=PlaybackState {state=0, position=0, buffered position=0, speed=0.0, "
        "updated=0, actions=0, custom actions=[], active item id=0, error=null}\n"
    )
    assert parse_media_session(idle_only) is None, "an idle-only session surfaces nothing (not actually on)"

    no_title = (
        "com.someapp.livetv/MediaSession@3333 (userId=0)\n"
        "  state=PlaybackState {state=3, position=10000, buffered position=0, speed=1.0, "
        "updated=0, actions=0, custom actions=[], active item id=0, error=null}\n"
    )
    r = parse_media_session(no_title)
    assert r is not None and r["transport"] == "playing" and "title" not in r, \
        "a playing session with no metadata title still surfaces (as the best fallback)"
    assert r["position_s"] == 10, "position in ms -> whole seconds"

    # two sessions: the first (paused, no title) must NOT win over the second
    # (playing, has a title) — the parser prefers a title-carrying session.
    multi = (
        "com.plexapp.android/MediaSession@1111 (userId=0)\n"
        "  state=PlaybackState {state=2, position=0, buffered position=0, speed=0.0, "
        "updated=0, actions=0, custom actions=[], active item id=0, error=null}\n"
        "com.netflix.ninja/MediaSession@2222 (userId=0)\n"
        "  state=PlaybackState {state=3, position=45000, buffered position=0, speed=1.0, "
        "updated=0, actions=0, custom actions=[], active item id=0, error=null}\n"
        "  metadata: description=Stranger Things, Season 4, Netflix\n"
        "  DURATION=3600000\n"
        "  ART_URI=https://example.com/art.jpg\n"
    )
    r2 = parse_media_session(multi)
    assert r2["transport"] == "playing", "the title-carrying session wins over the earlier titleless one"
    assert r2["title"] == "Stranger Things" and r2["artist"] == "Season 4" and r2["album"] == "Netflix", \
        "description=<title>, <subtitle>, <description> splits into title/artist/album"
    assert r2["position_s"] == 45, "position 45000ms -> 45s"
    assert r2["duration_s"] == 3600, "DURATION 3600000ms -> 3600s"
    assert r2["art"] == "https://example.com/art.jpg", "http(s) art URL is captured"


# --- digit_keycodes: channel number -> ADB keyevents -------------------------
def test_digit_keycodes():
    assert digit_keycodes("123") == [8, 9, 10], "digits 1/2/3 -> KEYCODE_1..3 (offset 7)"
    assert digit_keycodes("0") == [7], "digit 0 -> KEYCODE_0 (7)"
    assert digit_keycodes("9") == [16], "digit 9 -> KEYCODE_9 (16)"
    assert digit_keycodes("12a3!") == [8, 9, 10], "non-digit characters are ignored"
    assert digit_keycodes("") == [], "empty channel number -> no keycodes"


# --- sanity: KNOWN_APPS is a real lookup table used by app_friendly ---------
def test_known_apps_table_sane():
    assert KNOWN_APPS["com.netflix.ninja"] == "Netflix", "brand-name table has the expected Netflix entry"
    assert all(isinstance(v, str) and v for v in KNOWN_APPS.values()), "every KNOWN_APPS label is a non-empty string"
