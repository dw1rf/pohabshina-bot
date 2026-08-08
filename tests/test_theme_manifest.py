from __future__ import annotations

import hashlib
import json
from pathlib import Path


def test_dark_anime_glam_manifest_and_checksums() -> None:
    root = Path(__file__).resolve().parents[1] / "assets" / "themes" / "dark_anime_glam"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["brand"] == "Vulgarities Bot"
    assert len(manifest["assets"]) == 8
    for metadata in manifest["assets"].values():
        path = root / metadata["file"]
        assert path.is_file()
        assert hashlib.sha256(path.read_bytes()).hexdigest().lower() == metadata["sha256"].lower()
