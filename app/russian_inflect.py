"""Русская морфология: склонение имён по падежам через pymorphy3."""

from __future__ import annotations

import functools
from typing import Final

import pymorphy3

_morph: Final = pymorphy3.MorphAnalyzer()

CASE_NOMN: Final = "nomn"
CASE_GENT: Final = "gent"
CASE_DATV: Final = "datv"
CASE_ACCS: Final = "accs"
CASE_ABLT: Final = "ablt"
CASE_PRP: Final = "loct"  # pymorphy3 uses "loct" instead of "prp"

_VALID_CASES: Final = frozenset(
    {CASE_NOMN, CASE_GENT, CASE_DATV, CASE_ACCS, CASE_ABLT, CASE_PRP}
)


def _is_cyrillic_word(text: str) -> bool:
    if not text:
        return False
    return all("\u0400" <= ch <= "\u04FF" for ch in text)


@functools.lru_cache(maxsize=4096)
def _inflect(name: str, case: str) -> str:
    if not name:
        return name
    if case not in _VALID_CASES:
        return name
    if not _is_cyrillic_word(name):
        return name

    try:
        parsed = _morph.parse(name)
        if not parsed:
            return name
        inflected = parsed[0].inflect({case})
        if inflected is None:
            return name
        result = inflected.word

        if name[0].isupper():
            result = result[0].upper() + result[1:]
        return result
    except Exception:
        return name