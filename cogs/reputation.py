from __future__ import annotations

import logging

import discord
from discord.ext import commands

from bot_client import MovieBot

logger = logging.getLogger(__name__)


class ReputationCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.user_id == (self.bot.user.id if self.bot.user else None):
            return
        if str(payload.emoji) != self.bot.settings.rep_plus_emoji:
            return
        if not self.bot.db:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        giver = guild.get_member(payload.user_id)
        if giver is None:
            try:
                giver = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if giver.bot:
            return

        channel = guild.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        await self.remove_reaction_safely(message, payload.emoji, giver)

        receiver = message.author
        if receiver.id == giver.id or receiver.bot:
            return

        can_give = await self.bot.reputation.can_give_rep(self.bot.db, guild.id, giver.id)
        if not can_give:
            await channel.send(
                f"{giver.mention}, лимит 2 +реп за 24 часа исчерпан.",
                delete_after=12,
            )
            return

        await self.bot.reputation.add_rep_event(
            self.bot.db,
            guild_id=guild.id,
            giver_user_id=giver.id,
            receiver_user_id=receiver.id,
            channel_id=channel.id,
            message_id=message.id,
            rep_type="plus",
        )
        positive_rep, negative_rep = await self.bot.reputation.get_user_rep(self.bot.db, guild.id, receiver.id)
        await self.send_rep_announce(
            guild=guild,
            fallback_channel=channel,
            giver=giver,
            receiver=receiver,
            positive_rep=positive_rep,
            negative_rep=negative_rep,
        )

    async def remove_reaction_safely(
        self,
        message: discord.Message,
        emoji: discord.PartialEmoji,
        member: discord.Member,
    ) -> None:
        try:
            await message.remove_reaction(emoji, member)
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Failed to remove reputation reaction on message %s", message.id)

    async def send_rep_announce(
        self,
        guild: discord.Guild,
        fallback_channel: discord.TextChannel,
        giver: discord.Member,
        receiver: discord.User | discord.Member,
        positive_rep: int,
        negative_rep: int,
    ) -> None:
        target_channel: discord.TextChannel = fallback_channel
        configured_channel_id = self.bot.settings.rep_announce_channel_id
        if configured_channel_id:
            configured_channel = guild.get_channel(configured_channel_id)
            if configured_channel is None:
                try:
                    fetched = await self.bot.fetch_channel(configured_channel_id)
                except discord.HTTPException:
                    fetched = None
                configured_channel = fetched if isinstance(fetched, discord.TextChannel) else None
            if isinstance(configured_channel, discord.TextChannel):
                target_channel = configured_channel

        await target_channel.send(
            f"{giver.mention} поставил +реп {receiver.mention}\n"
            f"У {receiver.mention} +{positive_rep} и -{negative_rep} репутации."
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(ReputationCog(bot))
