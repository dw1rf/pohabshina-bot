from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)


def emoji_key(emoji: str | discord.PartialEmoji) -> str:
    if isinstance(emoji, discord.PartialEmoji):
        if emoji.id:
            return f"custom:{emoji.id}"
        return f"unicode:{emoji.name or ''}"
    parsed = discord.PartialEmoji.from_str(emoji.strip())
    if parsed.id:
        return f"custom:{parsed.id}"
    return f"unicode:{parsed.name or emoji.strip()}"


class ReactionRolesGroup(app_commands.Group):
    def __init__(self, cog: "ReactionRolesCog") -> None:
        super().__init__(name="reactionroles", description="Управление реакционными ролями")
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только администратор может управлять reaction roles.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="create", description="Создать сообщение reaction roles")
    async def create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message_title: str,
        message_description: str,
    ) -> None:
        await self.cog.create_message(interaction, channel, message_title, message_description)

    @app_commands.command(name="add", description="Добавить связку emoji -> role")
    async def add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role) -> None:
        await self.cog.add_binding(interaction, message_id, emoji, role)

    @app_commands.command(name="remove", description="Удалить связку emoji -> role")
    async def remove(self, interaction: discord.Interaction, message_id: str, emoji: str) -> None:
        await self.cog.remove_binding(interaction, message_id, emoji)

    @app_commands.command(name="list", description="Список всех reaction role сообщений")
    async def list_(self, interaction: discord.Interaction) -> None:
        await self.cog.list_messages(interaction)

    @app_commands.command(name="delete", description="Удалить message из reaction roles")
    async def delete(self, interaction: discord.Interaction, message_id: str) -> None:
        await self.cog.delete_message(interaction, message_id)


class ReactionRolesCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self.group = ReactionRolesGroup(self)
        self.bot.tree.add_command(self.group)

    def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.group.name, type=self.group.type)

    @staticmethod
    def _can_manage_role(guild: discord.Guild, role: discord.Role) -> bool:
        me = guild.me
        if not me:
            return False
        return me.guild_permissions.manage_roles and role < me.top_role and not role.is_default()

    async def _resolve_config_message(self, guild: discord.Guild, message_id: int) -> tuple[discord.TextChannel, discord.Message] | None:
        if not self.bot.db:
            return None
        config = await self.bot.reaction_roles.get_message(self.bot.db, guild.id, message_id)
        if not config:
            return None

        channel = guild.get_channel(config.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return None
        try:
            message = await channel.fetch_message(config.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return channel, message

    async def create_message(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        message_title: str,
        message_description: str,
    ) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        me = guild.me
        if not me:
            await interaction.response.send_message("Бот не найден в guild context.", ephemeral=True)
            return

        perms = channel.permissions_for(me)
        if not perms.send_messages or not perms.embed_links:
            await interaction.response.send_message("У бота нет прав Send Messages/Embed Links в выбранном канале.", ephemeral=True)
            return

        embed = discord.Embed(title=message_title, description=message_description, color=discord.Color.blurple())
        message = await channel.send(embed=embed)
        await self.bot.reaction_roles.create_message(
            self.bot.db,
            guild.id,
            channel.id,
            message.id,
            message_title,
            message_description,
            interaction.user.id,
        )
        await interaction.response.send_message(f"✅ Создано сообщение reaction roles: `{message.id}` в {channel.mention}", ephemeral=True)

    async def add_binding(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not message_id.isdigit():
            await interaction.response.send_message("message_id должен быть числом.", ephemeral=True)
            return

        target = await self._resolve_config_message(guild, int(message_id))
        if not target:
            await interaction.response.send_message("Сообщение reaction roles не найдено в конфигурации.", ephemeral=True)
            return
        _, message = target

        if not self._can_manage_role(guild, role):
            await interaction.response.send_message("Бот не может выдать эту роль (слишком высокая или нет Manage Roles).", ephemeral=True)
            return

        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            await interaction.response.send_message("Некорректный emoji или бот не может поставить реакцию.", ephemeral=True)
            return

        key = emoji_key(emoji)
        await self.bot.reaction_roles.upsert_binding(self.bot.db, guild.id, message.id, key, emoji.strip(), role.id)
        await interaction.response.send_message(f"✅ Связка добавлена: {emoji} -> {role.mention}", ephemeral=True)

    async def remove_binding(self, interaction: discord.Interaction, message_id: str, emoji: str) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not message_id.isdigit():
            await interaction.response.send_message("message_id должен быть числом.", ephemeral=True)
            return

        msg_id = int(message_id)
        target = await self._resolve_config_message(guild, msg_id)
        if not target:
            await interaction.response.send_message("Сообщение reaction roles не найдено в конфигурации.", ephemeral=True)
            return

        _, message = target
        key = emoji_key(emoji)
        deleted = await self.bot.reaction_roles.delete_binding(self.bot.db, guild.id, msg_id, key)
        if deleted:
            try:
                await message.clear_reaction(emoji)
            except (discord.HTTPException, discord.Forbidden):
                logger.warning("Failed to clear reaction %s from message %s", emoji, msg_id)
            await interaction.response.send_message(f"✅ Связка для {emoji} удалена.", ephemeral=True)
            return

        await interaction.response.send_message("Для этого emoji нет связки на указанном сообщении.", ephemeral=True)

    async def list_messages(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return

        messages = await self.bot.reaction_roles.list_messages(self.bot.db, guild.id)
        if not messages:
            await interaction.response.send_message("Список reaction roles пуст.", ephemeral=True)
            return

        blocks: list[str] = []
        for msg in messages[:20]:
            bindings = await self.bot.reaction_roles.list_bindings(self.bot.db, guild.id, msg.message_id)
            pairs = ", ".join(f"{b.emoji_display}→<@&{b.role_id}>" for b in bindings) or "(нет связок)"
            blocks.append(f"`{msg.message_id}` • <#{msg.channel_id}>\n{pairs}")

        await interaction.response.send_message("\n\n".join(blocks), ephemeral=True)

    async def delete_message(self, interaction: discord.Interaction, message_id: str) -> None:
        guild = interaction.guild
        if guild is None or not self.bot.db:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not message_id.isdigit():
            await interaction.response.send_message("message_id должен быть числом.", ephemeral=True)
            return

        config = await self.bot.reaction_roles.get_message(self.bot.db, guild.id, int(message_id))
        if not config:
            await interaction.response.send_message("Такого message_id нет в reaction roles.", ephemeral=True)
            return

        channel = guild.get_channel(config.channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                msg = await channel.fetch_message(config.message_id)
                await msg.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        deleted = await self.bot.reaction_roles.delete_message(self.bot.db, guild.id, int(message_id))
        if deleted:
            await interaction.response.send_message("✅ Конфигурация reaction roles удалена.", ephemeral=True)
        else:
            await interaction.response.send_message("Такого message_id нет в reaction roles.", ephemeral=True)

    async def _handle_raw_reaction(self, payload: discord.RawReactionActionEvent, add_role: bool) -> None:
        if payload.guild_id is None or payload.user_id == (self.bot.user.id if self.bot.user else None):
            return
        if not self.bot.db:
            return

        key = emoji_key(payload.emoji)
        binding = await self.bot.reaction_roles.find_binding(self.bot.db, payload.guild_id, payload.message_id, key)
        if not binding:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(payload.guild_id)
            except discord.HTTPException:
                return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member.bot:
            return

        role = guild.get_role(binding.role_id)
        if role is None or not self._can_manage_role(guild, role):
            return

        try:
            if add_role:
                await member.add_roles(role, reason="Reaction role add")
            else:
                await member.remove_roles(role, reason="Reaction role remove")
        except discord.Forbidden:
            logger.warning("No permissions to modify role %s for user %s", role.id, member.id)
        except discord.HTTPException:
            logger.exception("Failed to modify reaction role")

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_raw_reaction(payload, add_role=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._handle_raw_reaction(payload, add_role=False)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ReactionRolesCog(bot))
