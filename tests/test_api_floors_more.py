"""Integration test — the floor-plan routes that test_api_floors.py leaves
uncovered: scaffold-image upload (415/400/413/404 guards, w/h parse + range
clamp, stale-type drop), the gated image serve (403 for a page-scoped login,
404 for no-image / missing-file, success), delete's image-unlink branch, and the
planvision-backed border endpoints (_scaffold_bytes 400s, detect/rooms/erase 502
when planvision is unreachable, save-borders persist + 404, erase-border arity).

Runs in the api image (dida_api + httpx); the runner supplies the ephemeral
Postgres (see tests/run.sh). Bypasses the app lifespan by wiring app.state
directly. `_DIR` is repointed at a scratch dir and `_PLANVISION` at a dead port
so nothing touches the real state mount or the network. rate_limit.LOGIN is reset
per-test by tests/conftest.py.
"""
import json
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import dida_api.app as appmod
import dida_api.floors as floorsmod
from dida_core import apply_migrations, jsonb_init, pg_pool
from home_core.auth import hash_password
from httpx import ASGITransport, AsyncClient

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64


class StubBus:
    async def publish_command(self, *a, **k):
        pass

    async def publish_event(self, *a, **k):
        pass


class _PlanvisionHandler(BaseHTTPRequestHandler):
    """A one-shot stub of planvision: 200 + a canned payload for any POST, so the
    detect/rooms/erase success paths (raise_for_status + resp.json passthrough) run."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0) or 0))
        payload = json.dumps({"borders": [[1, 2, 3, 4]], "removed": [], "rooms": [[[0, 0], [1, 1]]]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):  # keep the test output clean
        pass


def _wire(pool):
    appmod.app.state.pool = pool
    appmod.app.state.secret_key = "floors-more-test-secret-0123456789abcdef"
    appmod.app.state.bus = StubBus()


async def test_floors_image_lifecycle():
    """Upload → serve → border endpoints → delete, exercising every guard."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM floors WHERE key LIKE 'zzimg%'")
    await pool.execute("DELETE FROM users WHERE username = 'flradmin2'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('flradmin2', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    _wire(pool)

    orig_dir, orig_pv = floorsmod._DIR, floorsmod._PLANVISION
    tmp = Path(tempfile.mkdtemp(prefix="dida-fp-"))
    floorsmod._DIR = tmp
    floorsmod._PLANVISION = "http://127.0.0.1:1"  # closed port → ConnectError → 502

    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            r = await c.post("/auth/login", json={"username": "flradmin2", "password": "adminpw12"})
            assert r.status_code == 200, "admin login"

            r = await c.post("/floors", json={"name": "zzimg_floor"})
            assert r.status_code == 201
            fid, key = r.json()["id"], r.json()["key"]
            assert key == "zzimg-floor"

            # serve before any upload → 404 (img_path is NULL)
            r = await c.get(f"/floorplan/{key}")
            assert r.status_code == 404, "no-image floor serves 404"

            # upload: wrong content-type → 415
            r = await c.put(f"/floors/{fid}/image", content=PNG, headers={"content-type": "text/plain"})
            assert r.status_code == 415, "non-image content-type rejected"

            # upload: empty body → 400
            r = await c.put(f"/floors/{fid}/image", content=b"", headers={"content-type": "image/png"})
            assert r.status_code == 400, "empty body rejected"

            # upload: over the 25 MB cap → 413
            big = b"\x00" * (floorsmod._MAX_BYTES + 1)
            r = await c.put(f"/floors/{fid}/image", content=big, headers={"content-type": "image/png"})
            assert r.status_code == 413, "oversized image rejected"

            # upload: valid PNG with sane dims → 200, dims stored
            r = await c.put(f"/floors/{fid}/image?w=800&h=600", content=PNG,
                            headers={"content-type": "image/png"})
            assert r.status_code == 200
            body = r.json()
            assert body["img_path"] == f"{key}.png", "img_path is <key>.<ext>"
            assert body["img_w"] == 800 and body["img_h"] == 600, "valid dims persisted"
            assert (tmp / f"{key}.png").read_bytes() == PNG, "bytes written to disk"

            # serve → 200 with the right media type + exact bytes
            r = await c.get(f"/floorplan/{key}")
            assert r.status_code == 200
            assert r.headers["content-type"] == "image/png"
            assert r.content == PNG, "served bytes round-trip"

            # w= non-numeric → ValueError branch drops BOTH dims to None
            r = await c.put(f"/floors/{fid}/image?w=abc&h=600", content=PNG,
                            headers={"content-type": "image/png"})
            assert r.status_code == 200
            assert r.json()["img_w"] is None and r.json()["img_h"] is None, "unparseable dims → None"

            # dims out of the 1..8192 range → clamped to None (both)
            r = await c.put(f"/floors/{fid}/image?w=99999&h=99999", content=PNG,
                            headers={"content-type": "image/png"})
            assert r.status_code == 200
            assert r.json()["img_w"] is None and r.json()["img_h"] is None, "out-of-range dims → None"

            # upload a DIFFERENT type → the stale .png is dropped, img_path becomes .jpg
            r = await c.put(f"/floors/{fid}/image?w=100&h=100", content=JPG,
                            headers={"content-type": "image/jpeg"})
            assert r.status_code == 200
            assert r.json()["img_path"] == f"{key}.jpg", "new type replaces img_path"
            assert not (tmp / f"{key}.png").exists(), "stale .png of the old type is unlinked"

            # upload to a non-existent floor → 404
            r = await c.put("/floors/999999999/image", content=PNG,
                            headers={"content-type": "image/png"})
            assert r.status_code == 404, "upload to a gone floor is 404"

            # detect-borders with the scaffold present → planvision down → 502
            r = await c.post(f"/floors/{key}/detect-borders")
            assert r.status_code == 502, "planvision unreachable surfaces as 502"

            # remove the file on disk but keep img_path → serve 404 + scaffold 400
            (tmp / f"{key}.jpg").unlink()
            r = await c.get(f"/floorplan/{key}")
            assert r.status_code == 404, "img_path set but file gone serves 404"
            r = await c.post(f"/floors/{key}/detect-borders")
            assert r.status_code == 400, "missing scaffold file → 400"

            # detect-borders on a floor with NO img_path at all → 400
            r = await c.post("/floors/nosuchkey/detect-borders")
            assert r.status_code == 400, "no scaffold → 400"

            # save-borders persists the confirmed set; 404 for a gone floor
            r = await c.put(f"/floors/{fid}/borders", json={"borders": [[1, 2, 3, 4]]})
            assert r.status_code == 200
            assert r.json()["borders"] == [[1, 2, 3, 4]], "borders persisted + echoed"
            r = await c.put("/floors/999999999/borders", json={"borders": []})
            assert r.status_code == 404, "save-borders on a gone floor is 404"

            # rooms-from-borders: provided set AND saved set both hit planvision → 502; 404 for gone
            r = await c.post(f"/floors/{key}/rooms-from-borders", json={"borders": [[1, 2, 3, 4]]})
            assert r.status_code == 502, "rooms-from-borders (provided) reaches planvision"
            r = await c.post(f"/floors/{key}/rooms-from-borders", json={})
            assert r.status_code == 502, "rooms-from-borders (saved) reaches planvision"
            r = await c.post("/floors/nosuchkey/rooms-from-borders", json={})
            assert r.status_code == 404, "rooms-from-borders on a gone floor is 404"

            # erase-border: arity guard, 404, and the planvision path
            r = await c.post(f"/floors/{key}/erase-border", json={"polys": [[[0, 0], [1, 1]]]})
            assert r.status_code == 400, "erase-border needs exactly two areas"
            two = [[[0, 0], [10, 0], [10, 10]], [[10, 0], [20, 0], [20, 10]]]
            r = await c.post(f"/floors/{key}/erase-border", json={"borders": [], "polys": two})
            assert r.status_code == 502, "erase-border reaches planvision"
            r = await c.post("/floors/nosuchkey/erase-border", json={"polys": two})
            assert r.status_code == 404, "erase-border on a gone floor is 404"

            # delete removes the floor AND unlinks its (now-missing) image, no error
            r = await c.delete(f"/floors/{fid}")
            assert r.status_code == 204, "delete floor with an image path succeeds"
            r = await c.get("/floors")
            assert not any(f["id"] == fid for f in r.json()), "floor is gone"
    finally:
        floorsmod._DIR, floorsmod._PLANVISION = orig_dir, orig_pv
        shutil.rmtree(tmp, ignore_errors=True)
        await pool.execute("DELETE FROM floors WHERE key LIKE 'zzimg%'")
        await pool.execute("DELETE FROM users WHERE username = 'flradmin2'")
        await pool.close()


async def test_floors_planvision_success():
    """The border endpoints against a LIVE (stub) planvision — the 2xx path that
    returns planvision's payload verbatim (detect / rooms-from-borders / erase)."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM floors WHERE key LIKE 'zzpv%'")
    await pool.execute("DELETE FROM users WHERE username = 'flrpv'")
    await pool.execute(
        "INSERT INTO users (username, password_hash, role) VALUES ('flrpv', $1, 'admin')",
        await hash_password("adminpw12"),
    )
    await pool.execute(
        "INSERT INTO floors (key, name, sort_order, img_path, img_w, img_h) "
        "VALUES ('zzpv-floor', 'zzpv', 91, 'zzpv-floor.png', 1200, 800)"
    )
    _wire(pool)

    orig_dir, orig_pv = floorsmod._DIR, floorsmod._PLANVISION
    tmp = Path(tempfile.mkdtemp(prefix="dida-pv-"))
    floorsmod._DIR = tmp
    (tmp / "zzpv-floor.png").write_bytes(PNG)

    srv = HTTPServer(("127.0.0.1", 0), _PlanvisionHandler)
    floorsmod._PLANVISION = f"http://127.0.0.1:{srv.server_address[1]}"
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()

    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            r = await c.post("/auth/login", json={"username": "flrpv", "password": "adminpw12"})
            assert r.status_code == 200, "admin login"

            r = await c.post("/floors/zzpv-floor/detect-borders")
            assert r.status_code == 200 and r.json()["borders"] == [[1, 2, 3, 4]], "detect returns planvision borders"

            r = await c.post("/floors/zzpv-floor/rooms-from-borders", json={"borders": [[1, 2, 3, 4]]})
            assert r.status_code == 200 and "rooms" in r.json(), "rooms-from-borders returns planvision rooms"

            two = [[[0, 0], [10, 0], [10, 10]], [[10, 0], [20, 0], [20, 10]]]
            r = await c.post("/floors/zzpv-floor/erase-border", json={"borders": [], "polys": two})
            assert r.status_code == 200 and "removed" in r.json(), "erase-border returns planvision payload"
    finally:
        srv.shutdown()
        floorsmod._DIR, floorsmod._PLANVISION = orig_dir, orig_pv
        shutil.rmtree(tmp, ignore_errors=True)
        await pool.execute("DELETE FROM floors WHERE key LIKE 'zzpv%'")
        await pool.execute("DELETE FROM users WHERE username = 'flrpv'")
        await pool.close()


async def test_floors_access_boundary():
    """A page-scoped (non-floorplan) login is blocked from the image serve AND
    from the admin-only write surface."""
    pool = await pg_pool(min_size=1, max_size=4, init=jsonb_init)
    await apply_migrations(pool, "db/migrations")
    await pool.execute("DELETE FROM floors WHERE key LIKE 'zzacl%'")
    await pool.execute("DELETE FROM users WHERE username = 'flrscoped'")
    # role=user with allowed_pages that does NOT include 'floorplan'
    await pool.execute(
        "INSERT INTO users (username, password_hash, role, allowed_pages) "
        "VALUES ('flrscoped', $1, 'user', $2)",
        await hash_password("userpw123"), ["media"],
    )
    fid = await pool.fetchval(
        "INSERT INTO floors (key, name, sort_order, img_path) VALUES ('zzacl-floor', 'zzacl', 90, 'zzacl-floor.png') "
        "RETURNING id"
    )
    _wire(pool)

    orig_dir = floorsmod._DIR
    tmp = Path(tempfile.mkdtemp(prefix="dida-acl-"))
    floorsmod._DIR = tmp
    (tmp / "zzacl-floor.png").write_bytes(PNG)

    try:
        async with AsyncClient(transport=ASGITransport(app=appmod.app), base_url="http://itest") as c:
            r = await c.post("/auth/login", json={"username": "flrscoped", "password": "userpw123"})
            assert r.status_code == 200, "scoped user login"

            # image serve is gated to floorplan-page access → 403 even though the file exists
            r = await c.get("/floorplan/zzacl-floor")
            assert r.status_code == 403, "page-scoped login can't fetch the floor plan image"

            # require_admin boundary on the write surface → 403
            r = await c.post("/floors", json={"name": "nope"})
            assert r.status_code == 403, "non-admin can't create a floor"
            r = await c.patch(f"/floors/{fid}", json={"name": "nope"})
            assert r.status_code == 403, "non-admin can't patch a floor"
            r = await c.delete(f"/floors/{fid}")
            assert r.status_code == 403, "non-admin can't delete a floor"
    finally:
        floorsmod._DIR = orig_dir
        shutil.rmtree(tmp, ignore_errors=True)
        await pool.execute("DELETE FROM floors WHERE key LIKE 'zzacl%'")
        await pool.execute("DELETE FROM users WHERE username = 'flrscoped'")
        await pool.close()
