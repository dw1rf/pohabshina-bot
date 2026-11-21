import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import discord

from cogs.ai_chat import AIChatCog, _extract_gemini_text, _extract_openai_response_text
from services.ai_persona_service import AIPersonaService


def test_replies_follow_the_addressed_conversation(monkeypatch):
    async def scenario():
        async with aiosqlite.connect(":memory:") as db:
            db.row_factory = aiosqlite.Row
            bot_user = SimpleNamespace(id=11, mention="<@11>")
            cog = AIChatCog(SimpleNamespace(db=db, user=bot_user, settings=SimpleNamespace()))
            await cog.init_db()
            channel = Mock(spec=discord.TextChannel, id=1)
            channel.name = "general"
            channel.typing.return_value = nullcontext()
            author = SimpleNamespace(id=3, display_name="Участник", bot=False)
            guild = SimpleNamespace(id=4)
            await cog._set_ai_channel(4, 1, True)
            parent = Mock(spec=discord.Message)
            parent.author = author
            parent.clean_content = "Это за три смены."
            parent.id = 8
            message = SimpleNamespace(
                id=10, author=author, guild=guild, channel=channel, content="Откуда", clean_content="Откуда",
                mentions=[], reference=SimpleNamespace(channel_id=1, message_id=8, resolved=parent), reply=AsyncMock(),
            )
            cog.maybe_add_reaction = AsyncMock()
            cog.adjust_relation_from_message = AsyncMock()
            generate = AsyncMock(return_value="Ты про оплату за три смены?")
            monkeypatch.setattr("cogs.ai_chat.generate_ai_response", generate)
            monkeypatch.setenv("AI_ENABLE_RANDOM_REPLIES", "true")
            monkeypatch.setattr("cogs.ai_chat.random.random", lambda: 0)
            await cog.on_message(message)
            generate.assert_not_awaited()
            message.reply.assert_not_awaited()

            message.mentions = [bot_user]
            message.content = "<@11> Откуда"
            message.clean_content = "@Vulgarities Откуда"
            await cog.on_message(message)
            prompt = generate.await_args.args[1]
            assert "Это за три смены." in prompt
            assert prompt.count("Откуда") == 1
            message.reply.assert_awaited_once()
            message.reference.channel_id = 77
            assert await cog._reply_parent(message) is None
            channel.fetch_message.assert_not_called()

    asyncio.run(scenario())


def test_plain_language_and_repetitions(monkeypatch):
    persona = AIPersonaService()
    assert persona.sanitize_ai_output("Я работаю три дня в неделю.") == "Я работаю три дня в неделю."
    assert persona.sanitize_ai_output("В чат, в чат, в чат, в чат.") == "В чат."
    cog = AIChatCog(SimpleNamespace())
    monkeypatch.delenv("AI_ENABLE_RANDOM_REPLIES", raising=False)
    assert not cog.runtime_config().enable_random_replies
    assert cog._contains_alias("Бот, откуда цифра?", ("бот",))
    assert not cog._contains_alias("Кто знает, где бот?", ("бот",))


def test_return_one_visible_answer_from_provider():
    google = {"candidates": [
        {"content": {"parts": [{"thought": True, "text": "internal"}, {"text": "Ответ"}]}},
        {"content": {"parts": [{"text": "Другой ответ"}]}},
    ]}
    responses = {"output": [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "internal"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "Ответ"}]},
    ]}
    assert _extract_gemini_text(google) == "Ответ"
    assert _extract_openai_response_text(responses) == "Ответ"
