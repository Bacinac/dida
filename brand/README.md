# DIDA brand assets

The geometry comes from the accepted family sheet,
`proposals/complete-app-family-overview-set-18.svg` (DIDA, BABA and the three
OPUS modules). `build-assets.py` carries DIDA's part of it verbatim — the D, I
and A strokes, the signature cut, the tile and the D's place on it — and derives
every shipped file. No derived file is ever hand-edited.

```sh
brand/build.sh     # runs build-assets.py in a container (rsvg-convert + Pillow)
```

## What it writes

| Target | Notes |
|---|---|
| `ui/static/favicon.svg`, `favicon.ico` (16/32/48), `favicon-{16x16,32x32}.png` | The cut D alone, `#0e9db5`, transparent. Framed on its measured ink, so the geometry — not the 96-unit glyph box — is centred |
| `ui/static/icon-{192,512}.png` | The rounded tile exactly as on the sheet; manifest only, never a `<link rel=icon>` |
| `ui/static/apple-touch-icon.png` | Full-bleed and opaque: iOS rounds it itself and puts black behind transparency |
| `ui/static/icon-maskable-{192,512}.png` | Full-bleed; tile content at 90 % so the D clears the 40 % safe circle |
| `ui/static/badge-96.png` | Push badge — white cut D on transparency, because Android masks it to monochrome |
| `ui/src/lib/dida-logo.svg` | The DIDA logo, cropped to its ink; `Brand.svelte` imports it (hashed URL) |
| `android/app/…/drawable/ic_launcher_{foreground,background}.xml`, `mipmap-anydpi/ic_launcher.xml` | Adaptive icon as vectors. VectorDrawable has no mask, so the cut is the same quadrilateral as a clip |

Colours: browser mark and logo `#0e9db5`; installed letter `#48d5e5`; tile
gradient `#073844 → #0d7689`.

Cache busting: every reference in `ui/src/app.html`, `site.webmanifest` and the
service worker carries `?v=<date>`. Bump it whenever the marks change.
