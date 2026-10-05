import hashlib
import json

import pytest
from demo_visual import check_visual_assets


def test_only_reviewed_image_bytes_can_be_published(tmp_path):
    asset = tmp_path / "camera.jpg"
    asset.write_bytes(b"reviewed camera image")
    manifest = tmp_path / "visual-assets.json"
    manifest.write_text(json.dumps({"approved_sha256": [hashlib.sha256(asset.read_bytes()).hexdigest()]}))
    check_visual_assets(manifest, [asset])
    asset.write_bytes(b"a new frame with a person")
    with pytest.raises(ValueError, match="visual privacy review"):
        check_visual_assets(manifest, [asset])
    asset.write_bytes(b"reviewed camera image")
    unexpected = tmp_path / "new-floor.png"
    unexpected.write_bytes(b"unreviewed floor plan")
    with pytest.raises(ValueError, match="new-floor"):
        check_visual_assets(manifest, [asset, unexpected])


def test_empty_review_manifest_fails_closed(tmp_path):
    manifest = tmp_path / "visual-assets.json"
    manifest.write_text(json.dumps({"approved_sha256": []}))
    with pytest.raises(ValueError, match="empty"):
        check_visual_assets(manifest, [])
