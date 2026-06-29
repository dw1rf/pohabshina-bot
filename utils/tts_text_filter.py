from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)
SPACE_RE = re.compile(r"\s+")
SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?])")
DUPLICATE_WEAK_PUNCTUATION_RE = re.compile(r"([,;:])(?:\s*[,;:])+")
WEAK_BEFORE_STRONG_PUNCTUATION_RE = re.compile(r"[,;:]+\s*([.!?])")
DUPLICATE_DASH_RE = re.compile(r"(?:\s*[—–-]\s*){2,}")
SPACE_BETWEEN_STRONG_PUNCTUATION_RE = re.compile(r"([.!?])\s+(?=[.!?])")
EXCESSIVE_DOTS_RE = re.compile(r"\.{4,}")
LEADING_ORPHAN_PUNCTUATION_RE = re.compile(r"^[\s,;:.!?—–-]+")
TRAILING_ORPHAN_PUNCTUATION_RE = re.compile(r"[\s,;:—–-]+$")
REPEATED_LETTER_RE = re.compile(r"(.)\1+")

EXACT_FILLER_WORDS = frozenset(
    {
        "ну",
        "короче",
        "кароче",
        "типа",
        "типо",
        "какбы",
        "э",
        "эм",
    }
)
FUZZY_FILLER_WORDS = frozenset(word for word in EXACT_FILLER_WORDS if len(word) >= 5)


@dataclass(frozen=True, slots=True)
class TTSFilterResult:
    cleaned_text: str
    removed_words: tuple[str, ...] = ()
    fallback_used: bool = False


@dataclass(frozen=True, slots=True)
class _WordToken:
    text: str
    normalized: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Removal:
    start: int
    end: int
    display_text: str


def _normalize_word(word: str) -> str:
    normalized = unicodedata.normalize("NFKC", word).casefold().replace("ё", "е")
    letters_only = "".join(char for char in normalized if char.isalpha())
    return REPEATED_LETTER_RE.sub(r"\1", letters_only)


def _is_within_one_edit(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False

    if len(left) == len(right):
        mismatches = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        if len(mismatches) <= 1:
            return True
        if len(mismatches) != 2 or mismatches[1] != mismatches[0] + 1:
            return False
        first, second = mismatches
        return left[first] == right[second] and left[second] == right[first]

    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = 0
    long_index = 0
    skipped = False
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        if skipped:
            return False
        skipped = True
        long_index += 1
    return True


def _is_filler_word(normalized: str) -> bool:
    if normalized in EXACT_FILLER_WORDS:
        return True
    if len(normalized) < 5:
        return False
    return any(_is_within_one_edit(normalized, filler) for filler in FUZZY_FILLER_WORDS)


def _words_are_repeats(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    return _is_within_one_edit(left, right)


def _remove_ranges(text: str, removals: list[_Removal]) -> str:
    if not removals:
        return text
    chunks: list[str] = []
    cursor = 0
    for removal in sorted(removals, key=lambda item: item.start):
        chunks.append(text[cursor : removal.start])
        chunks.append(" ")
        cursor = removal.end
    chunks.append(text[cursor:])
    return "".join(chunks)


def _clean_orphan_punctuation(text: str) -> str:
    text = SPACE_RE.sub(" ", text).strip()
    text = SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", text)
    text = DUPLICATE_WEAK_PUNCTUATION_RE.sub(r"\1", text)
    text = WEAK_BEFORE_STRONG_PUNCTUATION_RE.sub(r"\1", text)
    text = DUPLICATE_DASH_RE.sub(" — ", text)
    text = SPACE_BETWEEN_STRONG_PUNCTUATION_RE.sub(r"\1", text)
    text = EXCESSIVE_DOTS_RE.sub("...", text)
    text = LEADING_ORPHAN_PUNCTUATION_RE.sub("", text)
    text = TRAILING_ORPHAN_PUNCTUATION_RE.sub("", text)
    return SPACE_RE.sub(" ", text).strip()


def filter_tts_text(text: str) -> TTSFilterResult:
    """Remove configured filler words and adjacent repetitions from sanitized TTS text."""
    tokens = [
        _WordToken(
            text=match.group(0),
            normalized=_normalize_word(match.group(0)),
            start=match.start(),
            end=match.end(),
        )
        for match in WORD_RE.finditer(text)
    ]
    if not tokens:
        return TTSFilterResult(cleaned_text=text)

    removed_indices: set[int] = set()
    removals: list[_Removal] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            token.normalized == "как"
            and index + 1 < len(tokens)
            and tokens[index + 1].normalized == "бы"
        ):
            next_token = tokens[index + 1]
            removed_indices.update({index, index + 1})
            removals.append(
                _Removal(
                    start=token.start,
                    end=next_token.end,
                    display_text=f"{token.text} {next_token.text}",
                )
            )
            index += 2
            continue
        if _is_filler_word(token.normalized):
            removed_indices.add(index)
            removals.append(_Removal(token.start, token.end, token.text))
        index += 1

    last_kept_index: int | None = None
    for index, token in enumerate(tokens):
        if index in removed_indices:
            continue
        if last_kept_index is not None and _words_are_repeats(
            tokens[last_kept_index].normalized,
            token.normalized,
        ):
            removed_indices.add(index)
            removals.append(_Removal(token.start, token.end, token.text))
            continue
        last_kept_index = index

    if not removals:
        return TTSFilterResult(cleaned_text=text)

    cleaned_text = _clean_orphan_punctuation(_remove_ranges(text, removals))
    if not any(char.isalnum() for char in cleaned_text):
        return TTSFilterResult(cleaned_text=text, fallback_used=True)

    ordered_removals = sorted(removals, key=lambda item: item.start)
    return TTSFilterResult(
        cleaned_text=cleaned_text,
        removed_words=tuple(removal.display_text for removal in ordered_removals),
    )


def format_removed_words(removed_words: tuple[str, ...]) -> str:
    grouped: dict[str, tuple[str, int]] = {}
    for word in removed_words:
        key = SPACE_RE.sub(" ", unicodedata.normalize("NFKC", word).casefold()).strip()
        display, count = grouped.get(key, (word, 0))
        grouped[key] = (display, count + 1)

    return ", ".join(
        f"{display} ×{count}" if count > 1 else display
        for display, count in grouped.values()
    )
