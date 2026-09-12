"""Тесты для app.russian_inflect._inflect."""

from __future__ import annotations

import pytest

from app.russian_inflect import (
    CASE_ABLT,
    CASE_ACCS,
    CASE_DATV,
    CASE_GENT,
    CASE_NOMN,
    CASE_PRP,
    _inflect,
)


class TestCyrillicNames:
    """Склонение русских имён."""

    def test_maria_accs(self) -> None:
        assert _inflect("Мария", CASE_ACCS) == "Марию"

    def test_maria_ablt(self) -> None:
        assert _inflect("Мария", CASE_ABLT) == "Марией"

    def test_maria_datv(self) -> None:
        assert _inflect("Мария", CASE_DATV) == "Марии"

    def test_maria_gent(self) -> None:
        assert _inflect("Мария", CASE_GENT) == "Марии"

    def test_maria_nomn(self) -> None:
        assert _inflect("Мария", CASE_NOMN) == "Мария"

    def test_maria_prp(self) -> None:
        assert _inflect("Мария", CASE_PRP) == "Марии"

    def test_alexey_accs(self) -> None:
        assert _inflect("Алексей", CASE_ACCS) == "Алексея"

    def test_alexey_ablt(self) -> None:
        assert _inflect("Алексей", CASE_ABLT) == "Алексеем"

    def test_ivan_accs(self) -> None:
        assert _inflect("Иван", CASE_ACCS) == "Ивана"

    def test_anna_accs(self) -> None:
        assert _inflect("Анна", CASE_ACCS) == "Анну"


class TestCasePreservation:
    """Регистр первой буквы сохраняется."""

    def test_lowercase_stays_lowercase(self) -> None:
        assert _inflect("мария", CASE_ACCS) == "марию"

    def test_uppercase_stays_uppercase(self) -> None:
        assert _inflect("Мария", CASE_ACCS) == "Марию"


class TestNonCyrillic:
    """Латиница, цифры, username — не трогаем."""

    def test_latin(self) -> None:
        assert _inflect("John", CASE_ACCS) == "John"

    def test_latin_with_digits(self) -> None:
        assert _inflect("john123", CASE_ACCS) == "john123"

    def test_username(self) -> None:
        assert _inflect("@username", CASE_ACCS) == "@username"

    def test_mixed_cyrillic_latin(self) -> None:
        # Смешанное — не чистая кириллица → не трогаем
        assert _inflect("Мария123", CASE_ACCS) == "Мария123"

    def test_with_space(self) -> None:
        # Пробел → не чистая кириллица → не трогаем
        assert _inflect("Мария Ивановна", CASE_ACCS) == "Мария Ивановна"


class TestEdgeCases:
    """Границы: пустое, неизвестный падеж, кеш."""

    def test_empty_string(self) -> None:
        assert _inflect("", CASE_ACCS) == ""

    def test_unknown_case(self) -> None:
        assert _inflect("Мария", "unknown_case") == "Мария"

    def test_empty_case(self) -> None:
        assert _inflect("Мария", "") == "Мария"

    def test_unparseable_word(self) -> None:
        # Несуществующее слово — должно вернуться как есть
        # (pymorphy3 может вернуть его же, это ок)
        result = _inflect("ыфва", CASE_ACCS)
        assert isinstance(result, str)

    def test_cache_returns_same_result(self) -> None:
        first = _inflect("Мария", CASE_ACCS)
        second = _inflect("Мария", CASE_ACCS)
        assert first == second == "Марию"

    def test_case_constants_exist(self) -> None:
        # Константы падежей на месте
        assert CASE_NOMN == "nomn"
        assert CASE_GENT == "gent"
        assert CASE_DATV == "datv"
        assert CASE_ACCS == "accs"
        assert CASE_ABLT == "ablt"
        assert CASE_PRP == "loct"