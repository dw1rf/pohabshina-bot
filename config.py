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


@dataclass(slots=True)
class Settings:
    discord_token: str
    watchmode_api_key: str
    watchmode_region: str
    watchmode_limit: int
    target_channel_id: int
    control_channel_id: int
    delete_control_messages: bool
    allowed_user_ids: set[int]
    allowed_role_ids: set[int]
    mod_log_channel_id: int
    translate_api_url: str
    translate_api_key: str
    translate_source_lang: str
    translate_target_lang: str
    show_both_titles: bool
    db_path: str
    rep_announce_channel_id: int
    rep_plus_emoji: str
    support_category_id: int
    support_admin_role_id: int
    support_log_channel_id: int
    shop_requests_to_support: bool
    level_cooldown_seconds: int = 15
    min_message_length: int = 2
    max_level: int = 300



def load_settings() -> Settings:
    return Settings(
        discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
        watchmode_api_key=os.getenv("WATCHMODE_API_KEY", "").strip(),
        watchmode_region=os.getenv("WATCHMODE_REGION", "US").strip() or "US",
        watchmode_limit=int(os.getenv("WATCHMODE_LIMIT", "80")),
        target_channel_id=int(os.getenv("TARGET_CHANNEL_ID", "0") or 0),
        control_channel_id=int(os.getenv("CONTROL_CHANNEL_ID", "0") or 0),
        delete_control_messages=_parse_bool(os.getenv("DELETE_CONTROL_MESSAGES", "false")),
        allowed_user_ids=_parse_int_set(os.getenv("ALLOWED_USER_IDS", "")),
        allowed_role_ids=_parse_int_set(os.getenv("ALLOWED_ROLE_IDS", "")),
        mod_log_channel_id=int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or 0),
        translate_api_url=os.getenv("TRANSLATE_API_URL", "").strip(),
        translate_api_key=os.getenv("TRANSLATE_API_KEY", "").strip(),
        translate_source_lang=os.getenv("TRANSLATE_SOURCE_LANG", "auto").strip() or "auto",
        translate_target_lang=os.getenv("TRANSLATE_TARGET_LANG", "ru").strip() or "ru",
        show_both_titles=_parse_bool(os.getenv("SHOW_BOTH_TITLES", "1"), default=True),
        db_path=os.getenv("SQLITE_PATH", "data/bot.db"),
        rep_announce_channel_id=int(os.getenv("REP_ANNOUNCE_CHANNEL_ID", "0") or 0),
        rep_plus_emoji=os.getenv("REP_PLUS_EMOJI", "👍").strip() or "👍",
        support_category_id=int(os.getenv("SUPPORT_CATEGORY_ID", "0") or 0),
        support_admin_role_id=int(os.getenv("SUPPORT_ADMIN_ROLE_ID", "0") or 0),
        support_log_channel_id=int(os.getenv("SUPPORT_LOG_CHANNEL_ID", "0") or 0),
        shop_requests_to_support=_parse_bool(os.getenv("SHOP_REQUESTS_TO_SUPPORT", "true"), default=True),
    )
