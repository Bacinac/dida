#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from demo_visual import check_visual_assets

root = Path(__file__).resolve().parent.parent
site = Path(sys.argv[1])
files = [*site.glob("api/floorplan/*.png"), *site.glob("api/camera/*/snapshot"),
         *site.glob("floorplan/*.png"), site / "og.png"]
check_visual_assets(root / "demo/visual-assets.json", files)
print(f"PASS: {len(files)} published demo images match the visual review manifest")
