from __future__ import annotations

import random
from typing import Any

import aiohttp

from config import Settings
from utils.helpers import normalize_text

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
    "Action": "Боевик", "Action & Adventure": "Боевик и приключения", "Adult": "Для взрослых", "Adventure": "Приключения",
    "Animation": "Анимация", "Anime": "Аниме", "Biography": "Биография", "Comedy": "Комедия", "Crime": "Криминал",
    "Documentary": "Документальный", "Drama": "Драма", "Family": "Семейный", "Fantasy": "Фэнтези", "Food": "Еда",
    "Game Show": "Игровое шоу", "History": "История", "Horror": "Ужасы", "Kids": "Детский", "Music": "Музыка",
    "Musical": "Мюзикл", "Mystery": "Детектив", "Nature": "Природа", "News": "Новости", "Reality": "Реалити",
    "Romance": "Мелодрама", "Sci-Fi & Fantasy": "Фантастика и фэнтези", "Science Fiction": "Фантастика", "Soap": "Мыльная опера",
    "Sports": "Спорт", "Supernatural": "Сверхъестественное", "Talk": "Ток-шоу", "Thriller": "Триллер", "Travel": "Путешествия",
    "TV Movie": "Телефильм", "War": "Военный", "War & Politics": "Война и политика", "Western": "Вестерн",
}


class WatchmodeService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.genre_name_to_id: dict[str, int] = {}
        self.genre_id_to_name: dict[int, str] = {}
        self.translation_cache: dict[str, str] = {}

    async def get(self, session: aiohttp.ClientSession, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"https://api.watchmode.com/v1{path}"
        final_params = {"apiKey": self.settings.watchmode_api_key}
        if params:
            final_params.update(params)

        async with session.get(url, params=final_params) as response:
            if response.status != 200:
                text = await response.text()
                raise RuntimeError(f"Watchmode HTTP {response.status}: {text[:500]}")
            return await response.json()

    async def translate_text(self, session: aiohttp.ClientSession, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return text

        cache_key = f"{self.settings.translate_source_lang}|{self.settings.translate_target_lang}|{text}"
        if cache_key in self.translation_cache:
            return self.translation_cache[cache_key]

        if not self.settings.translate_api_url:
            self.translation_cache[cache_key] = text
            return text

        payload = {
            "q": text,
            "source": self.settings.translate_source_lang,
            "target": self.settings.translate_target_lang,
            "format": "text",
        }
        if self.settings.translate_api_key:
            payload["api_key"] = self.settings.translate_api_key

        translated: str = text
        try:
            async with session.post(self.settings.translate_api_url, json=payload) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if isinstance(data, dict):
                        translated = data.get("translatedText") or data.get("translation") or data.get("translated") or text
                    elif isinstance(data, list) and data and isinstance(data[0], dict):
                        translated = data[0].get("translatedText") or data[0].get("translation") or data[0].get("translated") or text
        except Exception:
            translated = text

        self.translation_cache[cache_key] = str(translated).strip() or text
        return self.translation_cache[cache_key]

    async def translate_title_for_display(self, session: aiohttp.ClientSession, title: str) -> str:
        title = (title or "").strip()
        if not title:
            return "Без названия"
        translated = await self.translate_text(session, title)
        if not translated or normalize_text(translated) == normalize_text(title):
            return title
        return f"{translated} / {title}" if self.settings.show_both_titles else translated

    async def load_genres(self, session: aiohttp.ClientSession) -> None:
        data = await self.get(session, "/genres/")
        if not isinstance(data, list):
            raise RuntimeError("Watchmode вернул неожиданный формат жанров")

        self.genre_name_to_id.clear()
        self.genre_id_to_name.clear()

        for item in data:
            genre_id = item.get("id")
            genre_name = item.get("name")
            if not genre_id or not genre_name:
                continue
            genre_name_ru = GENRE_RU.get(str(genre_name), str(genre_name))
            self.genre_id_to_name[int(genre_id)] = genre_name_ru
            self.genre_name_to_id[normalize_text(str(genre_name))] = int(genre_id)
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

    @staticmethod
    def extract_year(item: dict[str, Any]) -> int | None:
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

    @staticmethod
    def is_movie(item: dict[str, Any]) -> bool:
        values = {str(item.get(k)).strip().lower() for k in ("type", "title_type", "tmdb_type") if item.get(k) is not None}
        return True if not values else "movie" in values

    @staticmethod
    def score_title(item: dict[str, Any]) -> float:
        for key in ("user_rating", "imdb_rating", "tmdb_rating", "critic_score", "relevance_percentile"):
            try:
                return float(item.get(key))
            except (TypeError, ValueError):
                continue
        return 0.0

    async def fetch_titles(self, session: aiohttp.ClientSession, genre_id: int) -> list[dict[str, Any]]:
        data = await self.get(
            session,
            "/list-titles/",
            params={
                "genre_ids": str(genre_id),
                "regions": self.settings.watchmode_region,
                "limit": str(self.settings.watchmode_limit),
            },
        )
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("titles", "results"):
                if isinstance(data.get(key), list):
                    return [x for x in data[key] if isinstance(x, dict)]
        return []

    async def recommend_movies(self, session: aiohttp.ClientSession, genre_id: int, year: int, count: int = 1) -> list[dict[str, Any]]:
        titles = await self.fetch_titles(session, genre_id)
        filtered = [item for item in titles if self.is_movie(item) and self.extract_year(item) == year and item.get("id")]
        if not filtered:
            return []
        filtered.sort(key=self.score_title, reverse=True)
        top_pool = filtered[: min(len(filtered), max(count * 5, 10))]
        random.shuffle(top_pool)
        picked = top_pool[:count]

        detailed: list[dict[str, Any]] = []
        for item in picked:
            try:
                details = await self.get(
                    session,
                    f"/title/{item['id']}/details/",
                    params={"append_to_response": "sources", "regions": self.settings.watchmode_region},
                )
                detailed.append(details if isinstance(details, dict) else item)
            except Exception:
                detailed.append(item)
        return detailed
