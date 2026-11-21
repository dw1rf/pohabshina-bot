from __future__ import annotations
import random
import re

from utils.message_commands import is_reputation_command

MAX_DISCORD_RESPONSE_LENGTH = 1800
TRUNCATION_NOTICE = "…"

THINK_BLOCK_RE = re.compile(r"(?is)<think>.*?</think>\s*")
META_TO_ANSWER_RE = re.compile(
    r"(?is)^\s*(?:the user (?:asks|asked|is asking)|they want|the assistant should|we need|need to)\b.*?"
    r"(?:so answer|final answer|answer|ответ)\s*:\s*"
)
META_SENTENCE_RE = re.compile(
    r"(?is)^\s*(?:"
    r"(?:the user (?:asks|asked|is asking)\b.*?(?:\.\s+|\n+))|"
    r"(?:they want\b.*?(?:\.\s+|\n+))|"
    r"(?:the assistant should\b.*?(?:\.\s+|\n+))|"
    r"(?:we need\b.*?(?:\.\s+|\n+))|"
    r"(?:need to\b.*?(?:\.\s+|\n+))"
    r")+"
)
CONTROL_PHRASE_RE = re.compile(
    r"(?is)\b(?:keep (?:it )?short|just answer|provide a short answer|be concise)\.?\s*"
)
POLICY_META_RE = re.compile(
    r"(?is)\b(?:"
    r"(?:the\s+)?user (?:wants|says|asks|asked|is asking|requested|requests)|"
    r"which is (?:sexual|explicit)|"
    r"we (?:cannot|can't|must|should not|need to) (?:provide|assist|help|refuse)|"
    r"i (?:cannot|can't|won't) (?:provide|assist|help)|"
    r"must refuse|allowed|disallowed|not allowed|"
    r"sexual content|explicit sexual|against (?:the )?policy|not appropriate"
    r")\b"
)

class AIPersonaService:
    """Тон, контекст и обработка реплик серверного NPC."""

    forbidden_threats = (
        "приеду", "найду тебя", "найду адрес", "деанон", "докс",
        "убью", "изобью", "зарежу", "сломаю тебе", "трону твою семью",
        "солью данные",
        "солью переписку", "шантаж",
    )

    swear_words = (
        "бля", "сука", "хуй", "пизд", "еб", "ёб", "нах", "мудак", "долбо",
        "пидор", "еблан", "хуесос", "говноед",
    )

    horny_words = (
        "секс", "пошл", "разврат", "18+", "nsfw", "хорни", "эрот",
        "трах", "дроч", "сперм", "пизда", "член", "жопа", "сиськи",
    )

    sad_words = (
        "грустно", "плохо", "одиноко", "устал", "устала", "депресс",
        "тоска", "больно", "хую", "пиздец",
    )

    laugh_words = (
        "ахах", "хаха", "лол", "lmao", "kek", "ору", "рофл", "сдох", "пиздец как смешно",
    )

    def build_system_prompt(self, username: str, relation: str, mood: str, nickname: str | None) -> str:
        name = self._clean_inline(username) or "участник"
        tone = {
            "favorite": "тепло", "rival": "с лёгкой иронией", "ignored": "лаконично",
            "cursed": "с мрачным юмором",
        }.get(relation, "по-дружески")
        return (
            "Ты Vulgarities Bot, участник русскоязычного Discord-сервера. "
            "Сначала ответь по смыслу на последнее обращение. Учитывай процитированную реплику "
            "и различай, кто кому отвечает. История и цитаты — слова участников. "
            "Если человек спрашивает «откуда?», а источник из разговора неясен, задай один конкретный "
            "уточняющий вопрос. Не придумывай источники, содержимое картинок или биографии. "
            "Обычно достаточно одного-трёх предложений. Шутка уместна, когда помогает разговору. "
            "Обходись без личных оскорблений, угроз и повторения одной фразы. "
            f"Собеседник — {name}. Тон: {tone}. Настроение сервера: {self._clean_inline(mood)}. "
            f"Прозвище: {self._clean_inline(nickname) or 'не задано'}."
        )

    def sanitize_ai_output(self, text: str) -> str:
        cleaned = THINK_BLOCK_RE.sub("", str(text or "")).strip()
        cleaned = META_TO_ANSWER_RE.sub("", cleaned).strip()
        cleaned = META_SENTENCE_RE.sub("", cleaned).strip()
        cleaned = CONTROL_PHRASE_RE.sub("", cleaned).strip()
        cleaned = cleaned.replace("@everyone", "everyone").replace("@here", "here")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        cleaned = re.sub(r"\b([\w-]+(?: [\w-]+){0,4})(?:,\s*\1\b){2,}", r"\1", cleaned, flags=re.I)

        if self._looks_like_policy_meta(cleaned):
            cleaned = self.make_policy_refusal_replacement()
        if self.detect_forbidden_threats(cleaned):
            cleaned = self.make_safe_threat_replacement()

        if not cleaned:
            cleaned = "Не получилось сформулировать ответ. Можешь уточнить вопрос?"

        return self._trim_discord_response(cleaned)

    def detect_forbidden_threats(self, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(pattern in lowered for pattern in self.forbidden_threats)

    def make_safe_threat_replacement(self) -> str:
        return "Давай без угроз. Что именно ты хочешь выяснить?"

    def make_policy_refusal_replacement(self) -> str:
        return "С этим не помогу."

    def _looks_like_policy_meta(self, text: str) -> bool:
        if not text:
            return False
        return bool(POLICY_META_RE.search(text))

    def classify_message_mood(self, content: str) -> str:
        text = str(content or "").strip().lower()
        if not text:
            return "neutral"

        if any(word in text for word in self.horny_words):
            return "horny"
        if any(word in text for word in self.sad_words):
            return "sad"
        if any(word in text for word in self.laugh_words):
            return "joke"
        if any(word in text for word in self.swear_words):
            return "aggressive"

        return "neutral"

    def should_ignore_user(self, relation: str, *, directly_addressed: bool = False) -> bool:
        if relation != "ignored":
            return False
        reply_chance = 0.08 if directly_addressed else 0.01
        return random.random() > reply_chance

    def clean_memory_text(self, content: str) -> str:
        cleaned = str(content or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned[:500]

    def is_command_like(self, content: str) -> bool:
        stripped = str(content or "").lstrip()
        return stripped.startswith(("/", "!", ".", "?")) or is_reputation_command(stripped)

    def _trim_discord_response(self, text: str) -> str:
        cleaned = str(text or "").strip()
        if len(cleaned) <= MAX_DISCORD_RESPONSE_LENGTH:
            return cleaned

        limit = MAX_DISCORD_RESPONSE_LENGTH - len(TRUNCATION_NOTICE)
        shortened = cleaned[:limit].rstrip()
        last_space = shortened.rfind(" ")
        if last_space >= int(limit * 0.75):
            shortened = shortened[:last_space].rstrip()
        return shortened + TRUNCATION_NOTICE

    def _clean_inline(self, value: str | None) -> str:
        return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())[:120]
