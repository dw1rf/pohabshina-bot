from __future__ import annotations


REPUTATION_COMMANDS: dict[str, int] = {
    "+реп": 1,
    "+rep": 1,
    "-реп": -1,
    "-rep": -1,
}


def reputation_change(content: str | None) -> int | None:
    return REPUTATION_COMMANDS.get(str(content or "").strip().lower())


def is_reputation_command(content: str | None) -> bool:
    return reputation_change(content) is not None
