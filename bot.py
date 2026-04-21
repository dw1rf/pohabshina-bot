import os
import random
import asyncio
from typing import Dict, List, Optional

import aiohttp
import discord
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")




TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


if not DISCORD_TOKEN:
    raise RuntimeError("Не задан DISCORD_TOKEN")

if not TMDB_API_KEY:
    raise RuntimeError("Не задан TMDB_API_KEY")


GENRE_ALIASES: Dict[str, List[str]] = {
    "боевик": ["action"],
    "приключения": ["adventure"],
    "мультфильм": ["animation"],
    "анимация": ["animation"],
    "комедия": ["comedy"],
    "криминал": ["crime"],
    "документальный": ["documentary"],
    "драма": ["drama"],
    "семейный": ["family"],
    "фэнтези": ["fantasy"],
    "история": ["history"],
    "ужасы": ["horror"],
    "музыка": ["music"],
    "детектив": ["mystery"],
    "мелодрама": ["romance"],
    "фантастика": ["science fiction", "sci-fi", "scifi"],
    "тв фильм": ["tv movie"],
    "военный": ["war"],
    "вестерн": ["western"],
    # английские варианты тоже поддерживаются как есть
    "action": ["action"],
    "adventure": ["adventure"],
    "animation": ["animation"],
    "comedy": ["comedy"],
    "crime": ["crime"],
    "documentary": ["documentary"],
    "drama": ["drama"],
    "family": ["family"],
    "fantasy": ["fantasy"],
    "history": ["history"],
    "horror": ["horror"],
    "music": ["music"],
    "mystery": ["mystery"],
    "romance": ["romance"],
    "science fiction": ["science fiction", "sci-fi", "scifi"],
    "tv movie": ["tv movie"],
    "war": ["war"],
    "western": ["western"],
}


class MovieBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.session: Optional[aiohttp.ClientSession] = None
        self.genre_map: Dict[str, int] = {}

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        await self.load_genres()
        await self.tree.sync()
        print("Slash-команды синхронизированы.")

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        print(f"Бот запущен как {self.user} (ID: {self.user.id})")

    async def tmdb_get(self, endpoint: str, params: Optional[Dict] = None) -> Dict:
        if not self.session:
            raise RuntimeError("HTTP-сессия не инициализирована")

        final_params = {
            "api_key": TMDB_API_KEY,
            "language": "ru-RU",
        }
        if params:
            final_params.update(params)

        url = f"{TMDB_BASE_URL}{endpoint}"
        async with self.session.get(url, params=final_params) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Ошибка TMDb {response.status}: {text[:300]}")
            return await response.json()

    async def load_genres(self) -> None:
        data = await self.tmdb_get("/genre/movie/list", params={"language": "ru"})
        genres = data.get("genres", [])

        self.genre_map.clear()
        for genre in genres:
            genre_id = genre["id"]
            name_ru = genre["name"].strip().lower()
            self.genre_map[name_ru] = genre_id

        data_en = await self.tmdb_get("/genre/movie/list", params={"language": "en"})
        for genre in data_en.get("genres", []):
            genre_id = genre["id"]
            name_en = genre["name"].strip().lower()
            self.genre_map[name_en] = genre_id

        # Добавляем пользовательские синонимы
        for alias, variants in GENRE_ALIASES.items():
            alias_lower = alias.lower()
            for variant in [alias_lower, *variants]:
                variant_lower = variant.lower()
                if variant_lower in self.genre_map:
                    self.genre_map[alias_lower] = self.genre_map[variant_lower]
                    break

    def resolve_genre_id(self, user_input: str) -> Optional[int]:
        query = user_input.strip().lower()

        if query in self.genre_map:
            return self.genre_map[query]

        # Частичное совпадение
        for name, genre_id in self.genre_map.items():
            if query in name:
                return genre_id

        # По синонимам
        for alias, variants in GENRE_ALIASES.items():
            if query == alias.lower() or query in [v.lower() for v in variants]:
                if alias.lower() in self.genre_map:
                    return self.genre_map[alias.lower()]

        return None

    async def get_recommendations(self, genre_id: int, year: Optional[int]) -> List[Dict]:
        params = {
            "with_genres": genre_id,
            "sort_by": "vote_average.desc",
            "vote_count.gte": 300,
            "include_adult": "false",
            "include_video": "false",
            "page": 1,
        }

        if year:
            params["primary_release_year"] = year

        data = await self.tmdb_get("/discover/movie", params=params)
        results = data.get("results", [])

        # Убираем фильмы без описания и даты, чтобы выдача была полезнее
        filtered = [
            movie for movie in results
            if movie.get("title") and movie.get("overview") and movie.get("release_date")
        ]

        # Небольшая случайность, чтобы бот не выдавал одно и то же всегда
        top = filtered[:15]
        random.shuffle(top)
        return top[:5]


bot = MovieBot()


@bot.tree.command(name="recommend", description="Порекомендовать фильмы по жанру и году")
@app_commands.describe(
    genre="Жанр, например: драма, комедия, ужасы, фантастика",
    year="Год выпуска, например: 2014",
)
async def recommend(interaction: discord.Interaction, genre: str, year: Optional[int] = None) -> None:
    await interaction.response.defer(thinking=True)

    if year is not None and (year < 1888 or year > 2100):
        await interaction.followup.send("Укажи корректный год в диапазоне 1888–2100.")
        return

    genre_id = bot.resolve_genre_id(genre)
    if genre_id is None:
        known_genres = [
            "боевик", "приключения", "анимация", "комедия", "криминал", "драма",
            "семейный", "фэнтези", "история", "ужасы", "детектив", "мелодрама",
            "фантастика", "военный", "вестерн"
        ]
        await interaction.followup.send(
            "Не удалось распознать жанр. Примеры: " + ", ".join(known_genres)
        )
        return

    try:
        movies = await bot.get_recommendations(genre_id, year)
    except Exception as e:
        await interaction.followup.send(f"Не удалось получить фильмы: {e}")
        return

    if not movies:
        if year:
            await interaction.followup.send(
                f"По жанру **{genre}** за **{year}** ничего не нашлось. Попробуй другой год."
            )
        else:
            await interaction.followup.send(
                f"По жанру **{genre}** ничего не нашлось. Попробуй другой жанр."
            )
        return

    embed = discord.Embed(
        title="Подборка фильмов",
        description=f"Жанр: **{genre}**" + (f" | Год: **{year}**" if year else ""),
    )

    poster = None
    for index, movie in enumerate(movies, start=1):
        title = movie.get("title", "Без названия")
        release_date = movie.get("release_date", "0000")
        release_year = release_date[:4] if release_date else "—"
        rating = movie.get("vote_average", 0)
        overview = movie.get("overview", "Описание отсутствует.")
        overview = overview[:220] + "…" if len(overview) > 220 else overview

        embed.add_field(
            name=f"{index}. {title} ({release_year})",
            value=f"Рейтинг TMDb: **{rating}**\n{overview}",
            inline=False,
        )

        if not poster and movie.get("poster_path"):
            poster = f"{TMDB_IMAGE_BASE}{movie['poster_path']}"

    if poster:
        embed.set_thumbnail(url=poster)

    embed.set_footer(text="Источник рекомендаций: TMDb")
    await interaction.followup.send(embed=embed)


async def main() -> None:
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
