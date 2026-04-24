from __future__ import annotations

import discord

from config import Settings


def has_elevated_permissions(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.manage_messages


def has_bot_relay_access(member: discord.Member, settings: Settings) -> bool:
    if has_elevated_permissions(member):
        return True
    if member.id in settings.allowed_user_ids:
        return True
    role_ids = {role.id for role in member.roles}
    return bool(role_ids & settings.allowed_role_ids)


def can_moderate(member: discord.Member | discord.User | None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return perms.administrator or perms.moderate_members


def can_ban(member: discord.Member | discord.User | None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return perms.administrator or perms.ban_members


def can_kick(member: discord.Member | discord.User | None) -> bool:
    if not isinstance(member, discord.Member):
        return False
    perms = member.guild_permissions
    return perms.administrator or perms.kick_members
