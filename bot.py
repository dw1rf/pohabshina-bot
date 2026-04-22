import asyncio
import os
import random
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WATCHMODE_API_KEY = os.getenv("WATCHMODE_API_KEY")
WATCHMODE_REGION = "US"
WATCHMODE_LIMIT = int(os.getenv("WATCHMODE_LIMIT", "80"))

TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL", "").strip()
TRANSLATE_API_KEY = os.getenv("TRANSLATE_API_KEY", "").strip()
TRANSLATE_SOURCE_LANG = os.getenv("TRANSLATE_SOURCE_LANG", "auto").strip()
TRANSLATE_TARGET_LANG = os.getenv("TRANSLATE_TARGET_LANG", "ru").strip()
SHOW_BOTH_TITLES = os.getenv("SHOW_BOTH_TITLES", "1").strip() == "1"

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


def normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().replace("/", " ").split())


def truncate_text(value: str, limit: int) -> str:
    value = (value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


class MovieBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.session: aiohttp.ClientSession | None = None
        self.genre_name_to_id: dict[str, int] = {}
        self.genre_id_to_name: dict[int, str] = {}
        self.translation_cache: dict[str, str] = {}

    async def setup_hook(self) -> None:
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        await self.load_genres()
        await self.tree.sync()
        print(f"Бот запущен. Жанров загружено: {len(self.genre_id_to_name)}")

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

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


bot = MovieBot()


@bot.event
async def on_ready() -> None:
    await asyncio.sleep(2)
    await bot.change_presence(
        status=discord.Status.online,
        activity=discord.Game(name="Просмотр фильмов"),
    )
    print(f"Вошёл как {bot.user} (ID: {bot.user.id if bot.user else 'unknown'})")


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


async def main() -> None:
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())