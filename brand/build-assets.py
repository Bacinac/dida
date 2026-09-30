#!/usr/bin/env python3
"""Derive every shipped DIDA mark from the accepted family sheet.

Source of the geometry: brand/proposals/complete-app-family-overview-set-18.svg.
The glyph paths, the signature cut, the tile and the D's place on it are copied
from that sheet verbatim — nothing is redrawn here, and nothing this script
writes is ever edited by hand.

    brand/build.sh
"""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "ui" / "static"
LIB = ROOT / "ui" / "src" / "lib"
RES = ROOT / "android" / "app" / "src" / "main" / "res"

INK = "#0e9db5"
TILE_INK = "#48d5e5"
TILE_FROM, TILE_TO = "#073844", "#0d7689"
PLACE = "translate(14.5 12) scale(.76)"
PLACE_XY, PLACE_S = (14.5, 12.0), 0.76

STROKE = ('fill="none" stroke="{}" stroke-width="13" '
          'stroke-linecap="round" stroke-linejoin="round"')
GLYPH = {
    "D": "M18 13v70h18c25 0 39-13 39-35S61 13 36 13H18",
    "I": "M8 13h28M22 13v70M8 83h28",
    "A": "M8 83 36 13l28 70M19 58h34",
}
WORD = (("D", 0, True), ("I", 86, False), ("D", 138, False), ("A", 224, False))

CUT = "M55 4h15L53 38H38Z"
# The same cut as a clip: VectorDrawable has no mask. Its two edges run on to the
# top of the glyph box, where the glyph has no ink (the stroke starts at y 6.5).
CUT_CLIP = "M0,0H57L38,38H53L72,0H96V96H0Z"
MASK = ('<mask id="cut" maskUnits="userSpaceOnUse" x="0" y="0" width="96" height="96">'
        f'<rect width="96" height="96" fill="#fff"/><path d="{CUT}" fill="#000"/></mask>')
ARC = "M10 17C31 2 65 2 86 17"

# The favicon is framed on its measured ink, not on the 96-unit glyph box: the
# D's box is left-heavy (the stem starts at 11.5, the bowl ends at 81.5).
FAVICON_SIDE = 86
# The accepted tile puts the D's lower-left corner on the edge of the maskable
# safe circle (40 % radius); launchers that crop to a circle need a margin.
MASKABLE_SCALE = 0.9

OWNER = os.environ.get("OWNER")


def glyph(name: str, colour: str, *, cut: bool = False) -> str:
    g = f'<g {STROKE.format(colour)}><path d="{GLYPH[name]}"/></g>'
    return f'<g mask="url(#cut)">{g}</g>' if cut else g


def document(viewbox: str, body: str, defs: str = "", label: str = "") -> str:
    title = f"<title>{label}</title>" if label else ""
    aria = f' role="img" aria-label="{label}"' if label else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}"{aria}>'
            f"{title}<defs>{MASK}{defs}</defs>{body}</svg>\n")


def render(svg: str, width: int, height: int | None = None) -> Image.Image:
    png = subprocess.run(["rsvg-convert", "-w", str(width), "-h", str(height or width)],
                         input=svg.encode(), capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def ink_box(body: str, viewbox: tuple[float, float, float, float]) -> tuple[float, ...]:
    """The rendered ink's bounds, in the document's own units."""
    x, y, w, h = viewbox
    px = 20
    img = render(document(f"{x} {y} {w} {h}", body), round(w * px), round(h * px))
    x0, y0, x1, y1 = img.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
    return x + x0 / px, y + y0 / px, x + x1 / px, y + y1 / px


def write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)
    if OWNER:
        uid, gid = (int(n) for n in OWNER.split(":"))
        os.chown(path, uid, gid)
    print(f"  {path.relative_to(ROOT)}")


def png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def favicon() -> str:
    x0, y0, x1, y1 = ink_box(glyph("D", INK, cut=True), (0, 0, 96, 96))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half = FAVICON_SIDE / 2
    return document(f"{cx - half:g} {cy - half:g} {FAVICON_SIDE} {FAVICON_SIDE}",
                    glyph("D", INK, cut=True), label="DIDA")


def logo() -> str:
    body = "".join(f'<g transform="translate({dx})">{glyph(n, INK, cut=c)}</g>'
                   for n, dx, c in WORD)
    x0, y0, x1, y1 = ink_box(body, (0, 0, 320, 96))
    return document(f"{x0:g} {y0:g} {x1 - x0:g} {y1 - y0:g}", body, label="DIDA")


def tile(*, rounded: bool, scale: float = 1.0) -> str:
    gradient = ('<linearGradient id="tile" x1="0" y1="0" x2="1" y2="1">'
                f'<stop stop-color="{TILE_FROM}"/><stop offset="1" stop-color="{TILE_TO}"/>'
                "</linearGradient>")
    rx = ' rx="22"' if rounded else ""
    content = (f'<path d="{ARC}" fill="none" stroke="#fff" stroke-opacity=".08" stroke-width="2"/>'
               f'<g transform="{PLACE}">{glyph("D", TILE_INK, cut=True)}</g>')
    if scale != 1.0:
        content = f'<g transform="translate(48 48) scale({scale:g}) translate(-48 -48)">{content}</g>'
    return document("0 0 96 96", f'<rect width="96" height="96"{rx} fill="url(#tile)"/>{content}',
                    defs=gradient)


def vector(body: str, *, aapt: bool = False) -> str:
    ns = '\n    xmlns:aapt="http://schemas.android.com/aapt"' if aapt else ""
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            f'<vector xmlns:android="http://schemas.android.com/apk/res/android"{ns}\n'
            '    android:width="108dp"\n    android:height="108dp"\n'
            '    android:viewportWidth="108"\n    android:viewportHeight="108">\n'
            f"{body}</vector>\n")


def android_foreground() -> str:
    # The 96-unit tile fills the 72 dp an adaptive icon shows of its 108 dp canvas.
    (tx, ty), s = PLACE_XY, PLACE_S
    return vector(
        f'    <group\n        android:translateX="{18 + 0.75 * tx:g}"\n'
        f'        android:translateY="{18 + 0.75 * ty:g}"\n'
        f'        android:scaleX="{0.75 * s:g}"\n        android:scaleY="{0.75 * s:g}">\n'
        f'        <clip-path android:pathData="{CUT_CLIP}" />\n'
        f'        <path\n            android:pathData="{GLYPH["D"]}"\n'
        f'            android:strokeColor="{TILE_INK}"\n            android:strokeWidth="13"\n'
        '            android:strokeLineCap="round"\n            android:strokeLineJoin="round" />\n'
        "    </group>\n")


def android_background() -> str:
    return vector(
        '    <path android:pathData="M0,0h108v108h-108z">\n'
        '        <aapt:attr name="android:fillColor">\n'
        '            <gradient\n                android:type="linear"\n'
        '                android:startX="18"\n                android:startY="18"\n'
        '                android:endX="90"\n                android:endY="90">\n'
        f'                <item android:offset="0" android:color="{TILE_FROM}" />\n'
        f'                <item android:offset="1" android:color="{TILE_TO}" />\n'
        "            </gradient>\n        </aapt:attr>\n    </path>\n"
        '    <group\n        android:translateX="18"\n        android:translateY="18"\n'
        '        android:scaleX="0.75"\n        android:scaleY="0.75">\n'
        f'        <path\n            android:pathData="{ARC}"\n'
        '            android:strokeColor="#FFFFFF"\n            android:strokeAlpha="0.08"\n'
        '            android:strokeWidth="2" />\n'
        "    </group>\n", aapt=True)


ADAPTIVE = """<?xml version="1.0" encoding="utf-8"?>
<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">
    <background android:drawable="@drawable/ic_launcher_background" />
    <foreground android:drawable="@drawable/ic_launcher_foreground" />
    <monochrome android:drawable="@drawable/ic_launcher_foreground" />
</adaptive-icon>
"""


def main() -> None:
    mark = favicon()
    print("browser")
    write(STATIC / "favicon.svg", mark)
    sizes = {n: render(mark, n) for n in (16, 32, 48)}
    write(STATIC / "favicon-16x16.png", png(sizes[16]))
    write(STATIC / "favicon-32x32.png", png(sizes[32]))
    ico = io.BytesIO()
    sizes[48].save(ico, "ICO", sizes=[(16, 16), (32, 32), (48, 48)],
                   append_images=[sizes[16], sizes[32]])
    write(STATIC / "favicon.ico", ico.getvalue())

    print("installed")
    write(STATIC / "icon-192.png", png(render(tile(rounded=True), 192)))
    write(STATIC / "icon-512.png", png(render(tile(rounded=True), 512)))
    # iOS rounds the corners itself and puts black behind transparency.
    write(STATIC / "apple-touch-icon.png",
          png(render(tile(rounded=False), 180).convert("RGB")))
    for n in (192, 512):
        write(STATIC / f"icon-maskable-{n}.png",
              png(render(tile(rounded=False, scale=MASKABLE_SCALE), n).convert("RGB")))
    # Android draws the notification badge as a monochrome mask.
    write(STATIC / "badge-96.png",
          png(render(mark.replace(INK, "#ffffff"), 96)))

    print("in-app logo")
    write(LIB / "dida-logo.svg", logo())

    print("android launcher")
    write(RES / "drawable" / "ic_launcher_foreground.xml", android_foreground())
    write(RES / "drawable" / "ic_launcher_background.xml", android_background())
    write(RES / "mipmap-anydpi" / "ic_launcher.xml", ADAPTIVE)


if __name__ == "__main__":
    main()
