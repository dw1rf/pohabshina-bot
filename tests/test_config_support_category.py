from __future__ import annotations

from config import load_settings


def test_support_category_id_strips_quotes(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_CATEGORY_ID", ' "123456789012345678" ')

    settings = load_settings()

    assert settings.support_category_id == 123456789012345678
    assert settings.support_category_error is None


def test_support_category_id_reports_invalid(monkeypatch) -> None:
    monkeypatch.setenv("SUPPORT_CATEGORY_ID", "not-a-discord-id")

    settings = load_settings()

    assert settings.support_category_id == 0
    assert settings.support_category_error == "SUPPORT_CATEGORY_ID is invalid: not-a-discord-id"


def test_support_category_id_reports_missing(monkeypatch) -> None:
    monkeypatch.delenv("SUPPORT_CATEGORY_ID", raising=False)

    settings = load_settings()

    assert settings.support_category_id == 0
    assert settings.support_category_error == "SUPPORT_CATEGORY_ID is not set"
