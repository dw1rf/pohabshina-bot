import asyncio
import math
import os
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WATCHMODE_API_KEY = os.getenv("WATCHMODE_API_KEY")
WATCHMODE_REGION = "US"
WATCHMODE_LIMIT = int(os.getenv("WATCHMODE_LIMIT", "80"))
MOD_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or 0)

TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL", "").strip()
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY", "").strip()
TRANSLATE_SOURCE_LANG = os.getenv("TRANSLATE_SOURCE_LANG", "auto").strip()
TRANSLATE_TARGET_LANG = os.getenv("TRANSLATE_TARGET_LANG", "ru").strip()
SHOW_BOTH_TITLES = os.getenv("SHOW_BOTH_TITLES", "1").strip() == "1"

DB_PATH = os.getenv("SQLITE_PATH", "bot_data.sqlite3")
LEVEL_COOLDOWN_SECONDS = 15
MIN_MESSAGE_LENGTH = 2
MAX_LEVEL = 300

print("WATCHMODE_REGION =", WATCHMODE_REGION)

if not DISCORD_TOKEN:
    raise RuntimeError("Не задан DISCORD_TOKEN")

if not WATCHMODE_API_KEY:
    raise RuntimeError("Не задан WATCHMODE_API_KEY")

COMMON_GENRE_ALIASES = {
    "боевик": "action",
    "экшен": "action",
    "приключения": "adventure",
    "приключение": "adventure",
    "мультфильм": "animation",
    "анимация": "animation",
    "комедия": "comedy",
    "криминал": "crime",
    "документальный": "documentary",
    "драма": "drama",
    "семейный": "family",
    "фэнтези": "fantasy",
    "история": "history",
    "ужасы": "horror",
    "хоррор": "horror",
    "музыка": "music",
    "детектив": "mystery",
    "мелодрама": "romance",
    "романтика": "romance",
    "фантастика": "science fiction",
    "научная фантастика": "science fiction",
    "сайфай": "science fiction",
    "триллер": "thriller",
    "военный": "war",
    "вестерн": "western",
}

GENRE_RU = {
    "Action": "Боевик",
    "Action & Adventure": "Боевик и приключения",
    "Adult": "Для взрослых",
    "Adventure": "Приключения",
    "Animation": "Анимация",
    "Anime": "Аниме",
    "Biography": "Биография",
    "Comedy": "Комедия",
    "Crime": "Криминал",
    "Documentary": "Документальный",
    "Drama": "Драма",
    "Family": "Семейный",
    "Fantasy": "Фэнтези",
    "Food": "Еда",
    "Game Show": "Игровое шоу",
    "History": "История",
    "Horror": "Ужасы",
    "Kids": "Детский",
    "Music": "Музыка",
    "Musical": "Мюзикл",
    "Mystery": "Детектив",
    "Nature": "Природа",
    "News": "Новости",
    "Reality": "Реалити",
    "Romance": "Мелодрама",
    "Sci-Fi & Fantasy": "Фантастика и фэнтези",
    "Science Fiction": "Фантастика",
    "Soap": "Мыльная опера",
    "Sports": "Спорт",
    "Supernatural": "Сверхъестественное",
    "Talk": "Ток-шоу",
    "Thriller": "Триллер",
    "Travel": "Путешествия",
    "TV Movie": "Телефильм",
    "War": "Военный",
    "War & Politics": "Война и политика",
    "Western": "Вестерн",
}

PRAISES = [
    "Ты сегодня просто сияешь!",
    "У тебя отличный вкус и вайб 😎",
    "Ты очень крут(а), так держать!",
    "Ты делаешь этот сервер лучше.",
    "Ты молодец, продолжай в том же духе!",
]

DURATION_RE = re.compile(r"^(\d+)([smhd])$")


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().replace("/", " ").split())


def truncate_text(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def required_messages_for_level(level: int) -> int:
    if level <= 0:
        return 0
    return 10 * level * level


def calculate_level(message_count: int) -> int:
    if message_count <= 0:
        return 0
    raw_level = int(math.sqrt(message_count / 10))
    return max(0, min(MAX_LEVEL, raw_level))


def parse_duration(duration_text: str) -> timedelta | None:
    match = DURATION_RE.fullmatch(duration_text.strip().lower())
    if not match:
        return None

    value = int(match.group(1))
    unit = match.group(2)
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    seconds = value * unit_seconds[unit]

    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)


def format_dt(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return ts


class MovieBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.session: aiohttp.ClientSession | None = None
        self.db: aiosqlite.Connection | None = None
        self.genre_name_to_id: dict[str, int] = {}
        self.genre_id_to_name: dict[int, str] = {}
        self.translation_cache: dict[str, str] = {}

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.init_db()
        await self.load_genres()
        await self.tree.sync()
        print(f"Бот запущен. Жанров загружено: {len(self.genre_id_to_name)}")

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        if self.db:
            await self.db.close()
        await super().close()

    async def init_db(self) -> None:
        if not self.db:
            raise RuntimeError("SQLite не инициализирован")

        await self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS warning_totals (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                warn_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS levels (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 0,
                last_message_at TEXT,
                PRIMARY KEY (guild_id, user_id)
            );
            """
        )
        await self.db.commit()

    async def watchmode_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.session:
            raise RuntimeError("HTTP session не инициализирована")

        url = f"https://api.watchmode.com/v1{path}"
        final_params = {"apiKey": WATCHMODE_API_KEY}
        if params:
            final_params.update(params)

        async with self.session.get(url, params=final_params) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Watchmode HTTP {response.status}: {text[:500]}")
            return await response.json()

    async def translate_text(self, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return text

        cache_key = f"{TRANSLATE_SOURCE_LANG}|{TRANSLATE_TARGET_LANG}|{text}"
        cached = self.translation_cache.get(cache_key)
        if cached is not None:
            return cached

        if not TRANSLATE_API_URL:
            self.translation_cache[cache_key] = text
            return text

        if not self.session:
            return text

        payload = {
            "q": text,
            "source": TRANSLATE_SOURCE_LANG,
            "target": TRANSLATE_TARGET_LANG,
            "format": "text",
        }

        headers: dict[str, str] = {}
        if TRANSLATE_API_KEY:
            payload["api_key"] = TRANSLATE_API_KEY

        try:
            async with self.session.post(
                TRANSLATE_API_URL,
                json=payload,
                headers=headers,
            ) as response:
                if response.status != 200:
                    translated = text
                else:
                    data = await response.json(content_type=None)
                    translated = text

                    if isinstance(data, dict):
                        translated = (
                            data.get("translatedText")
                            or data.get("translation")
                            or data.get("translated")
                            or text
                        )
                    elif isinstance(data, list) and data:
                        first = data[0]
                        if isinstance(first, dict):
                            translated = (
                                first.get("translatedText")
                                or first.get("translation")
                                or first.get("translated")
                                or text
                            )

        except Exception:
            translated = text

        translated = str(translated).strip() or text
        self.translation_cache[cache_key] = translated
        return translated

    async def translate_title_for_display(self, title: str) -> str:
        title = (title or "").strip()
        if not title:
            return "Без названия"

        translated = await self.translate_text(title)
        if not translated or normalize_text(translated) == normalize_text(title):
            return title

        if SHOW_BOTH_TITLES:
            return f"{translated} / {title}"
        return translated

    async def load_genres(self) -> None:
        data = await self.watchmode_get("/genres/")

        if not isinstance(data, list):
            raise RuntimeError("Watchmode вернул неожиданный формат жанров")

        self.genre_name_to_id.clear()
        self.genre_id_to_name.clear()

        for item in data:
            genre_id = item.get("id")
            genre_name = item.get("name")
            if not genre_id or not genre_name:
                continue

            genre_name = str(genre_name)
            genre_name_ru = GENRE_RU.get(genre_name, genre_name)

            self.genre_id_to_name[int(genre_id)] = genre_name_ru
            self.genre_name_to_id[normalize_text(genre_name)] = int(genre_id)
            self.genre_name_to_id[normalize_text(genre_name_ru)] = int(genre_id)

        for ru_alias, english_name in COMMON_GENRE_ALIASES.items():
            normalized_en = normalize_text(english_name)
            if normalized_en in self.genre_name_to_id:
                self.genre_name_to_id[normalize_text(ru_alias)] = self.genre_name_to_id[normalized_en]

    def resolve_genre_id(self, value: str) -> int | None:
        value = value.strip()
        if value.isdigit():
            genre_id = int(value)
            return genre_id if genre_id in self.genre_id_to_name else None
        return self.genre_name_to_id.get(normalize_text(value))

    async def fetch_titles(self, genre_id: int) -> list[dict[str, Any]]:
        data = await self.watchmode_get(
            "/list-titles/",
            params={
                "genre_ids": str(genre_id),
                "regions": WATCHMODE_REGION,
                "limit": str(WATCHMODE_LIMIT),
            },
        )

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("titles", "results"):
                value = data.get(key)
                if isinstance(value, list):
                    return [x for x in value if isinstance(x, dict)]
        return []

    def extract_year(self, item: dict[str, Any]) -> int | None:
        for key in ("year", "release_year"):
            value = item.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)

        release_date = item.get("release_date")
        if isinstance(release_date, str) and len(release_date) >= 4 and release_date[:4].isdigit():
            return int(release_date[:4])

        return None

    def is_movie(self, item: dict[str, Any]) -> bool:
        candidates = [
            item.get("type"),
            item.get("title_type"),
            item.get("tmdb_type"),
        ]
        values = {str(x).strip().lower() for x in candidates if x is not None}
        if not values:
            return True
        return "movie" in values

    def score_title(self, item: dict[str, Any]) -> float:
        for key in ("user_rating", "imdb_rating", "tmdb_rating", "critic_score", "relevance_percentile"):
            value = item.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    async def recommend_movies(self, genre_id: int, year: int, count: int = 1) -> list[dict[str, Any]]:
        titles = await self.fetch_titles(genre_id)

        filtered = [
            item
            for item in titles
            if self.is_movie(item) and self.extract_year(item) == year and item.get("id")
        ]

        if not filtered:
            return []

        filtered.sort(key=self.score_title, reverse=True)
        top_pool = filtered[: min(len(filtered), max(count * 5, 10))]
        random.shuffle(top_pool)
        picked = top_pool[:count]

        detailed: list[dict[str, Any]] = []
        for item in picked:
            title_id = item.get("id")
            if not title_id:
                continue
            try:
                details = await self.watchmode_get(
                    f"/title/{title_id}/details/",
                    params={
                        "append_to_response": "sources",
                        "regions": WATCHMODE_REGION,
                    },
                )
                if isinstance(details, dict):
                    detailed.append(details)
                else:
                    detailed.append(item)
            except Exception:
                detailed.append(item)

        return detailed

    async def add_warning(self, guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
        if not self.db:
            raise RuntimeError("SQLite не инициализирован")

        ts = now_iso()
        await self.db.execute(
            """
            INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, reason, ts),
        )
        await self.db.execute(
            """
            INSERT INTO warning_totals (guild_id, user_id, warn_count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET warn_count = warn_count + 1, updated_at = excluded.updated_at
            """,
            (guild_id, user_id, ts),
        )
        await self.db.commit()

        cursor = await self.db.execute(
            "SELECT warn_count FROM warning_totals WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()
        return int(row["warn_count"] if row else 0)

    async def get_warnings(self, guild_id: int, user_id: int) -> tuple[int, list[aiosqlite.Row]]:
        if not self.db:
            raise RuntimeError("SQLite не инициализирован")

        cursor = await self.db.execute(
            "SELECT warn_count FROM warning_totals WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        total_row = await cursor.fetchone()
        total = int(total_row["warn_count"] if total_row else 0)

        cursor = await self.db.execute(
            """
            SELECT moderator_id, reason, created_at
            FROM warnings
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC
            LIMIT 20
            """,
            (guild_id, user_id),
        )
        rows = await cursor.fetchall()
        return total, rows

    async def clear_warnings(self, guild_id: int, user_id: int) -> None:
        if not self.db:
            raise RuntimeError("SQLite не инициализирован")

        await self.db.execute("DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await self.db.execute("DELETE FROM warning_totals WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await self.db.commit()

    async def update_level_progress(self, guild_id: int, user_id: int, message_ts: datetime) -> tuple[int, int, bool]:
        if not self.db:
            raise RuntimeError("SQLite не инициализирован")

        cursor = await self.db.execute(
            "SELECT message_count, level, last_message_at FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = await cursor.fetchone()

        if row is None:
            message_count = 1
            new_level = calculate_level(message_count)
            await self.db.execute(
                """
                INSERT INTO levels (guild_id, user_id, message_count, level, last_message_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (guild_id, user_id, message_count, new_level, message_ts.isoformat()),
            )
            await self.db.commit()
            return message_count, new_level, new_level > 0

        last_message_at = row["last_message_at"]
        if last_message_at:
            try:
                last_dt = datetime.fromisoformat(last_message_at)
                if (message_ts - last_dt).total_seconds() < LEVEL_COOLDOWN_SECONDS:
                    return int(row["message_count"]), int(row["level"]), False
            except ValueError:
                pass

        message_count = int(row["message_count"]) + 1
        old_level = int(row["level"])
        new_level = calculate_level(message_count)

        await self.db.execute(
            """
            UPDATE levels
            SET message_count = ?, level = ?, last_message_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (message_count, new_level, message_ts.isoformat(), guild_id, user_id),
        )
        await self.db.commit()
        return message_count, new_level, new_level > old_level

    async def get_rank(self, guild_id: int, user_id: int) -> aiosqlite.Row | None:
        if not self.db:
            return None
        cursor = await self.db.execute(
            "SELECT message_count, level FROM levels WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return await cursor.fetchone()

    async def get_top(self, guild_id: int, limit: int = 10) -> list[aiosqlite.Row]:
        if not self.db:
            return []
        cursor = await self.db.execute(
            """
            SELECT user_id, message_count, level
            FROM levels
            WHERE guild_id = ?
            ORDER BY level DESC, message_count DESC, user_id ASC
            LIMIT ?
            """,
            (guild_id, limit),
        )
        return await cursor.fetchall()

    async def send_mod_log(self, guild: discord.Guild, action: str, description: str, color: discord.Color) -> None:
        if not MOD_LOG_CHANNEL_ID:
            return
        channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
        if channel is None:
            fetched = await self.fetch_channel(MOD_LOG_CHANNEL_ID)
            if isinstance(fetched, discord.TextChannel):
                channel = fetched
        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(title=f"🛡️ Модерация: {action}", description=description, color=color)
        embed.timestamp = datetime.now(UTC)
        await channel.send(embed=embed)


bot = MovieBot()


def ensure_guild(interaction: discord.Interaction) -> discord.Guild | None:
    return interaction.guild


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


@bot.event
async def on_ready() -> None:
    await asyncio.sleep(2)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(name="Просмотр фильмов"),
    )
    print(f"Вошёл как {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or message.guild is None:
        return

    content = (message.content or "").strip()
    if len(content) < MIN_MESSAGE_LENGTH:
        return

    _, level, level_up = await bot.update_level_progress(message.guild.id, message.author.id, datetime.now(UTC))
    if level_up:
        await message.channel.send(f"🎉 {message.author.mention} достиг(ла) {level} уровня!")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        text = "У вас недостаточно прав для выполнения этой команды."
    elif isinstance(error, app_commands.CommandOnCooldown):
        text = f"Подождите {error.retry_after:.1f} сек. перед повтором команды."
    elif isinstance(error, app_commands.TransformerError):
        text = "Похоже, один из аргументов указан неверно. Проверьте формат и попробуйте снова."
    else:
        text = "Произошла ошибка при выполнении команды. Попробуйте позже."

    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(name="genres", description="Показать доступные жанры")
async def genres(interaction: discord.Interaction) -> None:
    if not bot.genre_id_to_name:
        await interaction.response.send_message("Жанры пока не загружены.", ephemeral=True)
        return

    lines = [f"`{genre_id}` — {name}" for genre_id, name in sorted(bot.genre_id_to_name.items(), key=lambda x: x[1])]

    chunks: list[str] = []
    current = ""
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}".strip()
    if current:
        chunks.append(current)

    await interaction.response.send_message(
        f"Доступные жанры для региона `{WATCHMODE_REGION}`:\n\n{chunks[0]}",
        ephemeral=True,
    )

    for chunk in chunks[1:]:
        await interaction.followup.send(chunk, ephemeral=True)


@bot.tree.command(name="movie", description="Рекомендовать фильм по жанру и году")
@app_commands.describe(
    genre="Жанр, например horror / drama / комедия",
    year="Год выпуска, например 2019",
)
async def movie(interaction: discord.Interaction, genre: str, year: int) -> None:
    await interaction.response.defer()

    genre_id = bot.resolve_genre_id(genre)
    if genre_id is None:
        await interaction.followup.send(
            "Не удалось распознать жанр. Используй `/genres`, чтобы посмотреть доступные жанры или их ID."
        )
        return

    if year < 1900 or year > 2100:
        await interaction.followup.send("Год выглядит некорректно.")
        return

    try:
        results = await bot.recommend_movies(genre_id, year, count=1)
    except Exception as e:
        await interaction.followup.send(f"Ошибка при запросе к Watchmode: `{e}`")
        return

    if not results:
        await interaction.followup.send(
            f"По жанру **{bot.genre_id_to_name.get(genre_id, genre)}** и году **{year}** ничего не нашлось для региона **{WATCHMODE_REGION}**."
        )
        return

    film = results[0]
    original_title = film.get("title") or film.get("name") or "Без названия"
    translated_title = await bot.translate_title_for_display(original_title)

    description = film.get("plot_overview") or film.get("overview") or "Описание отсутствует."
    description_ru = await bot.translate_text(description)
    description_ru = truncate_text(description_ru, 900)

    embed = discord.Embed(
        title=f"🎬 {translated_title}",
        description=description_ru,
        color=discord.Color.blurple(),
    )

    embed.add_field(name="Жанр", value=bot.genre_id_to_name.get(genre_id, genre), inline=True)
    embed.add_field(name="Год", value=str(bot.extract_year(film) or year), inline=True)
    embed.add_field(name="Регион", value=WATCHMODE_REGION, inline=True)

    rating = None
    for key in ("user_rating", "imdb_rating", "tmdb_rating", "critic_score"):
        if film.get(key) is not None:
            rating = film.get(key)
            break
    if rating is not None:
        embed.add_field(name="Рейтинг", value=str(rating), inline=True)

    imdb_id = film.get("imdb_id")
    if imdb_id:
        embed.add_field(name="IMDb", value=f"https://www.imdb.com/title/{imdb_id}/", inline=False)

    sources = film.get("sources") or []
    if isinstance(sources, list) and sources:
        names = []
        for source in sources[:5]:
            name = source.get("name") or source.get("source_name")
            if name:
                names.append(str(name))
        if names:
            embed.add_field(name="Где смотреть", value=", ".join(names), inline=False)

    poster = film.get("poster") or film.get("poster_url") or film.get("backdrop")
    if poster:
        embed.set_thumbnail(url=str(poster))

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="movies", description="Показать несколько фильмов по жанру и году")
@app_commands.describe(
    genre="Жанр, например horror / drama / комедия",
    year="Год выпуска",
    count="Сколько фильмов показать (1-5)",
)
async def movies(interaction: discord.Interaction, genre: str, year: int, count: app_commands.Range[int, 1, 5] = 3) -> None:
    await interaction.response.defer(ephemeral=True)

    genre_id = bot.resolve_genre_id(genre)
    if genre_id is None:
        await interaction.followup.send(
            "Не удалось распознать жанр. Используй `/genres`, чтобы посмотреть доступные жанры или их ID.",
            ephemeral=True,
        )
        return

    try:
        results = await bot.recommend_movies(genre_id, year, count=count)
    except Exception as e:
        await interaction.followup.send(f"Ошибка при запросе к Watchmode: `{e}`", ephemeral=True)
        return

    if not results:
        await interaction.followup.send(
            f"По жанру **{bot.genre_id_to_name.get(genre_id, genre)}** и году **{year}** ничего не нашлось для региона **{WATCHMODE_REGION}**.",
            ephemeral=True,
        )
        return

    lines = []
    for index, film in enumerate(results, start=1):
        original_title = film.get("title") or film.get("name") or "Без названия"
        translated_title = await bot.translate_title_for_display(original_title)
        film_year = bot.extract_year(film) or year
        rating = bot.score_title(film)
        imdb_id = film.get("imdb_id")
        imdb_part = f" — https://www.imdb.com/title/{imdb_id}/" if imdb_id else ""
        lines.append(f"{index}. **{translated_title}** ({film_year}) — рейтинг: {rating:g}{imdb_part}")

    await interaction.followup.send(
        f"Подборка по жанру **{bot.genre_id_to_name.get(genre_id, genre)}**, год **{year}**, регион **{WATCHMODE_REGION}**:\n\n"
        + "\n".join(lines),
        ephemeral=True,
    )


@bot.tree.command(name="warn", description="Выдать предупреждение пользователю")
@app_commands.describe(user="Пользователь", reason="Причина предупреждения")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
    guild = ensure_guild(interaction)
    if guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return
    if not can_moderate(interaction.user):
        await interaction.response.send_message("У вас нет прав для выдачи предупреждений.", ephemeral=True)
        return

    total = await bot.add_warning(guild.id, user.id, interaction.user.id, reason)
    if total >= 3:
        msg = f"⛔ Пользователь {user.mention} получил предупреждение (3/3). Достигнут лимит предупреждений."
    else:
        msg = f"⚠️ Пользователь {user.mention} получил предупреждение ({total}/3). Причина: {reason}"

    await interaction.response.send_message(msg)
    await bot.send_mod_log(
        guild,
        "warn",
        f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nПричина: {reason}\nТекущий счёт: {total}/3",
        discord.Color.orange(),
    )


@bot.tree.command(name="warnings", description="Показать предупреждения пользователя")
@app_commands.describe(user="Пользователь")
async def warnings(interaction: discord.Interaction, user: discord.Member) -> None:
    guild = ensure_guild(interaction)
    if guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    total, rows = await bot.get_warnings(guild.id, user.id)
    embed = discord.Embed(
        title=f"Предупреждения: {user.display_name}",
        description=f"Всего предупреждений: **{total}**",
        color=discord.Color.gold(),
    )

    if rows:
        lines = [
            f"• {format_dt(row['created_at'])} — <@{row['moderator_id']}>: {truncate_text(row['reason'], 120)}"
            for row in rows
        ]
        embed.add_field(name="Последние причины", value="\n".join(lines[:10]), inline=False)
    else:
        embed.add_field(name="Статус", value="У пользователя пока нет предупреждений.", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="clearwarns", description="Сбросить предупреждения пользователя")
@app_commands.describe(user="Пользователь")
async def clearwarns(interaction: discord.Interaction, user: discord.Member) -> None:
    guild = ensure_guild(interaction)
    if guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return
    if not can_moderate(interaction.user):
        await interaction.response.send_message("У вас нет прав для сброса предупреждений.", ephemeral=True)
        return

    await bot.clear_warnings(guild.id, user.id)
    await interaction.response.send_message(f"✅ Предупреждения пользователя {user.mention} сброшены.")
    await bot.send_mod_log(
        guild,
        "clearwarns",
        f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДействие: предупреждения очищены",
        discord.Color.green(),
    )


@bot.tree.command(name="mute", description="Выдать тайм-аут пользователю")
@app_commands.describe(user="Пользователь", duration="Например 30s, 10m, 1h, 2d", reason="Причина")
async def mute(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str) -> None:
    guild = ensure_guild(interaction)
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

    max_timeout = timedelta(days=28)
    if delta > max_timeout:
        await interaction.response.send_message("Максимальная длительность тайм-аута — 28 дней.", ephemeral=True)
        return

    if not guild.me or not guild.me.guild_permissions.moderate_members:
        await interaction.response.send_message("У бота нет права Moderate Members.", ephemeral=True)
        return

    until = datetime.now(UTC) + delta
    try:
        await user.timeout(until, reason=reason)
    except discord.Forbidden:
        await interaction.response.send_message("Не удалось выдать мут: недостаточно прав или роль пользователя выше.", ephemeral=True)
        return

    await interaction.response.send_message(f"🔇 Пользователь {user.mention} замучен на {duration}. Причина: {reason}")
    await bot.send_mod_log(
        guild,
        "mute",
        f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}\nДлительность: {duration}\nПричина: {reason}",
        discord.Color.dark_gold(),
    )


@bot.tree.command(name="unmute", description="Снять тайм-аут с пользователя")
@app_commands.describe(user="Пользователь")
async def unmute(interaction: discord.Interaction, user: discord.Member) -> None:
    guild = ensure_guild(interaction)
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
    await bot.send_mod_log(
        guild,
        "unmute",
        f"Модератор: {interaction.user.mention}\nПользователь: {user.mention}",
        discord.Color.green(),
    )


@bot.tree.command(name="ban", description="Забанить пользователя")
@app_commands.describe(user="Пользователь", reason="Причина")
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
    guild = ensure_guild(interaction)
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
    await bot.send_mod_log(
        guild,
        "ban",
        f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})\nПричина: {reason}",
        discord.Color.red(),
    )


@bot.tree.command(name="unban", description="Разбанить пользователя по ID")
@app_commands.describe(user_id="ID пользователя")
async def unban(interaction: discord.Interaction, user_id: str) -> None:
    guild = ensure_guild(interaction)
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

    user = await bot.fetch_user(int(user_id))
    try:
        await guild.unban(user, reason=f"Unban by {interaction.user}")
    except discord.NotFound:
        await interaction.response.send_message("Этот пользователь не найден в списке банов.", ephemeral=True)
        return

    await interaction.response.send_message(f"✅ Пользователь <@{user.id}> разбанен.")
    await bot.send_mod_log(
        guild,
        "unban",
        f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})",
        discord.Color.green(),
    )


@bot.tree.command(name="kick", description="Исключить пользователя с сервера")
@app_commands.describe(user="Пользователь", reason="Причина")
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
    guild = ensure_guild(interaction)
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
    await bot.send_mod_log(
        guild,
        "kick",
        f"Модератор: {interaction.user.mention}\nПользователь: {user} ({user.id})\nПричина: {reason}",
        discord.Color.orange(),
    )


@bot.tree.command(name="самый_красивый", description="Случайно выбрать самого красивого участника")
@app_commands.checks.cooldown(1, 5)
async def most_beautiful(interaction: discord.Interaction) -> None:
    guild = ensure_guild(interaction)
    if guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    members = [m for m in guild.members if not m.bot]
    if not members:
        await interaction.response.send_message("На сервере пока нет подходящих участников для выбора.")
        return

    chosen = random.choice(members)
    await interaction.response.send_message(f"👑 Самый красивый сегодня — {chosen.mention}")


@bot.tree.command(name="обнять", description="Обнять участника")
@app_commands.describe(user="Кого обнять")
@app_commands.checks.cooldown(1, 5)
async def hug(interaction: discord.Interaction, user: discord.Member) -> None:
    await interaction.response.send_message(f"🤗 {interaction.user.mention} обнял(а) {user.mention}")


@bot.tree.command(name="похвалить", description="Похвалить участника")
@app_commands.describe(user="Кого похвалить")
@app_commands.checks.cooldown(1, 5)
async def praise(interaction: discord.Interaction, user: discord.Member) -> None:
    text = random.choice(PRAISES)
    await interaction.response.send_message(f"🌟 {user.mention}, {text}")


@bot.tree.command(name="легенда", description="Назвать участника легендой")
@app_commands.describe(user="Кого назвать легендой")
@app_commands.checks.cooldown(1, 5)
async def legend(interaction: discord.Interaction, user: discord.Member) -> None:
    await interaction.response.send_message(f"✨ Сегодня {user.mention} официально признан легендой сервера")


@bot.tree.command(name="rank", description="Показать уровень пользователя")
@app_commands.describe(user="Пользователь (необязательно)")
async def rank(interaction: discord.Interaction, user: discord.Member | None = None) -> None:
    guild = ensure_guild(interaction)
    if guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    target = user or interaction.user
    row = await bot.get_rank(guild.id, target.id)

    if row is None:
        await interaction.response.send_message("У этого пользователя пока нет прогресса по уровням.", ephemeral=True)
        return

    level = int(row["level"])
    message_count = int(row["message_count"])
    next_level = min(MAX_LEVEL, level + 1)

    if level >= MAX_LEVEL:
        progress_text = "Достигнут максимальный уровень."
    else:
        current_req = required_messages_for_level(level)
        next_req = required_messages_for_level(next_level)
        progress_in_level = message_count - current_req
        needed = next_req - current_req
        progress_text = f"{progress_in_level}/{needed} сообщений"

    embed = discord.Embed(title=f"Ранг: {target.display_name}", color=discord.Color.blurple())
    embed.add_field(name="Уровень", value=str(level), inline=True)
    embed.add_field(name="Сообщений", value=str(message_count), inline=True)
    embed.add_field(name="Прогресс", value=progress_text, inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="top", description="Показать топ по уровням")
async def top(interaction: discord.Interaction) -> None:
    guild = ensure_guild(interaction)
    if guild is None:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    rows = await bot.get_top(guild.id, limit=10)
    if not rows:
        await interaction.response.send_message("Пока нет данных по сообщениям.", ephemeral=True)
        return

    lines = []
    for i, row in enumerate(rows, start=1):
        member = guild.get_member(int(row["user_id"]))
        name = member.mention if member else f"<@{int(row['user_id'])}>"
        lines.append(f"**{i}.** {name} — ур. {row['level']} • {row['message_count']} сообщений")

    embed = discord.Embed(title="🏆 Топ участников по уровням", description="\n".join(lines), color=discord.Color.purple())
    await interaction.response.send_message(embed=embed)


async def main() -> None:
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
