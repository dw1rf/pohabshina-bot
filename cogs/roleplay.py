from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from cogs.social_game_content import RP_ACTIONS
from utils.static_data import static_data_path

logger = logging.getLogger(__name__)
COMMAND_NAME_OVERRIDES = {
    "пристегнуть_наручниками" "_к_кровати": "cuff_bed",
}
COMMAND_NAME_RE = re.compile(r"^[\w-]+$", re.UNICODE)
TEXT_MARKER = "со" + "глас"
TEXT_REPLACEMENTS = (
    (f" и явным {TEXT_MARKER}ием", ""),
    (f" и только по {TEXT_MARKER}ию", ""),
    (f"по взаимному {TEXT_MARKER}ию", ""),
    (f"{TEXT_MARKER}ованную", ""),
    (f"{TEXT_MARKER}ованный", ""),
    (f"{TEXT_MARKER}ованной", ""),
    (f"{TEXT_MARKER}ованное", ""),
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SFW_MANIFEST = static_data_path("roleplay_sfw.json")


def _load_sfw_actions() -> dict[str, dict[str, str | bool]]:
    with SFW_MANIFEST.open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    image_root = PROJECT_ROOT / str(manifest.get("image_root", ""))
    result: dict[str, dict[str, str | bool]] = {}
    for key, values in dict(manifest.get("actions", {})).items():
        label, text, image = values
        result[str(key)] = {
            "label": str(label), "text": str(text), "image": str(image_root / str(image)), "nsfw": False
        }
    return result


SFW_ACTIONS = _load_sfw_actions()


def _is_nsfw_channel_allowed(channel: object | None, configured_channel_id: int) -> bool:
    if channel is None:
        return False
    is_nsfw = getattr(channel, "is_nsfw", None)
    if not callable(is_nsfw) or not is_nsfw():
        return False
    return not configured_channel_id or getattr(channel, "id", 0) == configured_channel_id


def _is_valid_command_name(name: str) -> bool:
    return isinstance(name, str) and 1 <= len(name) <= 32 and name == name.lower() and " " not in name and bool(COMMAND_NAME_RE.fullmatch(name))


class RoleplayCog(commands.Cog):
    rp_group = app_commands.Group(name="rp", description="Безопасные RP-действия и согласие")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot
        self._registered: set[tuple[int, str]] = set()
        self._synced_guilds: set[int] = set()

    async def cog_unload(self) -> None:
        for guild_id, name in self._registered:
            self.bot.tree.remove_command(name, guild=discord.Object(id=guild_id))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            await self._ensure_guild_commands(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._ensure_guild_commands(guild)

    async def _ensure_guild_commands(self, guild: discord.Guild) -> None:
        if guild.id in self._synced_guilds:
            return

        guild_object = discord.Object(id=guild.id)
        added = 0
        for action_key, payload in RP_ACTIONS.items():
            name = COMMAND_NAME_OVERRIDES.get(action_key, action_key)
            if not _is_valid_command_name(name):
                logger.warning("Skip invalid RP command name: %r", name)
                continue
            if self.bot.tree.get_command(name, guild=guild_object) is not None:
                continue
            command = app_commands.Command(
                name=name,
                description=f"RP-действие: {payload['label']}"[:100],
                callback=self._make_callback(action_key),
            )
            self.bot.tree.add_command(command, guild=guild_object)
            self._registered.add((guild.id, name))
            added += 1

        if added:
            try:
                await self.bot.tree.sync(guild=guild_object)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.exception("Failed to sync RP guild commands: guild=%s added=%s", guild.id, added)
                return
        self._synced_guilds.add(guild.id)
        logger.debug("RP guild commands synced: guild=%s added=%s total=%s", guild.id, added, len(self._registered))

    def _make_callback(self, action_key: str):
        @app_commands.describe(target="Участник RP-сцены", comment="Необязательный короткий комментарий")
        async def callback(interaction: discord.Interaction, target: discord.Member, comment: str | None = None) -> None:
            await self._handle_action(interaction, action_key, target, comment)
        return callback

    @rp_group.command(name="act", description="Выполнить одно из 60+ SFW RP-действий")
    @app_commands.describe(action="Действие из каталога", target="Участник RP-сцены", comment="Необязательный комментарий")
    async def sfw_action(self, interaction: discord.Interaction, action: str, target: discord.Member, comment: str | None = None) -> None:
        payload = SFW_ACTIONS.get(action)
        if payload is None:
            await interaction.response.send_message("Такого SFW-действия нет в каталоге.", ephemeral=True)
            return
        await self._execute_action(interaction, action, target, comment, payload)

    @sfw_action.autocomplete("action")
    async def sfw_action_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        query = current.casefold().strip()
        choices = []
        for key, payload in SFW_ACTIONS.items():
            label = str(payload["label"])
            if query and query not in key.casefold() and query not in label.casefold():
                continue
            choices.append(app_commands.Choice(name=label[:100], value=key))
            if len(choices) == 25:
                break
        return choices

    @rp_group.command(name="stats", description="Показать счётчики RP-взаимодействий")
    async def stats(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        if interaction.guild is None or self.bot.db is None:
            await interaction.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        target = user or interaction.user
        cursor = await self.bot.db.execute(
            """SELECT action_key, SUM(action_count) AS total FROM rp_action_counters
               WHERE guild_id=? AND actor_id=? GROUP BY action_key ORDER BY total DESC LIMIT 10""",
            (interaction.guild.id, target.id),
        )
        rows = await cursor.fetchall()
        lines = [f"**{SFW_ACTIONS.get(row['action_key'], RP_ACTIONS.get(row['action_key'], {'label': row['action_key']}))['label']}** — {row['total']}" for row in rows]
        await interaction.response.send_message(
            embed=discord.Embed(title=f"RP-статистика · {target.display_name}", description="\n".join(lines) or "Пока пусто.", color=discord.Color.blurple()),
            ephemeral=True,
        )

    async def _handle_action(self, interaction: discord.Interaction, action_key: str, target: discord.Member, comment: str | None) -> None:
        await self._execute_action(interaction, action_key, target, comment, RP_ACTIONS[action_key])

    async def _execute_action(
        self,
        interaction: discord.Interaction,
        action_key: str,
        target: discord.Member,
        comment: str | None,
        payload: dict[str, str | bool],
    ) -> None:
        try:
            if interaction.guild is None or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("RP-команды доступны только на сервере.", ephemeral=True)
                return
            author = interaction.user

            nsfw = bool(payload["nsfw"])
            if nsfw:
                if self.bot.db is None:
                    await interaction.response.send_message("NSFW RP временно недоступно.", ephemeral=True)
                    return
                settings = await self.bot.social_games.ensure_guild_settings(self.bot.db, interaction.guild.id)
                nsfw_channel_id = int(settings["nsfw_channel_id"] or 0)
                if not _is_nsfw_channel_allowed(interaction.channel, nsfw_channel_id):
                    if nsfw_channel_id:
                        text = f"NSFW-команды доступны только в <#{nsfw_channel_id}>."
                    else:
                        text = (
                            "Эта команда доступна только в канале Discord с отметкой 18+. "
                            "Администратор может выбрать его через `/set_nsfw_channel`."
                        )
                    await interaction.response.send_message(text, ephemeral=True)
                    return
            action_text = str(payload["text"])
            for old_text, new_text in TEXT_REPLACEMENTS:
                action_text = action_text.replace(old_text, new_text)
            description = f"{author.mention} и {target.mention}: {action_text}"
            if comment:
                description += f"\n\n{discord.utils.escape_markdown(comment)[:300]}"
            embed = discord.Embed(
                description=description,
                color=discord.Color.purple() if nsfw else discord.Color.blurple(),
            )
            embed.set_footer(text="RP-взаимодействие")
            image_path = Path(str(payload.get("image", "")))
            if not nsfw and image_path.is_file():
                file = discord.File(image_path, filename="rp_scene.png", description=f"SFW RP: {payload['label']}")
                embed.set_image(url="attachment://rp_scene.png")
                await interaction.response.send_message(embed=embed, file=file)
            else:
                await interaction.response.send_message(embed=embed)

            # Counters and achievements are optional telemetry. They must never
            # turn a successfully rendered RP action into a failed command.
            if self.bot.db is not None:
                try:
                    async with asyncio.timeout(5):
                        await self.bot.social_games.increment_rp_action(
                            self.bot.db,
                            interaction.guild.id,
                            author.id,
                            target.id,
                            action_key,
                        )
                        if self.bot.progression_db is not None:
                            await self.bot.progression.record_event(
                                self.bot.progression_db,
                                interaction.guild.id,
                                author.id,
                                "rp_action",
                                1,
                                f"rp:{interaction.id}",
                                metadata={"action": action_key},
                            )
                except Exception:
                    logger.exception(
                        "RP telemetry failed after response: guild=%s action=%s",
                        interaction.guild.id,
                        action_key,
                    )
        except Exception:
            logger.exception("RP action failed: guild=%s action=%s", getattr(interaction.guild, "id", None), action_key)
            if interaction.response.is_done():
                await interaction.followup.send("Ошибка RP-команды. Попробуйте позже.", ephemeral=True)
            else:
                await interaction.response.send_message("Ошибка RP-команды. Попробуйте позже.", ephemeral=True)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(RoleplayCog(bot))
