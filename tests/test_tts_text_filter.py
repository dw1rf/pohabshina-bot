from __future__ import annotations

import asyncio
from types import SimpleNamespace

from cogs.tts_voice import TTSSession, TTSVoiceCog, _build_tts_caption
from utils.tts_text_filter import filter_tts_text, format_removed_words


def test_filters_minimal_dictionary_and_common_spellings() -> None:
    result = filter_tts_text("Ну, я кароче типо понял, как бы. Эээ...")

    assert result.cleaned_text == "я понял..."
    assert result.removed_words == ("Ну", "кароче", "типо", "как бы", "Эээ")
    assert result.fallback_used is False


def test_normalizes_case_yo_and_stretched_letters() -> None:
    result = filter_tts_text("НУУУ человек корочьее ЭЭЭМММ закончил")

    assert result.cleaned_text == "человек закончил"
    assert result.removed_words == ("НУУУ", "корочьее", "ЭЭЭМММ")


def test_collapses_exact_and_fuzzy_adjacent_repetitions() -> None:
    exact = filter_tts_text("молодец молодец")
    fuzzy = filter_tts_text("Молодец, моладец!")

    assert exact.cleaned_text == "молодец"
    assert exact.removed_words == ("молодец",)
    assert fuzzy.cleaned_text == "Молодец!"
    assert fuzzy.removed_words == ("моладец",)


def test_does_not_collapse_non_adjacent_repetitions() -> None:
    result = filter_tts_text("молодец ты молодец")

    assert result.cleaned_text == "молодец ты молодец"
    assert result.removed_words == ()


def test_does_not_fuzzy_match_short_or_merely_similar_words() -> None:
    result = filter_tts_text("типаж короткий эмблема кактус был")

    assert result.cleaned_text == "типаж короткий эмблема кактус был"
    assert result.removed_words == ()


def test_falls_back_when_every_word_would_be_removed() -> None:
    result = filter_tts_text("ну, эээ")

    assert result.cleaned_text == "ну, эээ"
    assert result.removed_words == ()
    assert result.fallback_used is True


def test_removes_orphan_punctuation_around_fillers() -> None:
    result = filter_tts_text("я, ну, понял — как бы — всё")

    assert result.cleaned_text == "я, понял — всё"
    assert result.removed_words == ("ну", "как бы")


def test_formats_removed_words_in_order_with_counts_by_original_spelling() -> None:
    report = format_removed_words(("Ну", "ну", "нууу", "кароче", "кароче", "моладец"))

    assert report == "Ну ×2, нууу, кароче ×2, моладец"


def test_tts_caption_uses_cleaned_text_and_reports_every_removal() -> None:
    caption = _build_tts_caption(
        "Пользователь",
        "я понял",
        ("ну", "ну", "кароче", "моладец"),
    )

    assert caption == "🔊 **Пользователь:** я понял\n🧹 Убрано: ну ×2, кароче, моладец"


def test_enqueue_attaches_removal_report_only_to_first_part() -> None:
    cog = TTSVoiceCog(SimpleNamespace())
    session = TTSSession(guild_id=1, owner_id=2, text_channel_id=3, voice_channel_id=4)
    long_text = "ну " + "альфа бета " * 30

    queued = asyncio.run(cog.enqueue_text(session, long_text, "Автор"))
    items = [session.queue.get_nowait() for _ in range(queued)]

    assert queued >= 2
    assert items[0].removed_words == ("ну",)
    assert all(item.removed_words == () for item in items[1:])
    assert all("ну" not in item.text.casefold().split() for item in items)
    assert all(item.text == item.display_text for item in items)
