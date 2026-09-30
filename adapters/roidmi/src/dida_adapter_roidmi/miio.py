"""Minimal miio (Xiaomi home) binary protocol — MiOT methods over UDP.

The wire format is small enough to own outright: a 32-byte header, AES-128-CBC
payload encryption with keys derived from the device token, and a JSON-RPC
body. python-miio would pull a dependency chain (pydantic v1, netifaces) that
no longer builds on our Python; this speaks the same protocol with what
dida/base already ships (cryptography).

Header (all big-endian):

    0x2131 | length u16 | unknown u32 | device_id u32 | stamp u32 | md5[16]

* Handshake: a "hello" packet (length 0x20, everything after the magic 0xff).
  The reply carries the device id and its uptime stamp; every request must
  quote a stamp >= the device's current one, so the session tracks
  (stamp, monotonic-at-handshake) and re-handshakes when it gets old.
* Encryption: key = md5(token), iv = md5(key + token); payload is the JSON
  body, PKCS7-padded. The checksum field is md5(header[:16] + token + payload).

All I/O here is BLOCKING (plain UDP socket with a timeout) — the adapter calls
it via asyncio.to_thread. One in-flight request at a time per client.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

_HELLO = bytes.fromhex("21310020") + b"\xff" * 28
_MAGIC = 0x2131
# Re-handshake when the session is older than this: the stamp we quote drifts
# from the device clock, and a device reboot invalidates the old device id.
_SESSION_TTL = 60.0


class MiioError(Exception):
    """Protocol or device error — the poll loop reports it on the status badge."""


class MiioClient:
    def __init__(self, host: str, token_hex: str, timeout: float = 5.0) -> None:
        try:
            self._token = bytes.fromhex(token_hex.strip())
        except ValueError as exc:
            raise MiioError("token is not hex") from exc
        if len(self._token) != 16:
            raise MiioError("token must be 32 hex chars")
        self._addr = (host, 54321)
        self._timeout = timeout
        self._key = hashlib.md5(self._token).digest()
        self._iv = hashlib.md5(self._key + self._token).digest()
        self._sock: socket.socket | None = None
        self._device_id = b""
        self._stamp = 0
        self._session_at = 0.0     # monotonic of the last successful handshake
        self._id = 0               # JSON-RPC id, wraps like python-miio's

    # --- wire helpers ------------------------------------------------------

    def _encrypt(self, plain: bytes) -> bytes:
        padder = PKCS7(128).padder()
        padded = padder.update(plain) + padder.finalize()
        enc = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).encryptor()
        return enc.update(padded) + enc.finalize()

    def _decrypt(self, blob: bytes) -> bytes:
        dec = Cipher(algorithms.AES(self._key), modes.CBC(self._iv)).decryptor()
        padded = dec.update(blob) + dec.finalize()
        unpadder = PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    def _socket(self) -> socket.socket:
        if self._sock is None:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.settimeout(self._timeout)
        return self._sock

    def _handshake(self) -> None:
        sock = self._socket()
        sock.sendto(_HELLO, self._addr)
        data, _ = sock.recvfrom(1024)
        if len(data) < 32 or struct.unpack(">H", data[:2])[0] != _MAGIC:
            raise MiioError("malformed handshake reply")
        self._device_id = data[8:12]
        self._stamp = struct.unpack(">I", data[12:16])[0]
        self._session_at = time.monotonic()

    def _build(self, payload: bytes) -> bytes:
        stamp = self._stamp + int(time.monotonic() - self._session_at)
        head = struct.pack(">HHI", _MAGIC, 32 + len(payload), 0) + self._device_id + struct.pack(">I", stamp)
        checksum = hashlib.md5(head + self._token + payload).digest()
        return head + checksum + payload

    def _parse(self, data: bytes) -> dict:
        if len(data) < 32:
            raise MiioError("short reply")
        if len(data) == 32:
            raise MiioError("empty reply (bad token?)")
        payload = data[32:]
        head = data[:16]
        if hashlib.md5(head + self._token + payload).digest() != data[16:32]:
            raise MiioError("reply checksum mismatch (wrong token)")
        plain = self._decrypt(payload).rstrip(b"\x00")
        try:
            return json.loads(plain.decode("utf-8", "replace"))
        except ValueError as exc:
            raise MiioError(f"unparseable reply: {plain[:80]!r}") from exc

    # --- public API (blocking; run via asyncio.to_thread) ------------------

    def request(self, method: str, params: object) -> object:
        """One JSON-RPC round-trip, with a re-handshake retry on timeout."""
        last: Exception | None = None
        for _attempt in (1, 2):
            try:
                if time.monotonic() - self._session_at > _SESSION_TTL:
                    self._handshake()
                self._id = self._id % 9999 + 1
                body = json.dumps({"id": self._id, "method": method, "params": params}).encode()
                sock = self._socket()
                sock.sendto(self._build(self._encrypt(body)), self._addr)
                # The device occasionally re-acks an older id; read until ours.
                deadline = time.monotonic() + self._timeout
                while True:
                    if time.monotonic() > deadline:
                        raise TimeoutError("no matching reply")
                    data, _ = sock.recvfrom(65507)
                    reply = self._parse(data)
                    if reply.get("id") == self._id:
                        break
                if "error" in reply:
                    raise MiioError(f"device error: {reply['error']}")
                return reply.get("result")
            except (TimeoutError, OSError) as exc:
                last = exc
                self._session_at = 0.0  # force a fresh handshake on retry
        raise MiioError(f"no reply from {self._addr[0]} ({last})")

    def get_properties(self, props: list[dict]) -> list[dict]:
        result = self.request("get_properties", props)
        if not isinstance(result, list):
            raise MiioError(f"get_properties returned {type(result).__name__}")
        return result

    def set_property(self, siid: int, piid: int, value: object) -> None:
        self.request("set_properties", [{"did": f"set-{siid}-{piid}", "siid": siid, "piid": piid, "value": value}])

    def action(self, siid: int, aiid: int, params: list | None = None) -> None:
        self.request("action", {"did": f"call-{siid}-{aiid}", "siid": siid, "aiid": aiid, "in": params or []})

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self._session_at = 0.0
