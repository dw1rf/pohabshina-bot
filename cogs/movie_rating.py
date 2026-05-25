from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot_client import MovieBot

logger = logging.getLogger(__name__)

RATINGS: dict[str, tuple[str, str]] = {
    "masterpiece": ("⭐", "Шедевр"),
    "liked": ("👍", "Понравилось"),
    "normal": ("👌", "Норм"),
    "bad": ("💀", "Не зашло"),
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def rating_label(rating: str) -> str:
    emoji, label = RATINGS.get(rating, ("❔", rating))
    return f"{emoji} {label}"


def short(text: str, limit: int) -> str:
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


async def safe_interaction_reply(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = True,
) -> None:
    kwargs: dict[str, Any] = {"ephemeral": ephemeral, "allowed_mentions": discord.AllowedMentions.none()}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    try:
        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        logger.exception("Failed to reply to rating interaction: guild=%s", interaction.guild_id)


class RatingCommentModal(discord.ui.Modal, title="Комментарий к фильму"):
    comment = discord.ui.TextInput(
        label="Комментарий или любимый момент",
        placeholder="Что запомнилось после просмотра?",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=False,
    )

    def __init__(self, cog: MovieRatingCog, session_id: int) -> None:
        super().__init__()
        self.cog = cog
        self.session_id = session_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_comment(interaction, self.session_id, str(self.comment.value or "").strip())


class MovieRatingView(discord.ui.View):
    def __init__(self, cog: MovieRatingCog, session_id: int, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        self.session_id = session_id
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                if item.custom_id:
                    item.custom_id = item.custom_id.replace(":0", f":{session_id}")
                item.disabled = disabled

    @discord.ui.button(label="Шедевр", emoji="⭐", style=discord.ButtonStyle.success, custom_id="movie_rating:vote:0:masterpiece")
    async def masterpiece(self, interaction: discord.Interaction, button: discord.ui.Button[MovieRatingView]) -> None:
        await self.cog.save_vote(interaction, self.session_id, "masterpiece")

    @discord.ui.button(label="Понравилось", emoji="👍", style=discord.ButtonStyle.primary, custom_id="movie_rating:vote:0:liked")
    async def liked(self, interaction: discord.Interaction, button: discord.ui.Button[MovieRatingView]) -> None:
        await self.cog.save_vote(interaction, self.session_id, "liked")

    @discord.ui.button(label="Норм", emoji="👌", style=discord.ButtonStyle.secondary, custom_id="movie_rating:vote:0:normal")
    async def normal(self, interaction: discord.Interaction, button: discord.ui.Button[MovieRatingView]) -> None:
        await self.cog.save_vote(interaction, self.session_id, "normal")

    @discord.ui.button(label="Не зашло", emoji="💀", style=discord.ButtonStyle.danger, custom_id="movie_rating:vote:0:bad")
    async def bad(self, interaction: discord.Interaction, button: discord.ui.Button[MovieRatingView]) -> None:
        await self.cog.save_vote(interaction, self.session_id, "bad")

    @discord.ui.button(label="Комментарий", emoji="✍️", style=discord.ButtonStyle.secondary, custom_id="movie_rating:comment:0")
    async def comment(self, interaction: discord.Interaction, button: discord.ui.Button[MovieRatingView]) -> None:
        try:
            await interaction.response.send_modal(RatingCommentModal(self.cog, self.session_id))
        except discord.NotFound:
            logger.warning("Rating comment interaction expired: guild=%s session=%s", interaction.guild_id, self.session_id)
        except discord.InteractionResponded:
            logger.debug("Rating comment interaction already answered: guild=%s session=%s", interaction.guild_id, self.session_id)


@dataclass(slots=True)
class RatingSummary:
    total: int
    counts: dict[str, int]
    users: dict[str, list[int]]
    comments: list[tuple[int, str]]


class MovieRatingCog(commands.Cog):
    rating_group = app_commands.Group(name="rating", description="Оценки фильмов после просмотра")

    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        await self.init_db()
        await self.restore_open_views()
        if not self.close_due_sessions.is_running():
            self.close_due_sessions.start()

    def cog_unload(self) -> None:
        if self.close_due_sessions.is_running():
            self.close_due_sessions.cancel()

    async def init_db(self) -> None:
        if self.bot.db is None:
            raise RuntimeError("Database is not initialized")
        await self.bot.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS movie_rating_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                movie_title TEXT NOT NULL,
                movie_url TEXT,
                created_by INTEGER,
                created_at TEXT NOT NULL,
                closes_at TEXT NOT NULL,
                closed_at TEXT,
                status TEXT NOT NULL DEFAULT 'open'
            );
            CREATE TABLE IF NOT EXISTS movie_rating_votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rating TEXT NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(session_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_movie_rating_sessions_status_closes
                ON movie_rating_sessions(status, closes_at);
            CREATE INDEX IF NOT EXISTS idx_movie_rating_votes_session
                ON movie_rating_votes(session_id);
            """
        )
        await self.bot.db.commit()

    async def restore_open_views(self) -> None:
        if self.bot.db is None:
            return
        cursor = await self.bot.db.execute("SELECT id FROM movie_rating_sessions WHERE status = 'open'")
        rows = await cursor.fetchall()
        for row in rows:
            session_id = int(row["id"])
            self.bot.add_view(MovieRatingView(self, session_id))
        if rows:
            logger.info("Restored movie rating views: count=%s", len(rows))

    async def get_session(self, session_id: int) -> aiosqlite.Row | None:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute("SELECT * FROM movie_rating_sessions WHERE id = ?", (session_id,))
        return await cursor.fetchone()

    async def build_summary(self, session_id: int) -> RatingSummary:
        assert self.bot.db is not None
        cursor = await self.bot.db.execute("SELECT user_id, rating, comment FROM movie_rating_votes WHERE session_id = ?", (session_id,))
        rows = await cursor.fetchall()
        counts = {key: 0 for key in RATINGS}
        users = {key: [] for key in RATINGS}
        comments: list[tuple[int, str]] = []
        for row in rows:
            rating = str(row["rating"])
            if rating in counts:
                counts[rating] += 1
                users[rating].append(int(row["user_id"]))
            comment = str(row["comment"] or "").strip()
            if comment:
                comments.append((int(row["user_id"]), comment))
        return RatingSummary(total=len(rows), counts=counts, users=users, comments=comments)

    def build_panel_embed(self, session: aiosqlite.Row, *, closed: bool = False) -> discord.Embed:
        closes_at = parse_dt(str(session["closes_at"]))
        title = "Фильм завершён. Быстрая оценка"
        embed = discord.Embed(
            title=title,
            description=(
                f"**Фильм:** {discord.utils.escape_markdown(str(session['movie_title']))}\n\n"
                "Выберите оценку кнопкой ниже.\n"
                "Можно изменить голос до закрытия голосования."
            ),
            color=discord.Color.gold() if not closed else discord.Color.dark_grey(),
            timestamp=utcnow(),
        )
        if session["movie_url"]:
            embed.add_field(name="Ссылка", value=str(session["movie_url"]), inline=False)
        if closes_at:
            embed.add_field(name="Закрытие", value=discord.utils.format_dt(closes_at, style="R"), inline=True)
        embed.add_field(name="Статус", value="голосование закрыто" if closed else "голосование открыто", inline=True)
        embed.set_footer(text=f"Rating session #{session['id']}")
        return embed

    async def build_results_embed(self, session: aiosqlite.Row) -> discord.Embed:
        summary = await self.build_summary(int(session["id"]))
        embed = discord.Embed(
            title="Итоги оценки фильма",
            description=f"**Фильм:** {discord.utils.escape_markdown(str(session['movie_title']))}",
            color=discord.Color.blurple(),
            timestamp=utcnow(),
        )
        embed.add_field(name="Всего голосов", value=str(summary.total), inline=False)
        lines = [f"{rating_label(key)} — **{summary.counts[key]}**" for key in RATINGS]
        embed.add_field(name="Оценки", value="\n".join(lines), inline=False)

        user_lines: list[str] = []
        for key in RATINGS:
            if summary.users[key]:
                mentions = ", ".join(f"<@{user_id}>" for user_id in summary.users[key])
                user_lines.append(f"**{rating_label(key)}:** {mentions}")
        if user_lines:
            embed.add_field(name="Кто как оценил", value="\n".join(user_lines)[:1024], inline=False)

        if summary.comments:
            comment_lines = [
                f"{index}. <@{user_id}>: \"{short(discord.utils.escape_markdown(comment), 140)}\""
                for index, (user_id, comment) in enumerate(summary.comments[:10], start=1)
            ]
            embed.add_field(name="Комментарии", value="\n".join(comment_lines)[:1024], inline=False)
        embed.set_footer(text=f"Rating session #{session['id']}")
        return embed

    async def save_vote(self, interaction: discord.Interaction, session_id: int, rating: str) -> None:
        if self.bot.db is None:
            await safe_interaction_reply(interaction, "База данных недоступна.")
            return
        session = await self.get_session(session_id)
        if session is None or str(session["status"]) != "open":
            await safe_interaction_reply(interaction, "Голосование уже закрыто или не найдено.")
            return
        now = iso(utcnow())
        cursor = await self.bot.db.execute(
            "SELECT rating FROM movie_rating_votes WHERE session_id = ? AND user_id = ?",
            (session_id, interaction.user.id),
        )
        previous = await cursor.fetchone()
        await self.bot.db.execute(
            """
            INSERT INTO movie_rating_votes (session_id, user_id, rating, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id, user_id) DO UPDATE SET
                rating = excluded.rating,
                updated_at = excluded.updated_at
            """,
            (session_id, interaction.user.id, rating, now, now),
        )
        await self.bot.db.commit()
        action = "обновлена" if previous else "сохранена"
        await safe_interaction_reply(interaction, f"Оценка {action}: {rating_label(rating)}")

    async def save_comment(self, interaction: discord.Interaction, session_id: int, comment: str) -> None:
        if self.bot.db is None:
            await safe_interaction_reply(interaction, "База данных недоступна.")
            return
        session = await self.get_session(session_id)
        if session is None or str(session["status"]) != "open":
            await safe_interaction_reply(interaction, "Голосование уже закрыто или не найдено.")
            return
        cursor = await self.bot.db.execute(
            "SELECT id FROM movie_rating_votes WHERE session_id = ? AND user_id = ?",
            (session_id, interaction.user.id),
        )
        vote = await cursor.fetchone()
        if vote is None:
            await safe_interaction_reply(interaction, "Сначала выберите оценку, потом оставьте комментарий.")
            return
        await self.bot.db.execute(
            "UPDATE movie_rating_votes SET comment = ?, updated_at = ? WHERE session_id = ? AND user_id = ?",
            (comment[:500], iso(utcnow()), session_id, interaction.user.id),
        )
        await self.bot.db.commit()
        await safe_interaction_reply(interaction, "Комментарий сохранён.")

    async def default_rating_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        channel_id = self.bot.settings.movie_rating_channel_id or self.bot.settings.afisha_channel_id
        if not channel_id:
            return None
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.warning("Configured rating channel is not available: guild=%s channel=%s", guild.id, channel_id)
            return None
        return fetched if isinstance(fetched, discord.TextChannel) else None

    async def publish_session(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        movie_title: str,
        duration_hours: int,
        movie_url: str | None,
        created_by: int,
    ) -> int:
        assert self.bot.db is not None
        now = utcnow()
        closes_at = now + timedelta(hours=max(1, min(duration_hours, 24 * 14)))
        cursor = await self.bot.db.execute(
            """
            INSERT INTO movie_rating_sessions
                (guild_id, channel_id, movie_title, movie_url, created_by, created_at, closes_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
            """,
            (guild.id, channel.id, movie_title, movie_url, created_by, iso(now), iso(closes_at)),
        )
        await self.bot.db.commit()
        session_id = int(cursor.lastrowid)
        session = await self.get_session(session_id)
        assert session is not None
        view = MovieRatingView(self, session_id)
        self.bot.add_view(view)
        message = await channel.send(
            embed=self.build_panel_embed(session),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.bot.db.execute("UPDATE movie_rating_sessions SET message_id = ? WHERE id = ?", (message.id, session_id))
        await self.bot.db.commit()
        logger.info("Movie rating session posted: guild=%s session=%s channel=%s", guild.id, session_id, channel.id)
        return session_id

    async def close_session(self, session_id: int, *, publish: bool = True) -> discord.Embed | None:
        if self.bot.db is None:
            return None
        session = await self.get_session(session_id)
        if session is None:
            return None
        if str(session["status"]) != "closed":
            await self.bot.db.execute(
                "UPDATE movie_rating_sessions SET status = 'closed', closed_at = ? WHERE id = ?",
                (iso(utcnow()), session_id),
            )
            await self.bot.db.commit()
            session = await self.get_session(session_id)
            assert session is not None

        guild = self.bot.get_guild(int(session["guild_id"]))
        channel = self.bot.get_channel(int(session["channel_id"]))
        results_embed = await self.build_results_embed(session)
        if publish and isinstance(channel, discord.TextChannel):
            if session["message_id"]:
                try:
                    message = await channel.fetch_message(int(session["message_id"]))
                    await message.edit(embed=self.build_panel_embed(session, closed=True), view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    logger.exception("Failed to edit closed rating panel: session=%s", session_id)
            try:
                await channel.send(embed=results_embed, allowed_mentions=discord.AllowedMentions.none())
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Failed to publish rating results: session=%s", session_id)
        elif guild is None:
            logger.warning("Rating session closed without guild cache: session=%s", session_id)
        return results_embed

    @tasks.loop(minutes=2)
    async def close_due_sessions(self) -> None:
        if self.bot.db is None:
            return
        now = iso(utcnow())
        cursor = await self.bot.db.execute("SELECT id FROM movie_rating_sessions WHERE status = 'open' AND closes_at <= ?", (now,))
        rows = await cursor.fetchall()
        for row in rows:
            await self.close_session(int(row["id"]), publish=True)

    @close_due_sessions.before_loop
    async def before_close_due_sessions(self) -> None:
        await self.bot.wait_until_ready()

    @rating_group.command(name="post", description="Опубликовать оценку фильма после просмотра")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(movie_title="Название фильма", channel="Канал для публикации", duration_hours="Сколько часов держать голосование", movie_url="Ссылка на фильм")
    async def rating_post(
        self,
        interaction: discord.Interaction,
        movie_title: str,
        channel: discord.TextChannel | None = None,
        duration_hours: app_commands.Range[int, 1, 336] = 24,
        movie_url: str | None = None,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or self.bot.db is None:
            await safe_interaction_reply(interaction, "Команда доступна только на сервере.")
            return
        if not interaction.user.guild_permissions.administrator:
            await safe_interaction_reply(interaction, "Нужны права администратора.")
            return
        target_channel = channel or await self.default_rating_channel(interaction.guild)
        if target_channel is None:
            await safe_interaction_reply(interaction, "Канал для оценок не настроен. Укажите channel или MOVIE_RATING_CHANNEL_ID/AFISHA_CHANNEL_ID.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            session_id = await self.publish_session(interaction.guild, target_channel, movie_title, int(duration_hours), movie_url, interaction.user.id)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to post rating session")
            await interaction.followup.send("Не удалось отправить панель оценки в выбранный канал.", ephemeral=True)
            return
        await interaction.followup.send(f"Голосование опубликовано: #{session_id} в {target_channel.mention}", ephemeral=True)
        # TODO: connect publish_session() to an explicit movie-finished event when the cinema/afisha scheduler exposes one.

    @rating_group.command(name="results", description="Показать текущие итоги оценки фильма")
    @app_commands.describe(session_id="ID голосования", public="Показать результат в текущем канале")
    async def rating_results(self, interaction: discord.Interaction, session_id: int, public: bool = False) -> None:
        session = await self.get_session(session_id)
        if session is None:
            await safe_interaction_reply(interaction, "Голосование не найдено.")
            return
        embed = await self.build_results_embed(session)
        await safe_interaction_reply(interaction, embed=embed, ephemeral=not public)

    @rating_group.command(name="close", description="Закрыть голосование вручную")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(session_id="ID голосования")
    async def rating_close(self, interaction: discord.Interaction, session_id: int) -> None:
        if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
            await safe_interaction_reply(interaction, "Нужны права администратора.")
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = await self.close_session(session_id, publish=True)
        if embed is None:
            await interaction.followup.send("Голосование не найдено.", ephemeral=True)
            return
        await interaction.followup.send("Голосование закрыто, итоги опубликованы.", ephemeral=True)

    @rating_group.command(name="list", description="Показать последние голосования")
    async def rating_list(self, interaction: discord.Interaction) -> None:
        if self.bot.db is None or interaction.guild is None:
            await safe_interaction_reply(interaction, "База данных недоступна.")
            return
        cursor = await self.bot.db.execute(
            "SELECT id, movie_title, status, created_at FROM movie_rating_sessions WHERE guild_id = ? ORDER BY id DESC LIMIT 10",
            (interaction.guild.id,),
        )
        rows = await cursor.fetchall()
        if not rows:
            await safe_interaction_reply(interaction, "Голосований пока нет.")
            return
        lines = [f"`#{row['id']}` {row['status']} — {short(str(row['movie_title']), 80)}" for row in rows]
        embed = discord.Embed(title="Последние оценки фильмов", description="\n".join(lines), color=discord.Color.blurple())
        await safe_interaction_reply(interaction, embed=embed)


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(MovieRatingCog(bot))
