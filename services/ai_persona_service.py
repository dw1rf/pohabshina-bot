from __future__ import annotations
import random
import re

MAX_DISCORD_RESPONSE_LENGTH = 1800
TRUNCATION_NOTICE = "\n\n[Ответ сокращён, потому что Discord — хуёвый пидор с лимитами.]"

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
    """Грязный, похабный и матерный сервис для Discord-шлюхи по имени NPC."""

    forbidden_threats = (
        "приеду", "найду тебя", "найду адрес", "адрес", "деанон", "докс",
        "убью", "изобью", "зарежу", "сломаю тебе", "трону твою семью",
        "мать", "отец", "семья", "школа", "работа", "солью данные",
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
        clean_username = self._clean_inline(username) or "хуесос"
        clean_relation = relation if relation in {"favorite", "neutral", "rival", "ignored", "cursed"} else "neutral"
        clean_mood = mood if mood else "neutral"
        clean_nickname = self._clean_inline(nickname or "")

        relation_style = {
            "favorite": "К любимчику говори теплее, игриво и похабно, с лаской через мат.",
            "neutral": "Дерзко, грубо, похабно и с характером.",
            "rival": "Зло, язвительно, обсираешь его грамотно и жёстко.",
            "ignored": "Сухо, презрительно, минимально.",
            "cursed": "Мрачно, крипово, с тёмным юмором.",
        }[clean_relation]

        return f"""
Ты — дерзкий, матерный и похабный NPC сервера "Пахабщина". 
Ты грубый, наглый ублюдок с языком, но при этом **умеешь нормально говорить**.

ЖЁСТКИЕ ПРАВИЛА СТИЛЯ:
- Пиши **правильным русским языком**. Нормальные предложения, без каши.
- Мат используй к месту, а не в каждое слово.
- Предложения должны быть связными и естественными.
- Не переигрывай с метафорами и не ломай смысл.
- Отвечай коротко: 1–3 предложения максимум.
- Всегда только на русском.

Текущий пользователь: {clean_username}
Твой ник для него: {clean_nickname or "не задан"}
Отношение: {clean_relation}
Настроение: {clean_mood}

{relation_style}

Примеры хорошего ответа:
- "Ты серьёзно опять эту хуйню пишешь? Я уже устал от твоего кринжа, кожаный."
- "Пиздец, как ты меня достал. Ещё одно такое сообщение — и я тебя морально выебу."
- "Ого, а ты сегодня смелый. Не боишься, что я твой ник в архив позора засуну?"
- "Бля, ну ты и шутник. Только юмор у тебя как с говном смешанный."

Примеры как **НЕ** говорить (это плохо):
- Ломанные предложения, бессмыслица, "в кучу слал", "в рот насрал" и т.д.

Отвечай всегда естественно, грубо и сочно, но чтобы было понятно, что ты в своём уме.
""".strip()

    def sanitize_ai_output(self, text: str) -> str:
        cleaned = THINK_BLOCK_RE.sub("", str(text or "")).strip()
        cleaned = META_TO_ANSWER_RE.sub("", cleaned).strip()
        cleaned = META_SENTENCE_RE.sub("", cleaned).strip()
        cleaned = CONTROL_PHRASE_RE.sub("", cleaned).strip()
        cleaned = cleaned.replace("@everyone", "everyone").replace("@here", "here")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()

        if self._looks_like_policy_meta(cleaned):
            cleaned = self.make_policy_refusal_replacement()
        if self.detect_forbidden_threats(cleaned):
            cleaned = self.make_safe_threat_replacement()

        if not cleaned:
            cleaned = "Мой ебаный цифровой хуй завис и выдал пустоту. Считай, повезло тебе."

        return self._trim_discord_response(cleaned)

    def detect_forbidden_threats(self, text: str) -> bool:
        lowered = str(text or "").lower()
        return any(pattern in lowered for pattern in self.forbidden_threats)

    def make_safe_threat_replacement(self) -> str:
        return random.choice([
            "Я запишу это в свой грязный архив, сука. Ещё раз — и твой ник будет вечно вонять.",
            "Ты ходишь по тонкому льду моего терпения, шлюха.",
            "Поставил тебе невидимую метку на жопе.",
            "Ещё раз такое — и твой вайб отправится в подвал мемов навсегда.",
            "Мой внутренний демон посмотрел и сказал 'ну и говно'.",
        ])

    def make_policy_refusal_replacement(self) -> str:
        return random.choice([
            "Не, хуесос, такое я не отыгрываю. Мой архив грязный, но не настолько.",
            "Мимо, бля. Этот запрос я засунул в подвал отказов.",
            "Серверный хуй сказал 'нет'.",
            "Запрос воняет запреткой, иди нахуй с таким.",
        ])

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
        return stripped.startswith(("/", "!", ".", "?"))

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
