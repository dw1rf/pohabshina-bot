from __future__ import annotations

from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.helpers import format_dt, parse_duration, truncate_text
from utils.permissions import can_ban, can_kick, can_moderate


class ModerationCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @staticmethod
    def _guild(interaction: discord.Interaction) -> discord.Guild | None:
        return interaction.guild

    @app_commands.command(name="rules", description="Показать правила сервера")
    @app_commands.default_permissions(manage_guild=True)
    async def rules(self, interaction: discord.Interaction) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not interaction.user.guild_permissions.manage_guild and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return

        first_embed = discord.Embed(
            title="📜 Правила сервера",
            description="Пожалуйста, ознакомьтесь с правилами перед общением.",
            color=discord.Color.blue(),
        )
        first_embed.add_field(
            name="1. Уважайте участников и администрацию.",
            value="Оскорбления, травля, унижение, токсичность, угрозы.\n**Наказание:** мут 1 час → мут 6 часов → бан.",
            inline=False,
        )
        first_embed.add_field(
            name="2. Не устраивайте провокации и конфликты.",
            value="Ссоры ради ссоры, разжигание конфликтов, намеренный вывод людей на агрессию.\n**Наказание:** мут 1 час → мут 1 день → бан.",
            inline=False,
        )
        first_embed.add_field(
            name="3. Спам, флуд, капс и оффтоп запрещены.",
            value="Засорение чата сообщениями, эмодзи, пингами, бессмысленным текстом.\n**Наказание:** удаление сообщений + мут 1 час → 12 часов → 1 день.",
            inline=False,
        )
        first_embed.add_field(
            name="4. Реклама и подозрительные ссылки запрещены.",
            value="Реклама серверов, каналов, услуг, ботов, а также скам и фишинг.\n**Наказание:** бан без предупреждения.",
            inline=False,
        )
        first_embed.add_field(
            name="5. Запрещён 18+, шок-контент и запрещённый материал.",
            value="Исключение: NSFW-каналы, если такой контент разрешён их тематикой.\nЗапрещённый или незаконный контент запрещён везде.\n**Наказание:** удаление контента + мут 1 день / за тяжёлые случаи — бан сразу.",
            inline=False,
        )
        first_embed.add_field(
            name="6. Не публикуйте чужие личные данные.",
            value="Сливы переписок, фото, номеров, адресов, аккаунтов без согласия.\n**Наказание:** бан без предупреждения.",
            inline=False,
        )

        second_embed = discord.Embed(
            title="📜 Правила сервера (продолжение)",
            color=discord.Color.blue(),
        )
        second_embed.add_field(
            name="7. Соблюдайте тематику каналов.",
            value="Пишите туда, где это уместно.\n**Наказание:** предупреждение → мут 3 часа → мут 12 часов.",
            inline=False,
        )
        second_embed.add_field(
            name="8. Обход наказаний запрещён.",
            value="Твинки, заход с других аккаунтов после мута/бана.\n**Наказание:** перманентный бан всех аккаунтов.",
            inline=False,
        )
        second_embed.add_field(
            name="9. Голосовые каналы.",
            value="Крики, помехи, саундпад, музыка без согласования, намеренное мешательство.\n**Наказание:** отключение от канала / мут в войсе 1 час → 12 часов → 1 день.",
            inline=False,
        )
        second_embed.add_field(
            name="10. Ники, аватары и статусы должны быть адекватными.",
            value="Оскорбительные, провокационные, NSFW или вводящие в заблуждение профили запрещены.\n**Наказание:** требование сменить → мут 12 часов / кик / бан при отказе.",
            inline=False,
        )
        second_embed.add_field(
            name="11. Решения администрации обязательны к соблюдению.",
            value="Обсуждение наказаний — только в ЛС с администрацией.\n**Наказание:** мут 12 часов → 1 день.",
            inline=False,
        )
        second_embed.add_field(
            name="Важно",
            value="Администрация вправе пропустить этапы наказаний и выдать сразу мут/бан, если нарушение серьёзное.",
            inline=False,
        )

        await interaction.response.send_message(embeds=[first_embed, second_embed])

    @app_commands.command(name="warn", description="Выдать предупреждение пользователю")
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для выдачи предупреждений.", ephemeral=True)
            return

        total = await self.bot.levels.add_warning(self.bot.db, guild.id, user.id, interaction.user.id, reason)
        msg = f"⛔ Пользователь {user.mention} получил предупреждение (3/3). Достигнут лимит предупреждений." if total >= 3 else f"⚠️ Пользователь {user.mention} получил предупреждение ({total}/3). Причина: {reason}"
        await interaction.response.send_message(msg)
        await self.bot.send_mod_log(guild, "warn", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nПричина: {reason}\nТекущий счёт: {total}/3", discord.Color.orange())

    @app_commands.command(name="warnings", description="Показать предупреждения пользователя")
    async def warnings(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        total, rows = await self.bot.levels.get_warnings(self.bot.db, guild.id, user.id)
        embed = discord.Embed(title=f"Предупреждения: {user.display_name}", description=f"Всего предупреждений: **{total}**", color=discord.Color.gold())
        if rows:
            lines = [f"• {format_dt(row['created_at'])} — <@{row['moderator_id']}>: {truncate_text(row['reason'], 120)}" for row in rows]
            embed.add_field(name="Последние причины", value="\n".join(lines[:10]), inline=False)
        else:
            embed.add_field(name="Статус", value="У пользователя пока нет предупреждений.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarns", description="Сбросить предупреждения пользователя")
    async def clearwarns(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = self._guild(interaction)
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для сброса предупреждений.", ephemeral=True)
            return
        await self.bot.levels.clear_warnings(self.bot.db, guild.id, user.id)
        await interaction.response.send_message(f"✅ Предупреждения пользователя {user.mention} сброшены.")
        await self.bot.send_mod_log(guild, "clearwarns", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДействие: предупреждения очищены", discord.Color.green())

    @app_commands.command(name="mute", description="Выдать тайм-аут пользователю")
    async def mute(self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return

        delta = parse_duration(duration)
        if not delta:
            await interaction.response.send_message("Неверный формат duration. Используйте 30s, 10m, 1h или 2d.", ephemeral=True)
            return
        if delta > timedelta(days=28):
            await interaction.response.send_message("Максимальная длительность тайм-аута — 28 дней.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.moderate_members:
            await interaction.response.send_message("У бота нет права Moderate Members.", ephemeral=True)
            return

        try:
            await user.timeout(datetime.now(UTC) + delta, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось выдать мут: недостаточно прав или роль пользователя выше.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔇 Пользователь {user.mention} замучен на {duration}. Причина: {reason}")
        await self.bot.send_mod_log(guild, "mute", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДлительность: {duration}\nПричина: {reason}", discord.Color.dark_gold())

    @app_commands.command(name="unmute", description="Снять тайм-аут с пользователя")
    async def unmute(self, interaction: discord.Interaction, user: discord.Member) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_moderate(interaction.user):
            await interaction.response.send_message("У вас нет прав для этой команды.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.moderate_members:
            await interaction.response.send_message("У бота нет права Moderate Members.", ephemeral=True)
            return

        try:
            await user.timeout(None, reason=f"Unmute by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось снять мут: недостаточно прав или роль пользователя выше.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔊 Мут с пользователя {user.mention} снят.")
        await self.bot.send_mod_log(guild, "unmute", f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}", discord.Color.green())

    @app_commands.command(name="ban", description="Забанить пользователя")
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_ban(interaction.user):
            await interaction.response.send_message("У вас нет прав Ban Members.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.ban_members:
            await interaction.response.send_message("У бота нет права Ban Members.", ephemeral=True)
            return

        try:
            await guild.ban(user, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось забанить пользователя: недостаточно прав.", ephemeral=True)
            return

        await interaction.response.send_message(f"🔨 Пользователь {user.mention} забанен. Причина: {reason}")
        await self.bot.send_mod_log(guild, "ban", f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})\nПричина: {reason}", discord.Color.red())

    @app_commands.command(name="unban", description="Разбанить пользователя по ID")
    async def unban(self, interaction: discord.Interaction, user_id: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_ban(interaction.user):
            await interaction.response.send_message("У вас нет прав Ban Members.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.ban_members:
            await interaction.response.send_message("У бота нет права Ban Members.", ephemeral=True)
            return
        if not user_id.isdigit():
            await interaction.response.send_message("Нужно передать корректный числовой user_id.", ephemeral=True)
            return

        user = await self.bot.fetch_user(int(user_id))
        try:
            await guild.unban(user, reason=f"Unban by {interaction.user}")
        except discord.NotFound:
            await interaction.response.send_message("Этот пользователь не найден в списке банов.", ephemeral=True)
            return

        await interaction.response.send_message(f"✅ Пользователь <@{user.id}> разбанен.")
        await self.bot.send_mod_log(guild, "unban", f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})", discord.Color.green())

    @app_commands.command(name="kick", description="Исключить пользователя с сервера")
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        guild = self._guild(interaction)
        if guild is None:
            await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
            return
        if not can_kick(interaction.user):
            await interaction.response.send_message("У вас нет прав Kick Members.", ephemeral=True)
            return
        if not guild.me or not guild.me.guild_permissions.kick_members:
            await interaction.response.send_message("У бота нет права Kick Members.", ephemeral=True)
            return

        try:
            await guild.kick(user, reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message("Не удалось исключить пользователя: недостаточно прав.", ephemeral=True)
            return

        await interaction.response.send_message(f"👢 Пользователь {user.mention} исключён с сервера. Причина: {reason}")
        await self.bot.send_mod_log(guild, "kick", f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})\nПричина: {reason}", discord.Color.orange())


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ModerationCog(bot))
