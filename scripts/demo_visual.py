from __future__ import annotations

import hashlib
import json
from pathlib import Path


def check_visual_assets(manifest: Path, files: list[Path]) -> None:
    approved = set(json.loads(manifest.read_text())["approved_sha256"])
    if not approved:
        raise ValueError("The demo visual review manifest is empty")
    for path in files:
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest not in approved:
            raise ValueError(f"Demo image requires visual privacy review: {path.name}")
