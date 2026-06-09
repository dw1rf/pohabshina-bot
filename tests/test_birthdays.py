from __future__ import annotations

from datetime import date

from cogs.birthdays import format_birthday, next_birthday_date, validate_birthday


def test_validate_birthday_accepts_optional_year() -> None:
    assert validate_birthday(15, 8, None, 2026) is None
    assert format_birthday(15, 8, None) == "15 августа"


def test_validate_birthday_rejects_invalid_date() -> None:
    assert validate_birthday(31, 2, None, 2026) == "Такой даты не существует. Проверьте день и месяц."


def test_validate_birthday_rejects_out_of_range_year() -> None:
    assert validate_birthday(1, 1, 1899, 2026) == "Год рождения не может быть меньше 1900."
    assert validate_birthday(1, 1, 2027, 2026) == "Год рождения не может быть больше текущего года."


def test_next_birthday_date_handles_february_29() -> None:
    assert next_birthday_date(29, 2, date(2026, 6, 9)) == date(2028, 2, 29)
