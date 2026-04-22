from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot_client import MovieBot
from utils.helpers import truncate_text


class MoviesCog(commands.Cog):
    def __init__(self, bot: MovieBot) -> None:
        self.bot = bot

    @app_commands.command(name="genres", description="Показать доступные жанры")
    async def genres(self, interaction: discord.Interaction) -> None:
        genres = self.bot.watchmode.genre_id_to_name
        if not genres:
            await interaction.response.send_message("Жанры пока не загружены.", ephemeral=True)
            return

        lines = [f"`{genre_id}` — {name}" for genre_id, name in sorted(genres.items(), key=lambda x: x[1])]
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
            f"Доступные жанры для региона `{self.bot.settings.watchmode_region}`:\n\n{chunks[0]}", ephemeral=True
        )
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(name="movie", description="Рекомендовать фильм по жанру и году")
    @app_commands.describe(genre="Жанр, например horror / drama / комедия", year="Год выпуска, например 2019")
    async def movie(self, interaction: discord.Interaction, genre: str, year: int) -> None:
        if not self.bot.session:
            await interaction.response.send_message("Сервис фильмов недоступен.", ephemeral=True)
            return

        await interaction.response.defer()
        genre_id = self.bot.watchmode.resolve_genre_id(genre)
        if genre_id is None:
            await interaction.followup.send("Не удалось распознать жанр. Используй `/genres`.")
            return
        if year < 1900 or year > 2100:
            await interaction.followup.send("Год выглядит некорректно.")
            return

        try:
            results = await self.bot.watchmode.recommend_movies(self.bot.session, genre_id, year, count=1)
        except Exception as exc:
            await interaction.followup.send(f"Ошибка при запросе к Watchmode: `{exc}`")
            return
        if not results:
            await interaction.followup.send(
                f"По жанру **{self.bot.watchmode.genre_id_to_name.get(genre_id, genre)}** и году **{year}** ничего не нашлось для региона **{self.bot.settings.watchmode_region}**."
            )
            return

        film = results[0]
        original_title = film.get("title") or film.get("name") or "Без названия"
        translated_title = await self.bot.watchmode.translate_title_for_display(self.bot.session, original_title)

        description = truncate_text(
            await self.bot.watchmode.translate_text(self.bot.session, film.get("plot_overview") or film.get("overview") or "Описание отсутствует."),
            900,
        )
        embed = discord.Embed(title=f"🎬 {translated_title}", description=description, color=discord.Color.blurple())
        embed.add_field(name="Жанр", value=self.bot.watchmode.genre_id_to_name.get(genre_id, genre), inline=True)
        embed.add_field(name="Год", value=str(self.bot.watchmode.extract_year(film) or year), inline=True)
        embed.add_field(name="Регион", value=self.bot.settings.watchmode_region, inline=True)

        rating = next((film.get(key) for key in ("user_rating", "imdb_rating", "tmdb_rating", "critic_score") if film.get(key) is not None), None)
        if rating is not None:
            embed.add_field(name="Рейтинг", value=str(rating), inline=True)
        if film.get("imdb_id"):
            embed.add_field(name="IMDb", value=f"https://www.imdb.com/title/{film['imdb_id']}/", inline=False)

        sources = film.get("sources") or []
        if isinstance(sources, list):
            names = [str(source.get("name") or source.get("source_name")) for source in sources[:5] if source.get("name") or source.get("source_name")]
            if names:
                embed.add_field(name="Где смотреть", value=", ".join(names), inline=False)

        poster = film.get("poster") or film.get("poster_url") or film.get("backdrop")
        if poster:
            embed.set_thumbnail(url=str(poster))
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="movies", description="Показать несколько фильмов по жанру и году")
    async def movies(
        self,
        interaction: discord.Interaction,
        genre: str,
        year: int,
        count: app_commands.Range[int, 1, 5] = 3,
    ) -> None:
        if not self.bot.session:
            await interaction.response.send_message("Сервис фильмов недоступен.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        genre_id = self.bot.watchmode.resolve_genre_id(genre)
        if genre_id is None:
            await interaction.followup.send("Не удалось распознать жанр. Используй `/genres`.", ephemeral=True)
            return

        try:
            results = await self.bot.watchmode.recommend_movies(self.bot.session, genre_id, year, count=count)
        except Exception as exc:
            await interaction.followup.send(f"Ошибка при запросе к Watchmode: `{exc}`", ephemeral=True)
            return

        if not results:
            await interaction.followup.send(
                f"По жанру **{self.bot.watchmode.genre_id_to_name.get(genre_id, genre)}** и году **{year}** ничего не нашлось для региона **{self.bot.settings.watchmode_region}**.",
                ephemeral=True,
            )
            return

        lines = []
        for index, film in enumerate(results, start=1):
            title = await self.bot.watchmode.translate_title_for_display(self.bot.session, film.get("title") or film.get("name") or "Без названия")
            film_year = self.bot.watchmode.extract_year(film) or year
            imdb_id = film.get("imdb_id")
            imdb_part = f" — https://www.imdb.com/title/{imdb_id}/" if imdb_id else ""
            lines.append(f"{index}. **{title}** ({film_year}) — рейтинг: {self.bot.watchmode.score_title(film):g}{imdb_part}")

        await interaction.followup.send(
            f"Подборка по жанру **{self.bot.watchmode.genre_id_to_name.get(genre_id, genre)}**, год **{year}**, регион **{self.bot.settings.watchmode_region}**:\n\n" + "\n".join(lines),
            ephemeral=True,
        )


async def setup(bot: MovieBot) -> None:
    await bot.add_cog(MoviesCog(bot))
