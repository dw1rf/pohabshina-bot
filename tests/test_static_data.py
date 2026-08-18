from pathlib import Path

from utils import static_data


def test_static_data_prefers_packaged_catalog(monkeypatch, tmp_path: Path) -> None:
    packaged = tmp_path / "catalog_data"
    source = tmp_path / "data"
    packaged.mkdir()
    source.mkdir()
    (packaged / "catalog.json").write_text("packaged", encoding="utf-8")
    (source / "catalog.json").write_text("source", encoding="utf-8")
    monkeypatch.setattr(static_data, "PACKAGED_DATA_DIR", packaged)
    monkeypatch.setattr(static_data, "SOURCE_DATA_DIR", source)

    assert static_data.static_data_path("catalog.json").read_text(encoding="utf-8") == "packaged"


def test_static_data_falls_back_to_source_tree(monkeypatch, tmp_path: Path) -> None:
    packaged = tmp_path / "catalog_data"
    source = tmp_path / "data"
    packaged.mkdir()
    source.mkdir()
    (source / "catalog.json").write_text("source", encoding="utf-8")
    monkeypatch.setattr(static_data, "PACKAGED_DATA_DIR", packaged)
    monkeypatch.setattr(static_data, "SOURCE_DATA_DIR", source)

    assert static_data.static_data_path("catalog.json") == source / "catalog.json"
