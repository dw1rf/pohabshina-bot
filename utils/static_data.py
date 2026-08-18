from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGED_DATA_DIR = PROJECT_ROOT / "catalog_data"
SOURCE_DATA_DIR = PROJECT_ROOT / "data"


def static_data_path(filename: str) -> Path:
    """Return static catalog data without depending on the mutable data mount."""
    packaged_path = PACKAGED_DATA_DIR / filename
    if packaged_path.is_file():
        return packaged_path
    return SOURCE_DATA_DIR / filename
