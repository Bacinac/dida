"""Record every API response the real DIDA UI asks for, route by route.

Drives the actual app on the dev instance (which holds the anonymised
production snapshot) and captures each /api GET into a fixture map. The demo
build replays these, so the shipped frontend is the real one — only its
network layer is swapped.
"""
import contextlib
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

BASE = os.environ.get("DIDA_DEMO_UI", "http://localhost:5273")
USER = os.environ.get("DIDA_DEMO_USER", "admin")
PW = os.environ.get("DIDA_DEMO_PASS", "")
OUT = Path(os.environ.get("DIDA_DEMO_OUT", "/out"))

ROUTES = [
    "/floorplan", "/kamere", "/media", "/media/spotify", "/automations",
    "/automations/schedules", "/history", "/panel", "/ulaz", "/about", "/account",
    "/settings/adapters", "/settings/alerts", "/settings/areas", "/settings/backup",
    "/settings/commands", "/settings/devices", "/settings/floors", "/settings/helpers",
    "/settings/keys", "/settings/network", "/settings/panel", "/settings/retention",
    "/settings/scenes", "/settings/system", "/settings/translations", "/settings/users",
    "/settings/zones",
]


def scrub_cloudflare(fixtures: dict[str, object]) -> int:
    """Drop the tunnel's route entities from the recorded fixtures.

    Each Cloudflare route registers an entity named after its public hostname, so
    the list is the household's whole ingress inventory under one family domain —
    nothing a demo needs, and the privacy gate rightly refuses to publish it. The
    route names also reach the translation catalogue (adapter descriptors), which
    is why this scrubs three surfaces, not just /entities.
    """
    ents = fixtures.get("/entities")
    if not isinstance(ents, list):
        return 0
    names = {e.get("name") for e in ents if str(e.get("entity_id", "")).startswith("cloudflare:")}
    if not names:
        return 0
    dropped = 0
    for key, body in list(fixtures.items()):
        if not isinstance(body, list):
            continue
        path = key.split("?", 1)[0]
        if path in ("/entities", "/state"):
            keep = [r for r in body if not str(r.get("entity_id", "")).startswith("cloudflare:")]
        elif path == "/translations/keys":
            keep = [r for r in body if r.get("key") not in names]
        else:
            continue
        dropped += len(body) - len(keep)
        fixtures[key] = keep
    return dropped


def main() -> int:
    fixtures: dict[str, object] = {}
    misses: list[str] = []
    seen: list[str] = []          # every recorded response, in order — the liveness signal

    with sync_playwright() as p:
        b = p.chromium.launch()
        # Without this the browser answers repeat calls from cache (304, no
        # body) and those endpoints never get recorded — half the app then
        # ships empty.
        ctx = b.new_context(viewport={"width": 1440, "height": 900},
                            extra_http_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"})
        page = ctx.new_page()

        def on_response(res):
            u = urlsplit(res.url)
            if "/api/" not in u.path or res.request.method != "GET":
                return
            # Only successful bodies, and let later (authenticated) responses win:
            # the first /auth/me of the session is the pre-login 401.
            if res.status != 200:
                return
            key = u.path.split("/api", 1)[1] + (("?" + u.query) if u.query else "")
            seen.append(key)
            try:
                fixtures[key] = res.json()
            except ValueError:
                misses.append(f"{key} ({res.status})")

        page.on("response", on_response)

        page.goto(f"{BASE}/login", wait_until="networkidle")
        # A vite error overlay intercepts every pointer event; one reload clears a
        # transient HMR error. If it survives the reload, fail with the overlay's
        # own text instead of an opaque click timeout.
        if page.locator("vite-error-overlay").count():
            page.reload(wait_until="networkidle")
            if page.locator("vite-error-overlay").count():
                text = page.locator("vite-error-overlay").inner_text()[:400]
                raise RuntimeError(f"vite error overlay is up: {text}")
        page.fill("#u", USER)
        page.fill("#p", PW)
        page.click('button[type="submit"]')
        page.wait_for_url(lambda u: "/login" not in u, timeout=15000)

        # A route that never loads takes its endpoints with it, and the fixture map
        # says nothing about the hole: a busy box once published 31 of 40 endpoints
        # as if complete, leaving Media, Schedules and Entry blank in the demo. So
        # each route must be seen ANSWERING — at least one recorded response.
        #
        # Deliberately not "did it reach networkidle": the camera pages poll their
        # snapshots forever and never go idle, so they always time out while their
        # endpoints record perfectly well. Silence is the symptom, not the timeout.
        failed: list[str] = []
        for r in ROUTES:
            for attempt in (1, 2):
                before = len(seen)
                with contextlib.suppress(Exception):     # a poll-forever page never settles
                    page.goto(f"{BASE}{r}", wait_until="networkidle", timeout=25000)
                page.wait_for_timeout(1200)
                if len(seen) > before:
                    print(f"  {r}: {len(fixtures)} recorded", flush=True)
                    break
                print(f"  {r}: silent, attempt {attempt}/2", flush=True)
                if attempt == 2:
                    failed.append(r)

        ctx.close()
        b.close()

    if failed:
        print("REFUSING: routes never settled:", ", ".join(failed), file=sys.stderr)
        return 1

    print("cloudflare rows dropped:", scrub_cloudflare(fixtures))
    (OUT / "api-fixtures.json").write_text(json.dumps(fixtures))
    print("endpoints:", len(fixtures))
    print("non-JSON responses:", misses[:8] or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
