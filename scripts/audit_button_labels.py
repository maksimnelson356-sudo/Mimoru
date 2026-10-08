"""Audit: extract every button text across app/, group them, find duplicates/inconsistencies."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# text="..." or text=f"..." on the same line as InlineKeyboardButton
pattern = re.compile(
    r'InlineKeyboardButton\(\s*(?:text=(f?)"((?:[^"\\]|\\.)*)")?',
    re.DOTALL,
)
line_pattern = re.compile(r'InlineKeyboardButton\(.*?text=(f?)"((?:[^"\\]|\\.)*)"')

texts: dict[str, list[tuple[str, int, str]]] = defaultdict(list)
emoji_by_text: dict[str, set[str]] = defaultdict(set)

for path in sorted((ROOT / "app").rglob("*.py")):
    rel = path.relative_to(ROOT).as_posix()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = line_pattern.search(line)
        if not m:
            continue
        is_f, text = m.group(1), m.group(2)
        texts[text].append((rel, lineno, is_f))
        emojis = re.findall(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]", text)
        emoji_by_text[text].update(emojis)

# 1. Buttons with f-strings containing a literal fragment (labels that vary)
fstring_texts = {t for t, occ in texts.items() if any(f for _, _, f in occ)}
print(f"TOTAL button text literals (static): {len(texts) - len(fstring_texts)}")
print(f"TOTAL button text f-string patterns: {len(fstring_texts)}")

# 2. Static texts used in more than one place (potential inconsistency if same label, different cb)
print("\n--- Static labels used in 2+ places ---")
for text, occ in sorted(texts.items()):
    static = [o for o in occ if o[2] == "f"]
    if len(static) >= 2 and text:
        files = sorted({f"{o[0]}:{o[1]}" for o in static})
        print(f"{text!r}: {len(static)}x -> {', '.join(files[:4])}{' ...' if len(files) > 4 else ''}")

# 3. Exit/cancel/back family: find labels without emoji or with different emoji
print("\n--- Exit / cancel / close labels ---")
exit_words = ("Отмен", "Закры", "Выйти", "Завершить", "Главное меню", "Назад", "Вперёд", "назад")
for text, occ in sorted(texts.items()):
    if text and any(w in text for w in exit_words):
        files = sorted({f"{o[0]}:{o[1]}" for o in occ})
        print(f"{text!r} {sorted(emoji_by_text[text])} -> {len(occ)}x -> {', '.join(files[:4])}{' ...' if len(files) > 4 else ''}")

# 4. Buttons without any text (text= None / omitted)
print("\n--- Buttons with NO static text on the line (dynamic/None) ---")
no_text = 0
for path in sorted((ROOT / "app").rglob("*.py")):
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if "InlineKeyboardButton(" in line and 'text=' not in line:
            no_text += 1
            if no_text <= 12:
                print(f"{path.relative_to(ROOT).as_posix()}:{lineno}")
print(f"total: {no_text}")
