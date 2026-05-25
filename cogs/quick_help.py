from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)

USER_COOLDOWN_SECONDS = 60
CHANNEL_COOLDOWN_SECONDS = 30

RULE_TRIGGERS = ("где правила",)
ROLE_TRIGGERS = ("как получить роль", "как взять роль")
HELP_TRIGGERS = ("помогите", "что делать")

DEFAULT_RULES = (
    "1. Уважайте участников и администрацию.\n"
    "2. Не спамьте, не флудите и не провоцируйте конфликты.\n"
    "3. Не публикуйте NSFW, шок-контент, рекламу и личные данные.\n"
    "4. Следуйте темам каналов и просьбам модерации."
)


def channel_url(guild_id: int, channel_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}"


async def safe_reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    kwargs: dict[str, Any] = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to send quick-help interaction response: guild=%s", interaction.guild_id)


class QuickHelpButtons(discord.ui.View):
    def __init__(self, cog: QuickHelpCog) -> None:
        super().__init__(timeout=180)
        self.cog = cog

    @discord.ui.button(label="Правила", emoji="📜", style=discord.ButtonStyle.secondary)
    async def rules(self, interaction: discord.Interaction, button: discord.ui.Button[QuickHelpButtons]) -> None:
        await safe_reply(interaction, embed=self.cog.rules_embed(interaction.guild), view=self.cog.link_view(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Получить роль", emoji="🎭", style=discord.ButtonStyle.secondary)
    async def roles(self, interaction: discord.Interaction, button: discord.ui.Button[QuickHelpButtons]) -> None:
        await safe_reply(interaction, embed=self.cog.role_embed(interaction.guild), view=self.cog.link_view(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Магазин", emoji="🛒", style=discord.ButtonStyle.secondary)
    async def shop(self, interaction: discord.Interaction, button: discord.ui.Button[QuickHelpButtons]) -> None:
        embed = discord.Embed(
            title="Магазин",
            description="Если магазин настроен, открой канал с услугами или используй `/shop_panel`, если его опубликовала администрация.",
            color=discord.Color.blurple(),
        )
        await safe_reply(interaction, embed=embed, view=self.cog.link_view(interaction.guild), ephemeral=True)

    @discord.ui.button(label="Поддержка", emoji="🛟", style=discord.ButtonStyle.primary)
    async def support(self, interaction: discord.Interaction, button: discord.ui.Button[QuickHelpButtons]) -> None:
        embed = discord.Embed(
            title="Поддержка",
            description="Опиши вопрос в канале поддержки или создай обращение через опубликованную панель поддержки.",
            color=discord.Color.green(),
        )
        await safe_reply(interaction, embed=embed, view=self.cog.link_view(interaction.guild), ephemeral=True)


class QuickHelpCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.user_cooldowns: dict[tuple[int, int], float] = {}
        self.channel_cooldowns: dict[tuple[int, int], float] = {}

    def link_view(self, guild: discord.Guild | None) -> discord.ui.View | None:
        if guild is None:
            return None
        view = discord.ui.View(timeout=180)
        links = (
            ("Правила", "📜", self.bot.settings.rules_channel_id),
            ("Роли", "🎭", self.bot.settings.roles_channel_id),
            ("Магазин", "🛒", self.bot.settings.shop_channel_id),
            ("Поддержка", "🛟", self.bot.settings.support_channel_id),
        )
        for label, emoji, channel_id in links:
            if channel_id:
                view.add_item(discord.ui.Button(label=label, emoji=emoji, style=discord.ButtonStyle.link, url=channel_url(guild.id, channel_id)))
        return view if view.children else None

    def rules_embed(self, guild: discord.Guild | None) -> discord.Embed:
        embed = discord.Embed(
            title="Правила сервера",
            description=DEFAULT_RULES,
            color=discord.Color.blurple(),
        )
        if guild and self.bot.settings.rules_channel_id:
            embed.add_field(name="Канал правил", value=f"<#{self.bot.settings.rules_channel_id}>", inline=False)
        return embed

    def role_embed(self, guild: discord.Guild | None) -> discord.Embed:
        description = "Открой канал выбора ролей и нажми нужные реакции/кнопки. Если роль не выдалась, напиши в поддержку."
        embed = discord.Embed(title="Как получить роль", description=description, color=discord.Color.green())
        if guild and self.bot.settings.roles_channel_id:
            embed.add_field(name="Канал ролей", value=f"<#{self.bot.settings.roles_channel_id}>", inline=False)
        return embed

    def how_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Быстрая помощь",
            description="Выбери нужный раздел кнопкой ниже.",
            color=discord.Color.blurple(),
        )

    def helpme_embed(self) -> discord.Embed:
        return discord.Embed(
            title="Что нужно сделать?",
            description="Выбери, что ищешь: правила, роли, магазин или поддержку.",
            color=discord.Color.gold(),
        )

    @app_commands.command(name="rules", description="Показать правила сервера")
    @app_commands.describe(public="Показать ответ всем")
    async def rules(self, interaction: discord.Interaction, public: bool = False) -> None:
        await safe_reply(interaction, embed=self.rules_embed(interaction.guild), view=self.link_view(interaction.guild), ephemeral=not public)

    @app_commands.command(name="role", description="Объяснить, как получить роль")
    @app_commands.describe(public="Показать ответ всем")
    async def role(self, interaction: discord.Interaction, public: bool = False) -> None:
        await safe_reply(interaction, embed=self.role_embed(interaction.guild), view=self.link_view(interaction.guild), ephemeral=not public)

    @app_commands.command(name="how", description="Показать меню быстрых действий")
    @app_commands.describe(public="Показать ответ всем")
    async def how(self, interaction: discord.Interaction, public: bool = False) -> None:
        await safe_reply(interaction, embed=self.how_embed(), view=QuickHelpButtons(self), ephemeral=not public)

    @app_commands.command(name="helpme", description="Быстро открыть помощь")
    @app_commands.describe(public="Показать ответ всем")
    async def helpme(self, interaction: discord.Interaction, public: bool = False) -> None:
        await safe_reply(interaction, embed=self.helpme_embed(), view=QuickHelpButtons(self), ephemeral=not public)

    def classify_message(self, content: str) -> str | None:
        text = " ".join(content.lower().split())
        if any(trigger in text for trigger in RULE_TRIGGERS):
            return "rules"
        if any(trigger in text for trigger in ROLE_TRIGGERS):
            return "role"
        if any(trigger in text for trigger in HELP_TRIGGERS):
            return "help"
        return None

    def can_auto_reply(self, guild_id: int, channel_id: int, user_id: int) -> bool:
        now = time.monotonic()
        user_key = (guild_id, user_id)
        channel_key = (guild_id, channel_id)
        if now - self.user_cooldowns.get(user_key, 0.0) < USER_COOLDOWN_SECONDS:
            return False
        if now - self.channel_cooldowns.get(channel_key, 0.0) < CHANNEL_COOLDOWN_SECONDS:
            return False
        self.user_cooldowns[user_key] = now
        self.channel_cooldowns[channel_key] = now
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        kind = self.classify_message(message.content)
        if kind is None:
            return
        if not self.can_auto_reply(message.guild.id, message.channel.id, message.author.id):
            return
        if kind == "rules":
            embed = self.rules_embed(message.guild)
        elif kind == "role":
            embed = self.role_embed(message.guild)
        else:
            embed = self.helpme_embed()
        view = self.link_view(message.guild) or QuickHelpButtons(self)
        try:
            await message.reply(
                "Короткая подсказка:",
                embed=embed,
                view=view,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.exception("Failed to send quick auto-help: guild=%s channel=%s", message.guild.id, message.channel.id)


async def setup(bot: MovieBot) -> None:
    cog = QuickHelpCog(bot)
    if bot.tree.get_command("rules") is not None:
        logger.info("Slash command /rules already exists; quick_help will keep the existing command and load /role, /how, /helpme.")
        cog.__cog_app_commands__ = [command for command in cog.__cog_app_commands__ if command.name != "rules"]
    await bot.add_cog(cog)
