from __future__ import annotations

NBSP = "\u00A0"


def indent(text: str, spaces: int = 4) -> str:
    if not text:
        return text
    return f"{NBSP * max(spaces, 0)}{text}"


def indent_lines(text: str, spaces: int = 4) -> str:
    prefix = NBSP * max(spaces, 0)
    if not text or not prefix:
        return text

    lines = text.splitlines()
    return "\n".join(f"{prefix}{line}" if line.strip() else line for line in lines)
