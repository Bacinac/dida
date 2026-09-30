"""Per-adapter configuration — the UI-editable counterpart to the capability model.

Adapter config (hosts, intervals, credentials) was historically read from
environment variables and off-repo `state/` files — a developer workflow. This
module makes it UI-editable and DB-backed so a new user installs DIDA, opens the
UI, fills in forms, and is done — no file editing, no container restarts.

Design (mirrors how capabilities are the rigid contract):

  * The config SCHEMA per adapter is declared centrally here (`ADAPTER_CONFIG`),
    so the API can render forms for any adapter without importing its package
    (adapters and the API are separate containers).
  * The config VALUES live in the `adapter_config` table, edited via the API.
    Secrets are stored ENCRYPTED (Fernet over DIDA_SECRET_KEY) and masked in the
    UI; plain values (an IP, an interval) are stored as-is.
  * An adapter reads its config through `AdapterConfig`, which asks the api over
    the broker (an adapter holds neither the database nor the key) and polls for
    live edits; values fall back to each field's default.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC
from zoneinfo import ZoneInfo

from dida_core.crypto import derive_fernet

log = logging.getLogger("dida.adapter_config")


@dataclass(frozen=True)
class ConfigField:
    """One editable setting. `type` drives the UI control."""

    key: str
    label: str
    type: str = "text"          # text | number | password | bool | select | textarea | entity
    secret: bool = False        # stored encrypted, masked in the UI
    default: str = ""
    placeholder: str = ""
    help: str = ""
    options: tuple[str, ...] = ()
    # For `entity` fields: restrict the picker to the adapter's own devices that
    # expose this capability (e.g. "media_display" → only screens). Empty = all.
    entity_cap: str = ""
    # Optional section heading in the config form. Fields sharing a `group` (and
    # laid out contiguously) render under one subheading; "" = no heading. Lets a
    # multi-mode adapter (e.g. frigate: remote-viewer vs local-MQTT) separate its
    # fields so it's obvious which set to fill.
    group: str = ""



# Central config contract. Only adapters listed here are UI-configurable; each is
# added as its adapter is migrated to read via AdapterConfig (incremental).
ADAPTER_CONFIG: dict[str, list[ConfigField]] = {
    "ecowitt": [
        ConfigField("hosts", "Gateway IPs (CSV)", placeholder="192.0.2.30", help="Local Ecowitt gateway(s), comma-separated."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="30"),
    ],
    # BABA — the vision/NVR mirror. The adapter subscribes BABA's own NATS and
    # translates its authoritative state into capabilities; cameras come from
    # go2rtc as entities carrying a stream descriptor, pulled through DIDA's proxy.
    # A house and a holiday home each run their own BABA, so this is a LIST of
    # installs, edited through the per-location editor in Settings → Adapters (the
    # raw JSON field stays hidden there). Encrypted: every entry holds a peer key
    # and a go2rtc password. Per location:
    #   name, nats_url, go2rtc, go2rtc_user, go2rtc_password, api_url, peer_key
    # NO defaults for any host: the old ones (127.0.0.1 loopback, 192.0.2.200)
    # outlived the topology that made them true — .200 is decommissioned — so they
    # would have quietly pointed a fresh install at a dead host instead of saying
    # "unset".
    #
    # No tuning knobs here BY DESIGN: motion cooldown, occupancy hysteresis and
    # track staleness are BABA's — it has the frames. DIDA mirrors its state.
    "baba": [
        ConfigField("sites", "Locations (JSON)", type="textarea", secret=True,
                    help="One entry per BABA install. Edit these through the location cards above rather than by hand."),
    ],
    "solar": [
        ConfigField("url", "Inverter URL (local JSON)", placeholder="http://192.0.2.16:8484/getdevdata.cgi?device=2&sn=RG...",
                    help="Local JSON endpoint of the inverter's Solarman/iGEN logger (getdevdata.cgi). Read-only poll."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="60"),
    ],
    "iammeter": [
        ConfigField("url", "Meter URL (local JSON)", placeholder="http://192.0.2.17/monitorjson",
                    help="Local /monitorjson endpoint of the IAMMETER meter (WEM3080 single-phase or "
                         "WEM3080T three-phase). Read-only poll, no cloud."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="10"),
    ],
    # Peer — another DIDA installation, reached over its ordinary HTTPS surface as
    # a logged-in user. `entities` is an ALLOWLIST of remote entity-id globs: a peer
    # link is a curated bridge, so an empty list mirrors nothing and says so.
    "peer": [
        ConfigField("sites", "Locations (JSON)", type="textarea", secret=True,
                    placeholder='[{"name": "Cabin", "api_url": "https://example.org/api", '
                                '"username": "peer", "password": "…", '
                                '"entities": ["iammeter:*", "panasonic:*"]}]',
                    help="One entry per remote DIDA. Create a non-admin user there for this link; "
                         "disabling it at the far end stops the mirror."),
        ConfigField("resync_seconds", "Resync interval (s)", type="number", default="300",
                    help="How often the full catalog is re-read. Values arrive live in between."),
    ],
    "harmony": [
        ConfigField("host", "Hub IP", placeholder="192.0.2.90",
                    help="Logitech Harmony Hub on the LAN."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="15"),
    ],
    # Govee — no clean all-device local protocol, so DIDA runs Wez Furlong's
    # govee2mqtt bridge as a sibling container (dida-govee2mqtt) that talks to
    # Govee (account/AWS IoT realtime + optional API key + LAN) and republishes
    # everything as HA MQTT Discovery on our mosquitto — which THIS govee adapter
    # itself ingests (no separate `ha` adapter anymore). It is also the CONNECT
    # form + bridge lifecycle manager: writes the credential env + restarts on change.
    "govee": [
        ConfigField("email", "Govee email", placeholder="you@example.com",
                    help="Govee account email. Uses the account (AWS IoT) path — realtime updates, no LAN needed."),
        ConfigField("password", "Govee password", type="password", secret=True,
                    help="Govee account password. Stored encrypted; written to the bridge, never shown."),
        ConfigField("api_key", "API key (optional)", type="password", secret=True,
                    help="Govee HTTP API key (Govee Home app → profile → Apply for API Key). Only needed for scene control on devices without LAN."),
    ],
    "shelly": [
        ConfigField("hosts", "Devices — IPs (CSV)", placeholder="192.0.2.31, 192.0.2.32",
                    help="Shelly Gen2+ devices on the LAN, comma-separated. Add/remove is live."),
    ],
    "mqtt": [
        ConfigField("mqtt_url", "MQTT URL", placeholder="mqtt://mosquitto:1883", help="Broker for zigbee2mqtt / zwave-js."),
        ConfigField("username", "MQTT user"),
        ConfigField("password", "MQTT password", type="password", secret=True),
        ConfigField("topic_prefix", "Topic prefix", default="zigbee2mqtt"),
    ],
    # One Frigate adapter, many locations. `sites` is a JSON list (encrypted — it
    # holds per-site passwords), each: {name, url, user, password, go2rtc, and,
    # for a LOCAL Frigate, mqtt_url/mqtt_user/mqtt_password/topic_prefix}. Every
    # camera lives in the single `frigate:<cam>` namespace and carries the site's
    # name as a `site` label the UI groups by. Add/remove is live.
    "frigate": [
        ConfigField("sites", "Lokacije (JSON)", type="textarea", secret=True,
                    placeholder='[{"name": "Cabin", "url": "https://cabin-frigate.example", "user": "dida", "password": "…", "go2rtc": ""}]',
                    help="Frigate NVR po lokaciji: name, url (točka 8971 ako traži login), user, password, opc. go2rtc. Za LOKALNI Frigate dodaj i mqtt_url za žive događaje. Kamere svih lokacija dijele isti popis, razlikuje ih `site` oznaka."),
        ConfigField("config_refresh_s", "Roster refresh (s)", type="number", default="300",
                    help="Koliko često se ponovno čita /api/config svake lokacije (nova kamera/zona se pojavi bez restarta)."),
        ConfigField("track_ttl_s", "Event staleness threshold (s)", type="number", default="600",
                    help="Ako se izgubi Frigate 'end' event (lokalni MQTT), praćeni objekt se odbaci nakon toliko sekundi bez updatea."),
    ],
    "broadlink": [
        ConfigField("host", "Blaster IP", placeholder="192.0.2.50",
                    help="Broadlink RM IR/RF blaster on the LAN."),
        ConfigField("mac", "MAC", placeholder="02:00:5e:00:53:01",
                    help="The blaster's MAC address."),
        ConfigField("type", "Device type", placeholder="0x649b",
                    help="Broadlink device type (hex), e.g. 0x649b for RM4 Pro."),
    ],
    "notify": [
        ConfigField("server", "ntfy server", default="https://ntfy.sh", help="ntfy.sh or your own server."),
        ConfigField("targets", "Recipients (name:topic, CSV)", placeholder="alex:dida-alex, sam:dida-sam",
                    help="Maps a person to an ntfy topic; the name becomes the notify:<name> entity."),
    ],
    "announce": [
        ConfigField("targets", "Speakers (name=entity, CSV)", placeholder="kitchen=cast:kitchen_speaker, bedroom=cast:bedroom_speaker",
                    help="Announcement → target speaker (cast/dlna entity). Language is set in Settings → TTS."),
    ],
    # Cloud/keyed adapters: the whole credential JSON is one encrypted field,
    # stored (and edited) only in the DB — no off-repo file.
    "midea": [
        ConfigField("host", "AC IP", placeholder="192.0.2.40", help="Midea AC on the LAN."),
        ConfigField("id", "Device ID", help="From `msmart-ng discover`."),
        ConfigField("token", "Token", type="password", secret=True),
        ConfigField("k1", "Key (k1)", type="password", secret=True),
        ConfigField("port", "Port", type="number", default="6444"),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="30"),
    ],
    # Panasonic ACs carry Panasonic's own WiFi module, which answers nothing on
    # the LAN — the vendor cloud is the only control path, so the account is the
    # whole configuration.
    "panasonic": [
        ConfigField("username", "Comfort Cloud e-mail", placeholder="you@example.com",
                    help="The Panasonic account the air conditioner is registered to."),
        ConfigField("password", "Password", type="password", secret=True),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="60",
                    help="Cloud API — keep it slow; commands still apply immediately."),
    ],
    "tuya": [
        ConfigField("api_id", "Cloud Access ID", help="Tuya IoT Platform → project → Access ID. To fetch local keys."),
        ConfigField("api_secret", "Cloud Access Secret", type="password", secret=True),
        ConfigField("api_region", "Cloud region", type="select", default="eu",
                    options=("eu", "eu-w", "us", "us-e", "cn", "in"), help="The project's data center: eu=Central, eu-w=Western Europe."),
        ConfigField("config", "Devices (JSON array)", type="textarea", secret=True,
                    placeholder='[{"id": "...", "ip": "203.0.113.x", "key": "...", "name": "Outlet"}]',
                    help="Local Tuya devices: id, ip, local key, name. Auto-filled from the cloud."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="10"),
    ],
    # Roidmi robot vacuum (Xiaomi ecosystem) — fully local miio/MiOT. The token
    # is the device's 32-hex miio token, extracted once from the Mi Home cloud
    # (QR login), then the cloud is out of the control path entirely.
    "roidmi": [
        ConfigField("host", "Robot IP", placeholder="192.0.2.23",
                    help="Roidmi robot vacuum on the LAN (IoT VLAN). Local miio protocol, no cloud."),
        ConfigField("token", "miio token", type="password", secret=True,
                    help="32-hex device token from the Mi Home account the robot is paired with."),
        ConfigField("name", "Name", default="Roidmi",
                    help="Display name; builds the roidmi:vacuum entity."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="30"),
    ],
    # Dreame robot vacuum — CLOUD ONLY. Registering the robot in the Dreamehome
    # app turns its local API off (no miio handshake, every port closed), so the
    # account is the control path; there is nothing on the LAN to fall back to.
    "dreame": [
        ConfigField("email", "Dreamehome email", placeholder="ime@example.com",
                    help="Account the robot is paired with in the Dreamehome app."),
        ConfigField("password", "Dreamehome password", type="password", secret=True),
        ConfigField("country", "Cloud region", type="select", default="eu",
                    options=("eu", "cn", "us", "ru", "sg"),
                    help="Region chosen when the account was created. Croatia = eu."),
        ConfigField("name", "Name", default="Dreame",
                    help="Display name; builds the dreame:vacuum entity."),
        ConfigField("placement", "Map over the plan", type="textarea",
                    help="Where the robot's own map lies on the floor plan, written when you "
                         "drag it into place there. It is what lets a tap on the plan become a "
                         "place in the robot's map. Clear it to lay the map down again."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="30"),
    ],
    "landroid": [
        ConfigField("email", "Worx email", placeholder="ime@example.com",
                    help="Email of the Worx/Kress/Landxcape cloud account."),
        ConfigField("password", "Worx password", type="password", secret=True),
        ConfigField("cloud", "Cloud", type="select", default="worx",
                    options=("worx", "kress", "landxcape")),
        ConfigField("poll_seconds", "Check interval (s)", type="number", default="60",
                    help="How often the adapter checks the connection and config. State arrives on its own (MQTT push + cloud-poll fallback every 5–10 min)."),
        ConfigField("stale_seconds", "Staleness threshold (s)", type="number", default="1800",
                    help="If no report (neither push nor cloud-poll) arrives for longer than this, the badge goes to error (fail-loud)."),
    ],
    # SmartThings — Samsung appliances/AV (TV, soundbar, AC, washer, fridge, robot
    # vacuum) via the cloud. OAuth 2.0: new Personal Access Tokens die in 24h, so
    # a self-registered OAuth client (rolling refresh token) is the only durable
    # path. client_id/secret come from the OAuth app; the tokens themselves are
    # minted by the "Connect" flow and live in the adapter-managed `_oauth` blob
    # (encrypted), never a form field — like homekit's `pairings`.
    "smartthings": [
        ConfigField("client_id", "OAuth Client ID",
                    help="From the SmartThings OAuth app (`smartthings apps:create` → OAuth-In App)."),
        ConfigField("client_secret", "OAuth Client Secret", type="password", secret=True),
        ConfigField("redirect_uri", "Redirect URI (optional)",
                    placeholder="auto: <DIDA_PUBLIC_URL>/api/smartthings/callback",
                    help="Empty = auto from the public URL (Cloudflare tunnel). This is the value you register in the OAuth app."),
        # Home location: a SmartThings account can span multiple homes; without this
        # the adapter would ingest every device on the account (e.g. a TV at another
        # house). Rendered bespoke in the ST card (a dropdown of the account's
        # locations, populated from app_settings). Empty = all locations.
        ConfigField("location", "Location (this home)",
                    help="Ingest only devices from this SmartThings location. Empty = all locations on the account."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="30"),
    ],
    # Google contacts — the household's address book (people + birth dates), read
    # daily. A separate Google Cloud project from the one holding the FCM service
    # account: adding an OAuth client with a sensitive scope makes the consent
    # screen the whole project's, and anything Google does to an unverified one
    # would then reach the smart home's push notifications too.
    #
    # Two Google facts this shape is built around: the browserless device flow does
    # NOT allow a contacts scope (profile, Drive and YouTube only), and an app left
    # in "Testing" is issued a refresh token that dies after seven days — so the
    # consent runs in a browser against a PUBLISHED app. As with smartthings, the
    # refresh token is minted by "Connect" into the encrypted `_oauth` blob, never
    # typed into a field.
    "contacts": [
        ConfigField("client_id", "OAuth Client ID",
                    help="Google Cloud → Credentials → OAuth client ID → Web application, in a project of its own (not the FCM one)."),
        ConfigField("client_secret", "OAuth Client Secret", type="password", secret=True),
        ConfigField("redirect_uri", "Redirect URI (optional)",
                    placeholder="auto: <DIDA_PUBLIC_URL>/api/contacts/callback",
                    help="Empty = auto from the public URL (Cloudflare tunnel). This is the value you register as an Authorised redirect URI."),
        ConfigField("announce_days", "Announce days ahead", type="number", default="1",
                    help="How far ahead a birthday is announced. 1 = today and tomorrow."),
        ConfigField("sync_hour", "Daily sync at (local hour)", type="number", default="4",
                    help="A birth date changes about never; once a day, off-peak, is plenty."),
    ],
    "esphome": [
        ConfigField("config", "Nodes (JSON array)", type="textarea", secret=True,
                    placeholder='[{"name": "Room", "host": "192.0.2.51", "noise_psk": "base64psk=", "password": ""}]',
                    help="ESPHome nodes: name, host, noise_psk (or password). Add/remove is live."),
    ],
    # Cast devices are auto-discovered (mDNS) — no per-device config. These two
    # fields drive the living-room WALL PANEL: which Cast screen shows the DIDA
    # /panel dashboard, reasserted whenever that screen returns to idle/ambient.
    "cast": [
        ConfigField("panel_device", "Wall panel — screen", type="entity", entity_cap="media_display",
                    placeholder="cast:living_room_display",
                    help="Cast screen (Nest Hub / Android TV) that permanently shows the DIDA wall panel. Empty = disabled. Other cast devices work normally as media players."),
        ConfigField("panel_check_seconds", "Panel check (s)", type="number", default="20",
                    help="How often to check the screen still shows the panel; when it falls to ambient, the panel is reasserted."),
    ],
    "denon": [
        ConfigField("host", "AVR IP", placeholder="192.0.2.40",
                    help="Denon/Marantz IP; empty = SSDP auto-discovery."),
        ConfigField("name", "Name", default="Marantz", help="Display name (builds the denon:<name>_main/_zone2 entities)."),
        ConfigField("iface_ip", "Interface IP (SSDP)", help="The network interface IP for discovery; empty = auto."),
    ],
    "heos": [
        ConfigField("host", "HEOS IP", placeholder="192.0.2.41",
                    help="Any HEOS device; empty = auto-discovery."),
        ConfigField("iface_ip", "Interface IP (SSDP)", help="The interface IP for discovery; empty = auto."),
    ],
    "dlna": [
        ConfigField("iface_ip", "Interface IP (SSDP)", help="The interface IP for discovery; empty = auto."),
        ConfigField("exclude_manufacturers", "Exclude (CSV)", default="denon,marantz,ifi",
                    help="Renderers of these manufacturers/names are handled by another adapter (HEOS for Marantz, Volumio for the iFi Zen Stream)."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="3"),
        ConfigField("discovery_seconds", "Discovery interval (s)", type="number", default="60"),
    ],
    # Volumio player (the iFi Zen Stream). Driven via Volumio's native REST API,
    # not DLNA — one known host, richer/reliable state. The dlna adapter excludes
    # it (exclude_manufacturers) so there's one entity per device.
    "volumio": [
        ConfigField("host", "Device — IP", placeholder="192.0.2.41",
                    help="iFi Zen Stream (Volumio) on the LAN. Native Volumio API — more reliable than DLNA."),
        ConfigField("name", "Name", default="iFi",
                    help="Display name; empty = the name Volumio reports itself. Builds the volumio:<name> entity."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="2"),
        ConfigField("ssh_user", "SSH user", default="volumio",
                    help="For actions/settings the Volumio REST doesn't cover (SSH to the device)."),
        ConfigField("ssh_password", "SSH password", type="password", secret=True, default="volumio",
                    help="The SSH user's password; also used for sudo on Reboot."),
        # MPD playback buffering — Volumio hides these; larger buffer = fewer
        # dropouts on network/slow sources at the cost of higher play/seek latency.
        # Applied over SSH (rewrites mpd.conf + config store, restarts MPD → a brief
        # interruption). Empty = leave the device default.
        ConfigField("audio_buffer_kb", "Audio buffer (KB)", type="number", placeholder="8192",
                    help="MPD audio_buffer_size. Default 8192 (8 MB); raise (e.g. 16384) if the stream stutters. Empty = leave it."),
        ConfigField("buffer_before_play", "Fill before play (%)", placeholder="10",
                    help="MPD buffer_before_play (percentage of the buffer filled before starting). Higher = smoother start, slower play. Empty = leave it."),
        # Signal-path DAC node: the external DAC the streamer feeds + the digital link
        # to it. Purely descriptive (DIDA can't measure past the renderer's digital
        # output), so optional — empty DAC name = no DAC node in the signal path.
        ConfigField("dac", "DAC (name)", placeholder="iFi DAC",
                    help="The external DAC the streamer feeds. Shown as a digital node in the signal path. Empty = no DAC node."),
        ConfigField("dac_link", "DAC link", type="select",
                    options=("", "USB", "Coax SPDIF", "Optical SPDIF", "HDMI", "I2S"),
                    help="Digital link streamer → DAC."),
    ],
    # Android TV / Google TV box (NVIDIA Shield, Sony/Philips Android TV, Chromecast
    # with Google TV…). Driven over ADB (python-androidtv) — needs "Network
    # debugging" (developer mode) on the device. Connected by a one-time key-accept
    # prompt on the TV; the RSA keypair lives in the adapter-managed `_adb_key` blob
    # (encrypted), never a field. The app list is PULLED off the device, then
    # annotated (rename / show-hide) via the editor — no generic seed.
    "androidtv": [
        ConfigField("host", "Device — IP", placeholder="192.0.2.60",
                    help="Android TV / Google TV box (e.g. NVIDIA Shield) on the LAN. Enable Network debugging (developer mode) on the device, save the IP, then click Connect."),
        ConfigField("name", "Name", default="Shield",
                    help="Display name; builds the androidtv:<name> entity."),
        ConfigField("apps", "Apps", type="textarea",
                    help="Installed apps pulled off the device. Rename and show/hide them in the source picker."),
    ],
    # Samsung Tizen TV over its own LAN API: power read from REST (8001), turned off
    # with the remote-control websocket (8002), woken with wake-on-LAN. The remote
    # token the TV issues after its on-screen prompt lives in the adapter-managed
    # `_token` blob (encrypted), never a field.
    "samsungtv": [
        ConfigField("host", "TV — IP", placeholder="192.0.2.34",
                    help="Samsung TV on the LAN. The first time it is on, accept DIDA on the TV screen."),
        ConfigField("name", "Name", default="Samsung TV",
                    help="Display name; builds the samsungtv:<name> entity."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="5"),
    ],
    "presence": [
        ConfigField("mqtt_url", "OwnTracks MQTT URL", placeholder="mqtt://mosquitto:1883", help="The broker OwnTracks publishes locations to."),
        ConfigField("username", "MQTT user"),
        ConfigField("password", "MQTT password", type="password", secret=True),
        ConfigField("topic_prefix", "Topic prefix", default="owntracks"),
        ConfigField("zone_reload_seconds", "Zone reload (s)", type="number", default="30"),
    ],
    "unifi": [
        ConfigField("url", "Controller URL", default="https://192.0.2.10:11443",
                    help="The UniFi OS console, e.g. https://192.168.1.1."),
        ConfigField("api_key", "API key", type="password", secret=True,
                    help="Local console: Settings → Control Plane → Integrations → Create API Key. Shown once."),
        ConfigField("verify_tls", "Verify TLS certificate", type="bool", default="false",
                    help="Enable only if the controller presents a certificate this host trusts."),
        ConfigField("site", "Site name", default="Home",
                    help="The zone presence reports when someone is associated to an access point."),
        ConfigField("site_id", "Site id (optional)",
                    help="Leave empty and the first site the controller reports is used."),
        ConfigField("people", "People (one per line)", type="textarea",
                    placeholder="alex: Alex phone | 02:00:5e:00:53:01\nsam: Sam phone",
                    help="user: the CLIENT NAME shown in the controller, then MACs after |. The MACs are the identity where given — a client name is editable and need not be unique; without them the name is matched instead."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="30"),
        ConfigField("refresh_seconds", "Publish refresh (s)", type="number", default="240"),
    ],
    # No config fields: HomeKit is set up by PAIRING (discover + setup code), not a
    # form. Listed here only so it renders as a first-class adapter card (status +
    # accessory cards); the pairing keypair lives in the adapter-managed `pairings`
    # blob (encrypted), never a ConfigField.
    "homekit": [],
    # No config fields either: calendar is a DIDA-internal source that owns the
    # calendar:* schedule entities (edited in the Schedules UI, not here). Listed
    # so it renders as a first-class adapter card — without an entry the Settings →
    # Adapters page never queries dida.status.calendar and its badge is unreachable.
    "calendar": [],
    # Sun elevation + time-of-day source. Location is NOT a field — it comes from
    # the home zone (Zones → the one flagged Home), the single source of truth for
    # where "here" is. Only the refinements (altitude, tz) and cadence are config.
    "astro": [
        ConfigField("elevation", "Altitude (m)", type="number", default="0",
                    help="For a more precise calculation; location (lat/lon) comes from the home zone."),
        ConfigField("tz", "Time zone", default="Europe/Zagreb",
                    help="IANA zone for time-of-day, e.g. Europe/Zagreb."),
        ConfigField("poll_seconds", "Poll interval (s)", type="number", default="60"),
    ],
    "cloudflare": [
        ConfigField("source_path", "services.conf path", default="/ingress/services.conf",
                    help="The single ingress source, bind-mounted in from the Proxmox host. Editing it is all this adapter does; the host applies it."),
        ConfigField("status_path", "Apply status path", default="/ingress/.apply-status.json",
                    help="Written by apply-ingress.sh on the host after every run — the adapter mirrors it so a failed apply shows here instead of passing silently."),
        ConfigField("config_path", "Tunnel config.yml path", default="/cloudflared/config.yml",
                    help="Used only where there is no services.conf: the connectors' own config is then the ingress, and the adapter renders it and rolls them."),
        ConfigField("zone", "DNS zone (domain)", default="",
                    help="The domain routes live under (host.<zone>). Every installation sets its own."),
        ConfigField("api_token", "Cloudflare API token", secret=True, default="",
                    help="Owner's token, scoped Account → Cloudflare Tunnel:Edit + Zone → DNS:Edit. Enables creating a tunnel and DNS records from DIDA; empty = provisioning and DNS stay manual."),
        ConfigField("apply_wait_s", "Apply timeout (s)", type="number", default="150",
                    help="How long a save waits for the host applier to report. Measured worst case is ~51 s — the rolling connector restart alone is two 18 s waits — so this leaves room rather than reporting a timeout on a run that then succeeds."),
        ConfigField("poll_seconds", "Refresh interval (s)", type="number", default="60"),
    ],
}

# The adapter_config table schema lives in db/migrations (0003), applied by the
# migration runner at service boot — not self-healed here.


# Adapter-managed rows (not form fields) that hold credentials: OAuth grants, the
# ADB keypair, a TV's pairing token, HomeKit pairings. Stored encrypted like a
# secret field; the api seals and opens them for the adapter (dida_api.broker).
SEALED_KEYS = frozenset({"_oauth", "_adb_key", "_token", "pairings"})


def _fernet(secret: str):
    return derive_fernet(secret, domain="dida-config-v1")


def encrypt_secret(secret: str, plain: str) -> str:
    return _fernet(secret).encrypt(plain.encode()).decode()


class ConfigDecryptError(Exception):
    """A stored secret could not be decrypted (rotated DIDA_SECRET_KEY / corrupt
    value). Raised — instead of the graceful '' fallback — on the config-backbone
    path (`AdapterConfig.load`) so an adapter FAILS LOUD (status.error, keep the
    last-good device set) rather than laundering the failure into an empty blob
    that reads as 'no devices' and silently tears the whole estate down."""


def decrypt_secret(
    secret: str, token: str, *, adapter: str = "?", key: str = "?", raise_on_error: bool = False
) -> str:
    # An empty/absent DIDA_SECRET_KEY is a hard misconfiguration, not a per-value
    # hiccup — let derive_fernet's ValueError propagate so the adapter's load()
    # fails loud (status.error) instead of silently running with empty creds.
    fernet = _fernet(secret)
    try:
        return fernet.decrypt(token.encode()).decode()
    except Exception as exc:  # a rotated key / corrupt value
        # The config backbone (raise_on_error) must NOT silently degrade to '' —
        # for a secret-blob adapter (esphome/tuya) that empty string reads as an
        # EMPTY device list, silently disconnecting every node. Raise so load()
        # propagates into the adapter's fail-loud handler. Other callers (owntracks,
        # jellyfin, the UI config-read) keep the graceful '' + their own None-guard.
        if raise_on_error:
            raise ConfigDecryptError(f"{adapter}.{key}: undecryptable (key rotated or value corrupt)") from exc
        log.error("adapter_config: could not decrypt %s.%s (key rotated or value corrupt)", adapter, key)
        return ""


# Adapters that can scan the LAN for their devices (a "Scan network" button in
# the UI). Each such adapter answers a `dida.discover.<name>` request with a list
# of found devices; clicking one fills the config form (see discovery.py).
# `baba` scans for other DIDA-adjacent installs rather than devices: a BABA answers
# on its web port with its own title. Its peer key is a secret that exists only on
# that box, so a scan fills every other field and leaves that one to paste.
DISCOVERABLE: set[str] = {"shelly", "broadlink", "esphome", "baba"}


def fields_for(adapter: str) -> list[ConfigField]:
    return ADAPTER_CONFIG.get(adapter, [])


async def read_adapter_config(pool, adapter: str, secret: str, *, secrets: bool = True) -> dict[str, str]:
    """An adapter's merged config straight from the database: stored values over
    each field's default, secrets decrypted — or, with ``secrets=False``, left out.
    The api answers the broker with this; core services read it directly."""
    rows = await pool.fetch("SELECT key, value FROM adapter_config WHERE adapter = $1", adapter)
    db = {r["key"]: r["value"] for r in rows}
    out: dict[str, str] = {}
    for key, f in {f.key: f for f in fields_for(adapter)}.items():
        if f.secret and not secrets:
            continue
        if key in db:
            out[key] = (
                decrypt_secret(secret, db[key], adapter=adapter, key=key, raise_on_error=True)
                if f.secret else db[key]
            )
        else:
            out[key] = f.default
    return out


class AdapterConfig:
    """An adapter's live configuration: stored values over each field's default,
    secrets decrypted, polled for UI edits. `get`/`int` read the merged values.

    ``source`` is the adapter's broker (an adapter has no database) or a pool (a core
    service). Through the broker, another adapter's config comes without its secrets."""

    def __init__(self, adapter: str, source, *, secret: str | None = None) -> None:
        self._adapter = adapter
        self._source = source
        self._secret = secret if secret is not None else os.environ.get("DIDA_SECRET_KEY", "")
        self._fields = {f.key: f for f in fields_for(adapter)}
        self._values: dict[str, str] = {f.key: f.default for f in fields_for(adapter)}
        self._warned: set[tuple[str, object]] = set()

    async def load(self) -> None:
        if hasattr(self._source, "call"):
            self._values = await self._source.call("config", of=self._adapter)
        else:
            self._values = await read_adapter_config(self._source, self._adapter, self._secret)

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(key) or default

    def int(self, key: str, default: int = 0) -> int:
        raw = self._values.get(key) or default
        try:
            return int(raw)
        except (TypeError, ValueError):
            # A typo in a numeric field (a letter O for a zero in a port) used to
            # fall back to the default in silence: the adapter then talks to the
            # wrong port, reports healthy, and nothing anywhere says why. Warned
            # once per offending value — several of these are read inside poll
            # loops that run every few seconds.
            if (key, raw) not in self._warned:
                self._warned.add((key, raw))
                log.warning("adapter_config: %s.%s is not a number (%r) — using %s",
                            self._adapter, key, raw, default)
            return default

    async def poll_loop(self, interval: float = 10) -> None:
        failures = 0
        while True:
            try:
                await self.load()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A dead config poll = the adapter silently runs on stale config
                # forever. That's the config backbone — warn, don't whisper.
                # (Rate-limited: once per ~10 failures after the first.)
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    log.warning("adapter_config poll for %r failing (%d in a row): %s",
                                self._adapter, failures, exc, exc_info=True)
            await asyncio.sleep(interval)


# The installation's own clock. Everything DIDA stores is UTC — the right choice for
# a database — but everything a person READS is their wall clock, and the two differ
# by two hours here for half the year. The zone already existed as a concept (the
# astro adapter's `tz`, which the heating controller reads to decide what "night"
# means); nothing shared it, so surfaces that hand a timestamp to a human had no way
# to ask. `Europe/Zagreb` is the default the astro field itself declares.
_HOUSE_TZ_DEFAULT = "Europe/Zagreb"


async def house_timezone(pool) -> ZoneInfo:
    """The zone this installation lives in, from the astro adapter's config.

    Falls back to the declared default rather than to UTC: an unconfigured
    installation showing UTC is the exact failure this exists to prevent, and a
    wrong-but-plausible local time is more visible than a wrong-but-legitimate
    looking UTC one."""
    try:
        row = await pool.fetchval(
            "SELECT value FROM adapter_config WHERE adapter = 'astro' AND key = 'tz'"
        )
        return ZoneInfo((row or "").strip() or _HOUSE_TZ_DEFAULT)
    except Exception as exc:  # unknown zone name, DB hiccup — never break the caller
        log.warning("house_timezone: falling back to %s (%s)", _HOUSE_TZ_DEFAULT, exc, exc_info=True)
        return ZoneInfo(_HOUSE_TZ_DEFAULT)


def local_str(ts, tz: ZoneInfo, *, with_date: bool = True) -> str:
    """A timestamp as the household reads it. Naive input is assumed UTC, which is
    what every DIDA store hands back."""
    if ts is None:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(tz).strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M")
