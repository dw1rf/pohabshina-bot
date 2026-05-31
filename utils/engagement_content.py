from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    "morning_messages": [
        "🌞 Доброе утро, друзья!\n\nЖелаем вам отличного дня,\nхорошего настроения и приятного общения.\n\n☕ Не забудьте позавтракать и зарядиться энергией!",
        "🌅 Новый день уже здесь!\n\nПусть он принесёт приятные разговоры,\nполезные дела и хорошее настроение.\n\n🍵 Начните утро спокойно и с улыбкой.",
        "☀️ Доброе утро!\n\nПусть сегодня всё получается легче,\nа на сервере будет много тёплого общения.\n\n🥐 Время зарядиться энергией.",
    ],
    "morning_gifs": [
        "https://media.giphy.com/media/3o7TKsQ8UQ4l4LhGz6/giphy.gif",
        "https://media.giphy.com/media/ASd0Ukj0y3qMM/giphy.gif",
    ],
    "morning_images": [
        "https://images.unsplash.com/photo-1490750967868-88aa4486c946?auto=format&fit=crop&w=1200&q=80",
        "https://images.unsplash.com/photo-1500530855697-b586d89ba3ee?auto=format&fit=crop&w=1200&q=80",
    ],
    "night_messages": [
        "🌙 Спокойной ночи!\n\nСпасибо всем за сегодняшний день.\nПусть завтрашний день принесёт много хорошего.\n\n✨ Сладких снов и приятного отдыха.",
        "🌌 Время отдыхать.\n\nПусть ночь будет спокойной,\nа утро встретит вас новыми силами.\n\n💫 До завтра!",
        "⭐ Спокойной ночи, друзья!\n\nСпасибо за активность и тёплое общение сегодня.\n\n🌙 Пусть сон будет крепким и приятным.",
    ],
    "night_gifs": [
        "https://media.giphy.com/media/KD8Ldwzx90X9hi9QHW/giphy.gif",
        "https://media.giphy.com/media/3o6Zt481isNVuQI1l6/giphy.gif",
    ],
    "night_images": [
        "https://images.unsplash.com/photo-1507400492013-162706c8c05e?auto=format&fit=crop&w=1200&q=80",
        "https://images.unsplash.com/photo-1475274047050-1d0c0975c63e?auto=format&fit=crop&w=1200&q=80",
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
