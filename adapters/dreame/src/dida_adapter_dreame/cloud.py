"""Dreamehome cloud client (the only control path this robot has).

A Dreame robot registered in the Dreamehome app DISABLES its local API — no
miio handshake, every port closed — so unlike the Roidmi there is nothing to
talk to on the LAN. Everything goes through Dreame's own account API: an OAuth
password grant, then MIoT get_properties / set_properties / action calls
tunnelled through one `sendCommand` endpoint.

Synchronous by design (stdlib urllib), driven from the adapter through
asyncio.to_thread — same shape as the miio client it replaces.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("dida.adapter.dreame")

# Fixed client credentials of the Dreamehome iOS app: the password salt and the
# app's Basic auth. They are constants of the protocol, not user secrets.
_SALT = "RAylYC%fmSKp7%Tq"
_BASIC = "Basic ZHJlYW1lX2FwcHYxOkFQXmR2QHpAU1FZVnhOODg="
_UA = "Dreame_Smarthome/2.1.9 (iPhone; iOS 18.4.1; Scale/3.00)"
_PORT = 13267
COUNTRIES = ("eu", "cn", "us", "ru", "sg")
# Refresh this long before the token's stated expiry (it lives 2 h).
_RENEW_MARGIN = 120.0


class DreameCloudError(Exception):
    """Any failure of the cloud path — auth, transport or a non-zero API code."""


class DreameCloud:
    def __init__(self, email: str, password: str, country: str = "eu", *, timeout: float = 20.0) -> None:
        self._email = email
        self._password = password
        self._country = country if country in COUNTRIES else "eu"
        self._timeout = timeout
        self._token = ""
        self._refresh_token = ""
        self._tenant = "000000"
        self._expires_at = 0.0
        self._did = ""
        self._model = ""
        self._uid = ""
        self._broker: tuple[str, int] | None = None
        self._suffix = ""      # per-device shard of the command endpoint
        self._id = 0

    @property
    def device_id(self) -> str:
        return self._did

    @property
    def model(self) -> str:
        return self._model

    @property
    def uid(self) -> str:
        return self._uid

    @property
    def token(self) -> str:
        self._ensure_token()
        return self._token

    @property
    def broker(self) -> tuple[str, int]:
        """Their push broker, per device: the record's bindDomain IS host:port."""
        if not self._broker:
            raise DreameCloudError("no broker for this device")
        return self._broker

    def _url(self, path: str) -> str:
        return f"https://{self._country}.iot.dreame.tech:{_PORT}{path}"

    def _post(self, path: str, data: bytes | None, headers: dict[str, str]) -> dict:
        req = urllib.request.Request(self._url(path), data=data, method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise DreameCloudError(f"HTTP {exc.code} on {path}: {detail}") from exc
        except OSError as exc:
            raise DreameCloudError(f"{path}: {exc}") from exc
        try:
            return json.loads(body or b"{}")
        except ValueError as exc:
            raise DreameCloudError(f"{path}: undecodable reply") from exc

    # ── auth ──────────────────────────────────────────────────────────────────

    def _grant(self, form: str) -> dict:
        return self._post("/dreame-auth/oauth/token", form.encode(), {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept-Language": "en-US;q=0.8",
            "User-Agent": _UA,
            "Authorization": _BASIC,
            "Tenant-Id": self._tenant,
        })

    def _accept(self, data: dict) -> None:
        token = data.get("access_token")
        if not token:
            raise DreameCloudError(f"login rejected: {json.dumps(data)[:200]}")
        self._token = token
        self._refresh_token = data.get("refresh_token", "")
        self._tenant = data.get("tenant_id") or self._tenant
        self._uid = str(data.get("uid") or self._uid)
        self._expires_at = time.time() + float(data.get("expires_in", 7200)) - _RENEW_MARGIN

    def login(self) -> None:
        digest = hashlib.md5((self._password + _SALT).encode()).hexdigest()
        self._accept(self._grant(
            "platform=IOS&scope=all&grant_type=password"
            f"&username={urllib.parse.quote(self._email)}&password={digest}&type=account"
        ))
        log.info("dreame: logged in to the %s cloud", self._country)

    def _ensure_token(self) -> None:
        if self._token and time.time() < self._expires_at:
            return
        if self._refresh_token:
            try:
                self._accept(self._grant(
                    f"platform=IOS&scope=all&grant_type=refresh_token&refresh_token={self._refresh_token}"))
                return
            except DreameCloudError as exc:
                # A refused refresh is normal after a long outage; fall through to
                # a full login rather than reporting the adapter as broken.
                log.info("dreame: refresh rejected (%s) — logging in again", exc)
                self._refresh_token = ""
        self.login()

    # ── api ───────────────────────────────────────────────────────────────────

    def _api(self, path: str, body: dict | None = None) -> dict:
        self._ensure_token()
        data = json.dumps(body, separators=(",", ":")).encode() if body is not None else None
        out = self._post(path, data, {
            "Accept": "*/*",
            "Content-Type": "application/json",
            "User-Agent": _UA,
            "Authorization": _BASIC,
            "Tenant-Id": self._tenant,
            "Dreame-Auth": self._token,
        })
        if out.get("code") != 0:
            raise DreameCloudError(f"{path}: code {out.get('code')} {out.get('msg', '')}".strip())
        return out.get("data") or {}

    def devices(self) -> list[dict]:
        data = self._api("/dreame-user-iot/iotuserbind/device/listV2")
        return list(((data.get("page") or {}).get("records")) or [])

    def bind(self, record: dict) -> None:
        """Pin the client to one robot. The command endpoint is sharded per device
        (the record's bindDomain names the shard); talking to the wrong shard
        answers with an empty result instead of an error."""
        self._did = str(record.get("did") or "")
        self._model = str(record.get("model") or "")
        domain = str(record.get("bindDomain") or "")
        self._suffix = f"-{domain.split('.')[0]}" if domain else ""
        if ":" in domain:
            host, _, port = domain.partition(":")
            self._broker = (host, int(port))
        if not self._did:
            raise DreameCloudError("device record without a did")

    def _send(self, method: str, params: object) -> object:
        if not self._did:
            raise DreameCloudError("no device bound")
        self._id += 1
        payload = {"did": self._did, "id": self._id,
                   "data": {"did": self._did, "id": self._id, "method": method, "params": params}}
        data = self._api(f"/dreame-iot-com{self._suffix}/device/sendCommand", payload)
        return data.get("result")

    def get_properties(self, props: list[dict]) -> list[dict]:
        params = [{"did": self._did, "siid": p["siid"], "piid": p["piid"]} for p in props]
        result = self._send("get_properties", params)
        if not isinstance(result, list):
            raise DreameCloudError("get_properties returned no result")
        # Re-attach the caller's labels: the reply preserves request order but
        # carries only siid/piid, and the mapping layer keys off the label.
        out = []
        for prop, row in zip(props, result, strict=False):
            row = dict(row)
            row["did"] = prop["did"]
            out.append(row)
        return out

    def action(self, siid: int, aiid: int, params: list[dict] | None = None) -> None:
        self._send("action", {"did": self._did, "siid": siid, "aiid": aiid, "in": params or []})

    def set_property(self, siid: int, piid: int, value: object) -> None:
        self._send("set_properties", [{"did": self._did, "siid": siid, "piid": piid, "value": value}])

    # ── the map file ──────────────────────────────────────────────────────────
    #
    # The map is not a property: the property names an object in their object
    # store, and a second call trades that name for a signed URL. (The other file
    # endpoint, getOss1dDownloadUrl, prefixes the account and device itself and
    # answers 404 for a name that already carries them.)

    def map_stamp(self, siid: int, piid: int) -> tuple[str, str]:
        """The map's object name and its checksum, without downloading anything.

        The checksum is how we know the house has been remapped or a room renamed:
        the property is one small call, the file behind it is not."""
        result = self._send("get_properties", [{"did": self._did, "siid": siid, "piid": piid}])
        if not isinstance(result, list) or not result or result[0].get("code") != 0:
            raise DreameCloudError("the robot did not answer with its map")
        try:
            info = json.loads(result[0]["value"])
            return str(info["object_name"]), str(info.get("md5") or "")
        except Exception as exc:
            raise DreameCloudError(f"map property carries no object name: {exc}") from exc

    def map_file(self, siid: int, piid: int) -> bytes:
        name, _md5 = self.map_stamp(siid, piid)

        data = self._api("/dreame-user-iot/iotfile/getDownloadUrl",
                         {"did": self._did, "model": self._model, "filename": name,
                          "region": self._country})
        url = data.get("url") if isinstance(data, dict) else data
        if not isinstance(url, str) or not url.startswith("https://"):
            raise DreameCloudError("no download url for the map")
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return resp.read()
        except OSError as exc:
            raise DreameCloudError(f"map download failed: {exc}") from exc
