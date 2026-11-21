from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _parse_int_set(raw: str) -> set[int]:
    values: set[int] = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if part.isdigit():
            values.add(int(part))
    return values


def _parse_bool(raw: str, default: bool = False) -> bool:
    value = (raw or "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _normalize_discord_id(raw: str | None) -> tuple[str, int, str | None]:
    value = (raw or "").strip()
    cleaned = value.strip("\"'").strip()
    if not cleaned:
        return value, 0, "SUPPORT_CATEGORY_ID is not set"
    if not cleaned.isdigit():
        return value, 0, f"SUPPORT_CATEGORY_ID is invalid: {value}"
    return value, int(cleaned), None


@dataclass(slots=True)
class Settings:
    discord_token: str
    target_channel_id: int
    control_channel_id: int
    delete_control_messages: bool
    allowed_user_ids: set[int]
    allowed_role_ids: set[int]
    mod_log_channel_id: int
    db_path: str
    support_category_raw: str
    support_category_id: int
    support_category_error: str | None
    movie_rating_channel_id: int
    afisha_channel_id: int
    rules_channel_id: int
    roles_channel_id: int
    support_channel_id: int
    shop_channel_id: int
    ping_guard_delete_repeats: bool
    birthday_channel_id: int
    timezone: str
    engagement_content_path: str
    level_cooldown_seconds: int = 15
    min_message_length: int = 2
    max_level: int = 300



def load_settings() -> Settings:
    support_category_raw, support_category_id, support_category_error = _normalize_discord_id(
        os.getenv("SUPPORT_CATEGORY_ID")
    )
    return Settings(
        discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
        target_channel_id=int(os.getenv("TARGET_CHANNEL_ID", "0") or 0),
        control_channel_id=int(os.getenv("CONTROL_CHANNEL_ID", "0") or 0),
        delete_control_messages=_parse_bool(os.getenv("DELETE_CONTROL_MESSAGES", "false")),
        allowed_user_ids=_parse_int_set(os.getenv("ALLOWED_USER_IDS", "")),
        allowed_role_ids=_parse_int_set(os.getenv("ALLOWED_ROLE_IDS", "")),
        mod_log_channel_id=int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or 0),
        db_path=os.getenv("SQLITE_PATH", "data/bot.db"),
        support_category_raw=support_category_raw,
        support_category_id=support_category_id,
        support_category_error=support_category_error,
        movie_rating_channel_id=int(os.getenv("MOVIE_RATING_CHANNEL_ID", "0") or 0),
        afisha_channel_id=int(os.getenv("AFISHA_CHANNEL_ID", "0") or 0),
        rules_channel_id=int(os.getenv("RULES_CHANNEL_ID", "0") or 0),
        roles_channel_id=int(os.getenv("ROLES_CHANNEL_ID", "0") or 0),
        support_channel_id=int(os.getenv("SUPPORT_CHANNEL_ID", "0") or 0),
        shop_channel_id=int(os.getenv("SHOP_CHANNEL_ID", "0") or 0),
        ping_guard_delete_repeats=_parse_bool(os.getenv("PING_GUARD_DELETE_REPEATS", "false")),
        birthday_channel_id=int(os.getenv("BIRTHDAY_CHANNEL_ID", "0") or 0),
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
        engagement_content_path=os.getenv("ENGAGEMENT_CONTENT_PATH", "catalog_data/engagement_content.json").strip()
        or "catalog_data/engagement_content.json",
    )
