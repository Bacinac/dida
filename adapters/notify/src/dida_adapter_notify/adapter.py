from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from dida_core import (
    AdapterConfig,
    Bus,
    CapabilityError,
    Command,
    CommandRejected,
    EntityInfo,
    slug,
    validate_command,
)

log = logging.getLogger("dida.adapter.notify")

NAMESPACE = "notify"

# notify:all fans a notification out to every target on every channel.
SNAP_SUBJECT = "dida.camera.snap"   # api mints one frame for a notification
CTL_SUBJECT = "dida.notify.ctl"     # api asks which targets can actually be reached
ALL_ENTITY = f"{NAMESPACE}:all"

# RFC 8292 `sub` claim — a contact for push-service operators. Overridable via
# app_settings key `webpush_contact` if a push service ever insists on a
# reachable address; the default is accepted by FCM/Mozilla/Apple in practice.
DEFAULT_CONTACT = "mailto:admin@example.com"

# Web Push TTL: how long the push service holds an undelivered message for a
# briefly-offline phone. An hour — a home notification much older than that is
# stale noise, not information.
WEBPUSH_TTL_S = 3600


def _parse_targets(raw: str) -> dict[str, str]:
    """targets = "alex:dida-alex,sam:dida-sam" -> {entity_id: topic}."""
    out: dict[str, str] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if ":" in part:
            name, topic = part.split(":", 1)
            if name.strip() and topic.strip():
                out[f"{NAMESPACE}:{slug(name)}"] = topic.strip()
    return out


def _app_build(user_agent: str | None) -> int:
    """Build number out of `DIDA-App/v0.1.858 (Android)`. 0 when unreadable —
    treated as too old, because guessing in the other direction buzzes a phone."""
    match = re.search(r"/v\d+\.\d+\.(\d+)", user_agent or "")
    return int(match.group(1)) if match else 0


class NotifyAdapter:
    """Delivers `notify` commands over two channels: ntfy topics (configured in
    Settings → Adapters) and Web Push to the DIDA PWA (subscriptions the API
    stores per user in push_subscriptions). Both converge on the same
    `notify:<who>` entities — an automation doesn't know or care which channel
    reaches the phone — and `notify:all` broadcasts to everyone. WRITE-only:
    there is no device state. Implements `dida_core.Adapter`."""

    name = NAMESPACE

    # A provisioned phone reports on movement or a 15-minute heartbeat, so this
    # much silence means its location engine is not running at all.
    _WAKE_AFTER_S = 2 * 3600
    # Retry slowly. The phone may be switched off, abroad without data, or
    # force-stopped (FCM cannot reach a force-stopped app at all), and FCM keeps
    # only the last message for an offline device — hammering buys nothing.
    _WAKE_EVERY_S = 3600
    # An FcmService older than this renders ANY data message as a notification,
    # so a wake it cannot act on just buzzes the phone — hourly, for a fault its
    # owner cannot fix from the notification shade. Only wake a client that knows
    # the key; older ones need the app updated first, which the log says out loud.
    _WAKE_MIN_BUILD = 858

    def __init__(self) -> None:
        self._bus: Bus | None = None
        self._server = "https://ntfy.sh"
        self._topics: dict[str, str] = {}
        self._webpush: dict[str, list[dict]] = {}  # entity_id -> subscription_info list
        self._fcm: dict[str, list[str]] = {}  # entity_id -> FCM token list
        self._fcm_rows: list[dict] = []  # username, token, user_agent
        self._fcm_key: str | None = None
        self._fcm_sender = None  # fcm.FcmSender | None
        self._vapid_priv: str | None = None
        self._contact = DEFAULT_CONTACT
        self._last_error: str | None = None  # last failed send, shown in the status badge
        self._woke: dict[str, float] = {}  # username -> monotonic time of the last wake push
        self._cfg: AdapterConfig | None = None
        self.broker = None
        self._session = None

    async def start(self, bus: Bus) -> None:
        import aiohttp

        self._bus = bus
        self._session = aiohttp.ClientSession()
        self._cfg = AdapterConfig("notify", self.broker)
        await bus.nc.subscribe(CTL_SUBJECT, cb=self._on_ctl)
        # Idle adapter (sends arrive as commands); poll so UI edits to the server
        # or target list — and new web-push subscriptions — apply live.
        prev: str | None = None
        while True:
            try:
                await self._cfg.load()
                self._server = (self._cfg.get("server") or "https://ntfy.sh").rstrip("/")
                self._topics = _parse_targets(self._cfg.get("targets"))
                await self._load_routes()
                await self._announce_targets()
                summary = (
                    f"{self._server}|{','.join(sorted(self._topics))}"
                    f"|{','.join(f'{k}:{len(v)}' for k, v in sorted(self._webpush.items()))}"
                    f"|{','.join(f'{k}:{len(v)}' for k, v in sorted(self._fcm.items()))}"
                )
                if summary != prev:
                    log.info(
                        "notify adapter — server=%s, ntfy: %s, web push: %s, fcm: %s",
                        self._server,
                        ", ".join(sorted(self._topics)) or "(none)",
                        ", ".join(f"{k} ({len(v)})" for k, v in sorted(self._webpush.items())) or "(none)",
                        ", ".join(f"{k} ({len(v)})" for k, v in sorted(self._fcm.items())) or "(none)",
                    )
                    prev = summary
                n_push = sum(len(v) for v in self._webpush.values())
                n_fcm = sum(len(v) for v in self._fcm.values())
                if n_push and not self._vapid_priv:
                    # Subscriptions exist but the VAPID key is missing/undecryptable —
                    # EVERY web push would be silently dropped, so fail loud instead of
                    # a green badge (the api log names the key it could not decrypt).
                    self.status.error(f"{n_push} web push subs but VAPID key missing")
                elif n_fcm and self._fcm_sender is None:
                    # Same failure shape for the third channel: registered phones but
                    # no usable service-account key means every FCM send is dropped.
                    self.status.error(f"{n_fcm} fcm tokens but service-account key missing")
                elif self._topics or n_push or n_fcm:
                    detail = f"{len(self._topics)} ntfy · {n_push} web push · {n_fcm} fcm"
                    if self._last_error:
                        detail += f" · last send: {self._last_error}"
                    self.status.ok(detail)
                else:
                    self.status.idle("no targets")
                await self._wake_dark_phones()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("notify: config poll error")
            await asyncio.sleep(10)

    def routes(self) -> list[str]:
        """Targets a notification would actually leave this adapter for — a channel
        with somewhere to send AND the key to send it with."""
        ids = set(self._topics)
        if self._vapid_priv:
            ids |= {eid for eid, subs in self._webpush.items() if subs}
        if self._fcm_sender is not None:
            ids |= {eid for eid, tokens in self._fcm.items() if tokens}
        return sorted(ids)

    async def _on_ctl(self, msg) -> None:
        try:
            action = json.loads(msg.data or b"{}").get("action")
        except ValueError:
            action = None
        reply = {"routes": self.routes()} if action == "routes" else {"error": f"unknown action {action!r}"}
        if msg.reply and self._bus is not None:
            await self._bus.nc.publish(msg.reply, json.dumps(reply).encode())

    async def _wake_dark_phones(self) -> None:
        """Re-arm the location engine on a phone that has stopped reporting.

        The engine starts only from the app's own onResume or a reboot, so a
        phone that goes dark stays dark until somebody picks it up — no use at
        all when the person is in another country. A silent data push reaches
        the app without them touching it and tells it to arm. The app answers on
        /app/location-status, so a phone that CANNOT arm (background grant
        missing) stays distinguishable from one that never got the message.

        Latitude is the honest freshness signal: only the GPS reporters ever
        write it. The network adapters keep `location` current for a phone whose
        tracker is long dead, which is exactly how one went unnoticed for weeks.
        """
        if self._fcm_sender is None or self._session is None or not self._fcm:
            return
        rows = self._fcm_rows
        if not rows:
            return
        # Only phones whose location this house actually tracks. Someone can hold
        # the app for notifications alone and never have been a presence entity —
        # waking their phone would be a ping about a thing nobody watches.
        tracked = {r["entity_id"] for r in await self.broker.call("entities", prefix="presence:")}
        silent_for: dict[str, float] = {}
        for r in await self.broker.call("state", prefix="presence:", capabilities=["latitude", "longitude"]):
            eid = r["entity_id"]
            silent_for[eid] = min(silent_for.get(eid, r["age"]), r["age"])
        mono = time.monotonic()
        for row in rows:
            user = row["username"]
            last_wake = self._woke.get(user)
            if last_wake is not None and mono - last_wake < self._WAKE_EVERY_S:
                continue
            eid = f"presence:{slug(user)}"
            if eid not in tracked:
                continue
            age = silent_for.get(eid)
            if age is not None and age < self._WAKE_AFTER_S:
                continue
            self._woke[user] = mono
            silent = "never" if age is None else f"{int(age / 3600)}h"
            build = _app_build(row["user_agent"])
            if build < self._WAKE_MIN_BUILD:
                log.info(
                    "notify: %s has no GPS for %s but its app (build %s) predates the "
                    "wake handler — it must be updated before it can be armed remotely",
                    user, silent, build or "unknown",
                )
                continue
            await self._wake_phone(user, row["token"], silent)

    async def _wake_phone(self, user: str, token: str, silent: str) -> None:
        try:
            result = await self._fcm_sender.send_data(
                self._session, token, {"type": "wake_location"}
            )
        except Exception as exc:
            log.warning("notify: wake push to %s failed: %s", user, exc, exc_info=True)
            return
        if result == "gone":
            await self.broker.call("push_prune", fcm_token=token)
            log.info("notify: pruned dead FCM token while waking %s", user)
        elif result.startswith("error:"):
            log.warning("notify: wake push to %s errored: %s", user, result[6:])
        else:
            log.info("notify: wake push sent to %s (no GPS for %s)", user, silent)

    async def _announce_targets(self) -> None:
        """Register each notification target as an entity so it's visible in the
        UI and pickable in the automation builder — the same way `announce`
        speakers show up. WRITE-only (`notify` has no state), so it's announced
        via EntityInfo, not a StateUpdate. `notify:all` appears once there is at
        least one target to fan out to."""
        if self._bus is None:
            return
        ids = set(self._topics) | set(self._webpush) | set(self._fcm)
        if ids:
            ids.add(ALL_ENTITY)
        for eid in ids:
            who = eid.split(":", 1)[1]
            name = "Svi" if eid == ALL_ENTITY else who.replace("_", " ").title()
            await self._bus.publish_entity(EntityInfo(
                entity_id=eid, adapter=NAMESPACE, capabilities=["notify"], name=name,
            ))

    async def _load_routes(self) -> None:
        """Refresh push routing: per-user web-push subscriptions and app tokens
        (owned by the API) + both signing keys."""
        routes = await self.broker.call("push_routes")
        self._vapid_priv = routes["vapid_private"] or None
        self._contact = routes["contact"] or DEFAULT_CONTACT
        webpush: dict[str, list[dict]] = {}
        for r in routes["webpush"]:
            webpush.setdefault(f"{NAMESPACE}:{slug(r['username'])}", []).append(
                {"endpoint": r["endpoint"], "keys": {"p256dh": r["p256dh"], "auth": r["auth"]}}
            )
        self._webpush = webpush
        key = routes["fcm_service_account"] or None
        if key != self._fcm_key:
            # Rebuilt only on a new key: the sender caches its OAuth access token.
            self._fcm_key = key
            self._fcm_sender = None
            if key:
                from . import fcm
                try:
                    self._fcm_sender = fcm.FcmSender(json.loads(key))
                except Exception as exc:  # bad/rotated key — loud, not fatal
                    log.error("notify: FCM service account unusable: %s", exc, exc_info=True)
        self._fcm_rows = routes["fcm"]
        tokens: dict[str, list[str]] = {}
        for r in self._fcm_rows:
            tokens.setdefault(f"{NAMESPACE}:{slug(r['username'])}", []).append(r["token"])
        self._fcm = tokens

    async def handle_command(self, command: Command) -> None:
        try:
            validate_command(command.capability, command.command)
        except CapabilityError as exc:
            raise CommandRejected(f"bad command: {exc}") from exc
        if command.command != "notify":
            raise CommandRejected(f"notify has no {command.command}")
        # `message` is canonical; `value` is what the automations builder's
        # generic single-value action form collects (same fallback as announce).
        message = str(command.args.get("message") or command.args.get("value") or "")
        title = command.args.get("title")
        def webpush_subs(eid: str) -> list[dict]:
            # A phone with the app installed gets the native notification; the
            # same alert to that user's browser would just buzz them twice. Web
            # push stays the route for app-less users (iPhone/desktop PWA) and
            # resumes by itself if the app goes (token pruned / key unusable).
            if self._fcm_sender is not None and self._fcm.get(eid):
                return []
            return self._webpush.get(eid, [])

        if command.entity_id == ALL_ENTITY:
            topics = list(dict.fromkeys(self._topics.values()))
            subs = [s for eid in self._webpush for s in webpush_subs(eid)]
            fcm_tokens = list({t for v in self._fcm.values() for t in v})
        else:
            topic = self._topics.get(command.entity_id)
            topics = [topic] if topic else []
            subs = webpush_subs(command.entity_id)
            fcm_tokens = self._fcm.get(command.entity_id, [])
        # A camera named on the command turns the notification into a picture: the
        # api mints one frame behind an expiring link (it owns the descriptors and
        # the credentials; we only ever learn a path). Never fatal — a notification
        # that loses its picture still has to arrive.
        image = await self._snapshot_url(str(command.args.get("camera") or ""))
        if not topics and not subs and not (fcm_tokens and self._fcm_sender):
            # Ours but unroutable (renamed/removed target, typo in an automation) —
            # a silently vanished NOTIFICATION is the worst possible artifact here.
            raise CommandRejected(f"no target, dropping {message[:60]!r}")
        # Fan out concurrently: a notify:all to N phones is N independent round-
        # trips (ntfy POST up to 8 s, web push up to its own timeout). Serially
        # they'd sum; gathered they overlap. Each send handles its own errors
        # (status badge + prune) and says whether it delivered; return_exceptions=True
        # is a belt so one unexpected raise can't cancel the siblings.
        sent = await asyncio.gather(
            *(self._send_ntfy(t, message, title, image) for t in topics),
            *(self._send_webpush(sub, message, title, image) for sub in subs),
            *(self._send_fcm(tok, message, title, image) for tok in fcm_tokens),
            return_exceptions=True,
        )
        if not any(r is True for r in sent):
            raise CommandRejected(f"no route delivered {message[:60]!r}: {self._last_error}")

    async def _send_fcm(self, token: str, message: str, title: object,
                        image: str | None = None) -> bool:
        if self._fcm_sender is None or self._session is None:
            return False
        # Like ntfy, the phone fetches the frame itself with no DIDA session, so
        # it needs the absolute (tunnel) URL, not the same-origin path.
        base = await self._app_url() if image else ""
        try:
            result = await self._fcm_sender.send(
                self._session, token, str(title) if title else "DIDA", message,
                image=f"{base}{image}" if image and base else None,
            )
        except Exception as exc:
            self._last_error = f"fcm: {exc}"
            log.warning("notify: FCM send failed: %s", exc, exc_info=True)
            return False
        if result == "gone":
            # FCM says this registration token is dead — prune it so the roster
            # stays clean (mirrors web-push 404/410 pruning).
            await self.broker.call("push_prune", fcm_token=token)
            log.info("notify: pruned dead FCM token")
            return False
        if result.startswith("error:"):
            self._last_error = f"fcm: {result[6:]}"
            log.warning("notify: FCM send error: %s", result[6:])
            return False
        log.info("notify -> fcm %s…: %r", token[:12], message[:60])
        self._last_error = None
        return True

    async def _app_url(self) -> str:
        """This installation's public origin — Settings → Network owns it."""
        from dida_core import host_setting

        return (await host_setting(self.broker, "app_url")).rstrip("/")

    async def _snapshot_url(self, camera: str) -> str | None:
        """Ask the api for one frame from `camera` (a camera entity, or any of its
        children — an automation's trigger usually holds a zone or the bell)."""
        if not camera or self._bus is None:
            return None
        try:
            resp = await self._bus.nc.request(
                SNAP_SUBJECT, json.dumps({"entity_id": camera}).encode(), timeout=8
            )
            url = json.loads(resp.data).get("url")
            return url if isinstance(url, str) and url else None
        except Exception as exc:
            log.warning("notify: no snapshot for %s: %s", camera, exc, exc_info=True)
            return None

    async def _send_ntfy(self, topic: str, message: str, title: object,
                         image: str | None = None) -> bool:
        if self._session is None:
            return False
        import aiohttp

        headers: dict[str, str] = {}
        if title:
            # ntfy headers must be latin-1; encode any other chars defensively.
            headers["Title"] = str(title).encode("utf-8").decode("latin-1", "replace")
        base = await self._app_url()
        if image and base:
            # ntfy fetches an attachment itself, so it needs an absolute URL — it is
            # not the phone following a same-origin path.
            headers["Attach"] = f"{base}{image}"
        try:
            async with self._session.post(
                f"{self._server}/{topic}", data=message.encode("utf-8"), headers=headers,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status >= 400:
                    log.warning("notify ntfy %s -> HTTP %s", topic, resp.status)
                    self._last_error = f"ntfy HTTP {resp.status}"
                    return False
                log.info("notify -> %s/%s: %r", self._server, topic, message[:60])
                self._last_error = None
                return True
        except Exception as exc:
            log.warning("notify ntfy send to %s failed: %s", topic, exc, exc_info=True)
            self._last_error = f"ntfy: {exc}"
            return False

    async def _send_webpush(self, sub: dict, message: str, title: object,
                            image: str | None = None) -> bool:
        """One Web Push send. A 404/410 from the push service means the browser
        dropped the subscription (app uninstalled, permission revoked) — the row
        is pruned so delivery stays clean instead of erroring forever."""
        if not self._vapid_priv:
            log.warning("notify: web push subscription exists but the VAPID key is "
                        "missing/undecryptable — see the api log (api generates the key)")
            self._last_error = "web push: VAPID key missing"
            return False
        from pywebpush import WebPushException, webpush

        payload = json.dumps({"title": str(title or "DIDA"), "message": message,
                              **({"image": image} if image else {})})
        try:
            await asyncio.to_thread(
                webpush,
                subscription_info=sub,
                data=payload,
                vapid_private_key=self._vapid_priv,
                vapid_claims={"sub": self._contact},
                ttl=WEBPUSH_TTL_S,
            )
            await self.broker.call("push_ok", endpoint=sub["endpoint"])
            log.info("notify -> web push %s…: %r", sub["endpoint"][:40], message[:60])
            self._last_error = None
            return True
        except WebPushException as exc:
            code = getattr(exc.response, "status_code", None)
            if code in (404, 410):
                await self.broker.call("push_prune", endpoint=sub["endpoint"])
                log.info("notify: pruned dead web push subscription (HTTP %s)", code)
            else:
                log.warning("notify web push failed (HTTP %s): %s", code, exc)
                self._last_error = f"web push HTTP {code}" if code else f"web push: {exc}"
        except Exception as exc:
            log.warning("notify web push failed: %s", exc, exc_info=True)
            self._last_error = f"web push: {exc}"
        return False

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
