from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot
from services.community_ops_service import (
    ActiveSuggestionLimit,
    CommunityOpsService,
    SuggestionCooldown,
    SuggestionNotFound,
    SuggestionRecord,
    VoteTally,
)
from utils.brand import BRAND_COLOR, BRAND_NAME

logger = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


def _can_manage(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild


def _suggestion_embed(record: SuggestionRecord, tally: VoteTally) -> discord.Embed:
    labels = {
        "active": "На голосовании",
        "approved": "Принято",
        "rejected": "Отклонено",
        "cancelled": "Закрыто",
    }
    colors = {
        "active": BRAND_COLOR,
        "approved": discord.Color.green(),
        "rejected": discord.Color.red(),
        "cancelled": discord.Color.dark_grey(),
    }
    embed = discord.Embed(
        title=f"Идея #{record.suggestion_id}",
        description=record.body,
        color=colors[record.status],
    )
    embed.add_field(name="За", value=str(tally.upvotes))
    embed.add_field(name="Против", value=str(tally.downvotes))
    embed.add_field(name="Статус", value=labels[record.status])
    if record.decision_reason:
        embed.add_field(name="Решение команды", value=record.decision_reason, inline=False)
    embed.set_footer(text=f"Автор: участник {record.author_id} • {BRAND_NAME}")
    return embed


class SuggestionView(discord.ui.View):
    def __init__(self, cog: object, suggestion_id: int) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.suggestion_id = suggestion_id
        self._vote_lock = asyncio.Lock()
        up = discord.ui.Button(
            label="За",
            emoji="👍",
            style=discord.ButtonStyle.success,
            custom_id=f"vulgarities:suggestion:up:{suggestion_id}",
        )
        down = discord.ui.Button(
            label="Против",
            emoji="👎",
            style=discord.ButtonStyle.danger,
            custom_id=f"vulgarities:suggestion:down:{suggestion_id}",
        )
        up.callback = self.vote_up
        down.callback = self.vote_down
        self.add_item(up)
        self.add_item(down)

    async def vote_up(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, 1)

    async def vote_down(self, interaction: discord.Interaction) -> None:
        await self._vote(interaction, -1)

    async def _vote(self, interaction: discord.Interaction, value: int) -> None:
        async with self._vote_lock:
            await self._vote_locked(interaction, value)

    async def _vote_locked(self, interaction: discord.Interaction, value: int) -> None:
        cog = self.cog
        bot = getattr(cog, "bot", None)
        service = getattr(cog, "service", None)
        if bot is None or service is None or bot.community_db is None or interaction.guild is None:
            await interaction.response.send_message(
                "Голосование сейчас недоступно.", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        try:
            record = await service.get_suggestion(bot.community_db, self.suggestion_id)
            if record is None or record.guild_id != interaction.guild.id:
                raise SuggestionNotFound("Предложение не найдено.")
            tally = await service.cast_vote(
                bot.community_db, self.suggestion_id, user_id=interaction.user.id, vote=value
            )
        except SuggestionNotFound as exc:
            await interaction.response.send_message(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        await interaction.response.edit_message(
            embed=_suggestion_embed(record, tally), view=self, allowed_mentions=NO_MENTIONS
        )
        await interaction.followup.send("Голос сохранён.", ephemeral=True, allowed_mentions=NO_MENTIONS)


class SuggestionsCog(commands.Cog):
    suggestion_group = app_commands.Group(
        name="suggestion", description="Предложения по развитию сервера"
    )

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.service = CommunityOpsService()

    async def cog_load(self) -> None:
        if self.bot.community_db is None:
            return
        await self.service.init_db(self.bot.community_db)
        await self.service.cancel_orphaned_suggestions(self.bot.community_db)
        for record in await self.service.list_active_suggestions(self.bot.community_db):
            if record.message_id is not None:
                self.bot.add_view(
                    SuggestionView(self, record.suggestion_id), message_id=record.message_id
                )
        if not self.projection_loop.is_running():
            self.projection_loop.start()

    def cog_unload(self) -> None:
        if self.projection_loop.is_running():
            self.projection_loop.cancel()

    @tasks.loop(seconds=30)
    async def projection_loop(self) -> None:
        if self.bot.community_db is None or self.bot.progression_db is None:
            return
        try:
            pending = await self.service.pending_projections(self.bot.community_db)
        except Exception:
            logger.exception("Could not read suggestion projection outbox")
            return
        for item in pending:
            try:
                await self.bot.progression.record_event(
                    self.bot.progression_db,
                    int(item["guild_id"]),
                    int(item["user_id"]),
                    str(item["event_type"]),
                    int(item["amount"]),
                    str(item["event_key"]),
                )
                await self.service.mark_projection_delivered(
                    self.bot.community_db, str(item["event_key"])
                )
            except Exception:
                logger.exception("Could not project suggestion event %s", item["event_key"])

    @projection_loop.before_loop
    async def before_projection_loop(self) -> None:
        await self.bot.wait_until_ready()

    @suggestion_group.command(name="setup", description="Выбрать канал для предложений")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        if interaction.guild is None or self.bot.community_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.set_suggestion_channel(
            self.bot.community_db,
            guild_id=interaction.guild.id,
            channel_id=channel.id,
            actor_id=interaction.user.id,
        )
        await interaction.response.send_message(
            f"Предложения будут публиковаться в канале #{channel.name}.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @suggestion_group.command(name="create", description="Предложить идею")
    @app_commands.describe(text="Коротко опишите идею")
    async def create(self, interaction: discord.Interaction, text: app_commands.Range[str, 3, 1000]) -> None:
        if interaction.guild is None or self.bot.community_db is None:
            await interaction.response.send_message(
                "Команда доступна только на сервере.", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        settings = await self.service.suggestion_settings(self.bot.community_db, interaction.guild.id)
        if settings is None:
            await interaction.response.send_message(
                "Администратор ещё не настроил канал предложений.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        channel = interaction.guild.get_channel(int(settings["channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Настроенный канал недоступен. Попросите администратора повторить настройку.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            record = await self.service.create_suggestion(
                self.bot.community_db,
                guild_id=interaction.guild.id,
                channel_id=channel.id,
                author_id=interaction.user.id,
                body=text,
            )
        except SuggestionCooldown as exc:
            await interaction.followup.send(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        except (ActiveSuggestionLimit, ValueError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
            return

        view = SuggestionView(self, record.suggestion_id)
        try:
            message = await channel.send(
                embed=_suggestion_embed(record, VoteTally(0, 0)),
                view=view,
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not publish suggestion %s", record.suggestion_id)
            await self.service.decide_suggestion(
                self.bot.community_db,
                record.suggestion_id,
                status="cancelled",
                actor_id=interaction.user.id,
                reason="Не удалось опубликовать сообщение.",
            )
            await interaction.followup.send(
                "Не получилось отправить идею в настроенный канал.",
                ephemeral=True,
                allowed_mentions=NO_MENTIONS,
            )
            return
        await self.service.bind_suggestion_message(
            self.bot.community_db, record.suggestion_id, message_id=message.id
        )
        self.bot.add_view(view, message_id=message.id)
        await interaction.followup.send(
            f"Идея #{record.suggestion_id} опубликована.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @suggestion_group.command(name="decide", description="Принять или отклонить предложение")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.choices(
        decision=[
            app_commands.Choice(name="Принять", value="approved"),
            app_commands.Choice(name="Отклонить", value="rejected"),
        ]
    )
    async def decide(
        self,
        interaction: discord.Interaction,
        suggestion_id: int,
        decision: app_commands.Choice[str],
        reason: app_commands.Range[str, 2, 500],
    ) -> None:
        if interaction.guild is None or self.bot.community_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        record = await self.service.get_suggestion(self.bot.community_db, suggestion_id)
        if record is None or record.guild_id != interaction.guild.id:
            await interaction.response.send_message(
                "Активное предложение не найдено.", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        try:
            record = await self.service.decide_suggestion(
                self.bot.community_db,
                suggestion_id,
                status=decision.value,
                actor_id=interaction.user.id,
                reason=reason,
            )
        except (SuggestionNotFound, ValueError) as exc:
            await interaction.response.send_message(str(exc), ephemeral=True, allowed_mentions=NO_MENTIONS)
            return
        tally = await self.service.vote_tally(self.bot.community_db, suggestion_id)
        if record.message_id is not None:
            channel = interaction.guild.get_channel(record.channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    message = await channel.fetch_message(record.message_id)
                    await message.edit(
                        embed=_suggestion_embed(record, tally), view=None, allowed_mentions=NO_MENTIONS
                    )
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    logger.warning("Could not update decided suggestion %s", suggestion_id)
        await interaction.response.send_message(
            f"Решение для идеи #{suggestion_id} сохранено.",
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )

    @suggestion_group.command(name="status", description="Показать настройку предложений")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.community_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        settings = await self.service.suggestion_settings(self.bot.community_db, interaction.guild.id)
        text = (
            f"Предложения включены, канал ID: {int(settings['channel_id'])}."
            if settings is not None
            else "Предложения выключены."
        )
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)

    @suggestion_group.command(name="off", description="Выключить новые предложения")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def disable(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or self.bot.community_db is None or not _can_manage(interaction):
            await interaction.response.send_message(
                "Нужно право «Управлять сервером».", ephemeral=True, allowed_mentions=NO_MENTIONS
            )
            return
        await self.service.disable_suggestions(
            self.bot.community_db, guild_id=interaction.guild.id, actor_id=interaction.user.id
        )
        await interaction.response.send_message(
            "Новые предложения выключены.", ephemeral=True, allowed_mentions=NO_MENTIONS
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(SuggestionsCog(bot))
