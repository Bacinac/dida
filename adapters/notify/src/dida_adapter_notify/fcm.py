"""FCM HTTP v1 sender for the notify adapter — native push to the Android app.

A service-account key (stored Fernet-encrypted in app_settings, imported once
with scripts/set_fcm_sa.py) is exchanged for a short-lived OAuth token via the
JWT-bearer grant, then used to POST data-only messages to
`/v1/projects/<id>/messages:send`. Data-only (not `notification`) so the app's
FcmService renders every message the same way, foreground or not. The caller
prunes a token this reports GONE.
"""

from __future__ import annotations

import base64
import json
import time

import aiohttp
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

_TOKEN_URI = "https://oauth2.googleapis.com/token"
_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class FcmSender:
    """Holds one project's service account + a cached access token."""

    def __init__(self, sa: dict) -> None:
        self.project_id: str = sa["project_id"]
        self._email: str = sa["client_email"]
        self._key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
        self._token: str | None = None
        self._exp: float = 0.0

    async def _access_token(self, session: aiohttp.ClientSession) -> str:
        now = time.time()
        if self._token and now < self._exp - 60:
            return self._token
        header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64(json.dumps({
            "iss": self._email, "scope": _SCOPE, "aud": _TOKEN_URI,
            "iat": int(now), "exp": int(now) + 3600,
        }).encode())
        signing_input = f"{header}.{claims}".encode()
        sig = self._key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = f"{header}.{claims}.{_b64(sig)}"
        async with session.post(_TOKEN_URI, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion,
        }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            body = await resp.json()
        self._token = body["access_token"]
        self._exp = now + int(body.get("expires_in", 3600))
        return self._token

    async def send(
        self, session: aiohttp.ClientSession, token: str,
        title: str, message: str, image: str | None = None, url: str | None = None,
    ) -> str:
        """Returns "ok", "gone" (prune this token), or "error:<detail>"."""
        data = {"title": title or "DIDA", "message": message}
        if image:
            data["image"] = image
        if url:
            data["url"] = url
        return await self.send_data(session, token, data)

    async def send_data(
        self, session: aiohttp.ClientSession, token: str, data: dict[str, str],
    ) -> str:
        """Raw data-only message. A payload the app acts on without rendering
        anything (no title/message keys) reaches it silently — that is how a
        phone is told to do something rather than shown something. Same result
        vocabulary as `send`."""
        access = await self._access_token(session)
        payload = {"message": {"token": token, "data": data, "android": {"priority": "HIGH"}}}
        async with session.post(
            f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send",
            json=payload, headers={"Authorization": f"Bearer {access}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                return "ok"
            text = await resp.text()
            if resp.status == 404 or "UNREGISTERED" in text or "NOT_FOUND" in text:
                return "gone"
            return f"error:{text[:200]}"
