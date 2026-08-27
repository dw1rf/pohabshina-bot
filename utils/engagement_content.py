from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils.static_data import static_data_path

logger = logging.getLogger(__name__)


DEFAULT_ENGAGEMENT_CONTENT: dict[str, list[str]] = {
    "levelup_messages": [
        "💖 Ты отлично проявляешь себя в жизни сервера.\nПродолжай общаться, заводить новые знакомства\nи собирать ещё больше опыта.",
        "✨ Твоя активность делает сообщество ярче.\nНе сбавляй темп и двигайся только вперёд!",
        "🌟 Каждый новый уровень — это результат твоего участия.\nТак держать!",
        "💬 Спасибо за время, которое ты проводишь с нами.\nЖелаем ещё больше приятных моментов на сервере!",
        "🚀 Отличный результат!\nПродолжай покорять новые вершины и удивлять всех своей активностью.",
        "🎯 Ещё один уровень успешно взят.\nВпереди тебя ждут новые достижения и награды!",
        "🔥 Ты продолжаешь уверенно набирать обороты.\nПусть следующий уровень придёт ещё быстрее!",
        "⭐ Опыт копится, уровни растут,\nа твой путь на сервере становится всё интереснее.",
        "💎 Такой прогресс заслуживает уважения.\nПродолжай в том же духе!",
        "🌸 Благодаря таким участникам сервер становится лучше.\nСпасибо за твою активность!",
        "🏆 Новая ступень пройдена!\nЖелаем тебе ещё больше успехов и ярких событий.",
        "🎊 Сегодня отличный повод для поздравлений.\nПусть это будет лишь начало новых достижений!",
        "⚡ Ты становишься сильнее с каждым уровнем.\nНе останавливайся на достигнутом!",
        "🌙 Ещё один шаг вперёд.\nПусть впереди тебя ждут только новые победы.",
        "💫 Продолжай писать свою историю на сервере.\nСамое интересное ещё впереди!",
        "🎁 Уровень получен!\nА значит пришло время двигаться к следующей цели.",
    ],
    "levelup_gifs": [
        "https://media.giphy.com/media/11sBLVxNs7v6WA/giphy.gif",
        "https://media.giphy.com/media/10VjiVoa9rWC4M/giphy.gif",
        "https://media.giphy.com/media/12LalkAXSlXnWw/giphy.gif",
    ],
    "reputation_messages": [
        "💬 Хорошая репутация показывает доверие сообщества.",
        "🌟 Каждая оценка делает вклад участника заметнее.",
        "🤝 Репутация растёт там, где есть поддержка и уважение.",
        "✨ Спасибо, что отмечаете вклад других участников.",
    ],
}


@dataclass(frozen=True, slots=True)
class EngagementContent:
    values: dict[str, list[str]]

    def list(self, key: str) -> list[str]:
        return self.values.get(key, [])


def _clean_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def load_engagement_content(path: str) -> EngagementContent:
    content = {key: list(values) for key, values in DEFAULT_ENGAGEMENT_CONTENT.items()}
    config_path = Path(path)
    if not config_path.exists() and config_path.name == "engagement_content.json":
        config_path = static_data_path("engagement_content.json")

    try:
        if config_path.exists():
            raw = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-level JSON value must be an object")
            for key in content:
                values = _clean_string_list(raw.get(key))
                if values:
                    content[key] = values
        else:
            logger.warning("Engagement content config not found: %s; using defaults", config_path)
    except Exception as exc:
        logger.warning("Failed to load engagement content config %s: %s; using defaults", config_path, exc)

    return EngagementContent(content)
