"""Publish the car-app AABs to the Play internal testing track.

Run by android/build.sh --auto --publish inside the dida/api container (it has
httpx + cryptography; no Google SDK needed — the OAuth2 JWT-bearer flow is ~20
lines). Auth: the play-publisher service account key in keystore/ ("Release to
testing tracks" on each app). Per app: create edit -> upload bundle -> point the
internal track at that versionCode -> commit. A versionCode Play has already
seen is reported and skipped, not an error — re-running after a no-change build
must stay green. Everything else fails loud with the API's own message.
"""

from __future__ import annotations

import base64
import glob
import json
import sys
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

ROOT = Path(__file__).parent
# Only Play-distributed apps. DIDA Auto is on Play by force (AA IoT templated
# apps cannot sideload). DIDA Music is self-hosted (media sideloads) — served
# from /api/app + in-app self-update — so it is NOT published here.
APPS = {"auto": "biz.boskovic.dida.auto"}
API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
UPLOAD = "https://androidpublisher.googleapis.com/upload/androidpublisher/v3/applications"
SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def access_token(client: httpx.Client) -> str:
    # keystore/ holds more than one service account (the Firebase Admin key for
    # FCM lives here too) — select the Play publisher by its email, not by glob.
    keys = [
        p for p in glob.glob(str(ROOT / "keystore" / "*.json"))
        if (sa := json.loads(Path(p).read_text())).get("type") == "service_account"
        and sa.get("client_email", "").startswith("play-publisher@")
    ]
    if len(keys) != 1:
        sys.exit(f"expected exactly one play-publisher key in keystore/, found {len(keys)}")
    sa = json.loads(Path(keys[0]).read_text())
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({
        "iss": sa["client_email"], "scope": SCOPE, "aud": sa["token_uri"],
        "iat": now, "exp": now + 600,
    }).encode())
    signing_input = f"{header}.{claims}".encode()
    key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
    jwt = f"{header}.{claims}." + _b64(key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256()))
    r = client.post(sa["token_uri"], data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt,
    })
    if r.status_code != 200:
        sys.exit(f"token exchange failed: {r.status_code} {r.text}")
    return r.json()["access_token"]


def publish(client: httpx.Client, headers: dict, module: str, pkg: str) -> None:
    aab = ROOT / "dist" / f"dida-{module}.aab"
    meta = ROOT / "dist" / f"{module}.json"
    if not aab.is_file() or not meta.is_file():
        sys.exit(f"{module}: dist artifacts missing — run build.sh --auto first")
    version = json.loads(meta.read_text())
    code, name = version["versionCode"], version["versionName"]

    r = client.post(f"{API}/{pkg}/edits", headers=headers)
    if r.status_code == 404:
        sys.exit(f"{module}: app {pkg} not found on Play — create it in the Console "
                 f"and grant the service account access to it first")
    if r.status_code != 200:
        sys.exit(f"{module}: edit create failed: {r.status_code} {r.text}")
    edit = r.json()["id"]

    r = client.post(
        f"{UPLOAD}/{pkg}/edits/{edit}/bundles?uploadType=media",
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=aab.read_bytes(),
        timeout=300,
    )
    if r.status_code != 200:
        if "already been used" in r.text or "already exists" in r.text:
            print(f"{module}: versionCode {code} already on Play — nothing to publish")
            return
        sys.exit(f"{module}: bundle upload failed: {r.status_code} {r.text}")

    r = client.put(
        f"{API}/{pkg}/edits/{edit}/tracks/internal",
        headers=headers,
        json={"track": "internal",
              "releases": [{"name": name, "versionCodes": [str(code)], "status": "completed"}]},
    )
    if r.status_code != 200:
        sys.exit(f"{module}: track update failed: {r.status_code} {r.text}")

    r = client.post(f"{API}/{pkg}/edits/{edit}:commit", headers=headers)
    if r.status_code != 200:
        sys.exit(f"{module}: commit failed: {r.status_code} {r.text}")
    print(f"{module}: published {name} (versionCode {code}) to internal testing")


def main() -> None:
    modules = sys.argv[1:] or list(APPS)
    with httpx.Client(timeout=60) as client:
        headers = {"Authorization": f"Bearer {access_token(client)}"}
        for module in modules:
            publish(client, headers, module, APPS[module])


if __name__ == "__main__":
    main()
