"""Шаблоны действий для game_friendly_results.

Каждое действие имеет кортеж вариантов. В шаблонах используются плейсхолдеры:
    {actor}          — имя инициатора (именительный)
    {actor_acc}      — инициатор в винительном ("обнял Марию" — если бы actor был целью)
    {actor_ablt}     — инициатор в творительном
    {actor_dat}      — инициатор в дательном
    {actor_gent}     — инициатор в родительном
    {target}         — имя цели (именительный)
    {target_acc}     — цель в винительном ("обнял Марию")
    {target_ablt}    — цель в творительном ("ссорился с Марией")
    {target_dat}     — цель в дательном ("дал Марии")
    {target_gent}    — цель в родительном ("у Марии")

Эмодзи НЕ включается в шаблон — он добавляется отдельно через ACTION_EMOJI.
"""

from __future__ import annotations

from typing import Final

from app.russian_inflect import (
    CASE_ABLT,
    CASE_ACCS,
    CASE_DATV,
    CASE_GENT,
    CASE_NOMN,
)

# ── Действия ──────────────────────────────────────────────────────────────
# Формат: "действие": ("вариант 1", "вариант 2", ...)
# Все глаголы в прошедшем времени, согласованы с {actor}.
# Цель склоняется через {target_acc} / {target_ablt} / {target_dat} / {target_gent}.

ACTION_TEMPLATES: Final[dict[str, tuple[str, ...]]] = {

    # ── Физическая близость / нежность ────────────────────────────────────
    "обнять": (
        "{actor} обнял {target_acc} 🫂",
        "{actor} крепко обнял {target_acc} 🫂",
        "{actor} заключил {target_acc} в объятия 🫂",
    ),
    "поцеловать": (
        "{actor} поцеловал {target_acc} 💋",
        "{actor} нежно поцеловал {target_acc} 💋",
        "{actor} оставил поцелуй на щеке {target_gent} 💋",
    ),
    "засосать": (
        "{actor} засосал {target_acc} 😘",
        "{actor} увлёк {target_acc} в долгий поцелуй 😘",
    ),
    "погладить": (
        "{actor} погладил {target_acc} 🤗",
        "{actor} погладил {target_acc} по голове 🤗",
    ),
    "пощекотать": (
        "{actor} пощекотал {target_acc} 😂",
        "{actor} защекотал {target_acc} до слёз 😂",
    ),
    "ущипнуть": (
        "{actor} ущипнул {target_acc} 🤏",
        "{actor} больно ущипнул {target_acc} 🤏",
    ),

    # ── Удары / агрессия ─────────────────────────────────────────────────
    "пнуть": (
        "{actor} пнул {target_acc} 🦵",
        "{actor} отвесил пинок {target_dat} 🦵",
    ),
    "пнуть под зад": (
        "{actor} пнул {target_acc} под зад 🦵",
        "{actor} дал пинка {target_dat} 🦵",
    ),
    "дать леща": (
        "{actor} дал леща {target_dat} 👋",
        "{actor} отвесил леща {target_dat} 👋",
    ),
    "дать подзатыльник": (
        "{actor} дал подзатыльник {target_dat} 🤚",
        "{actor} хлопнул {target_acc} по затылку 🤚",
    ),
    "ударить": (
        "{actor} ударил {target_acc} 👊",
        "{actor} двинул {target_dat} по щам 👊",
        "{actor} стукнул {target_acc} 👊",
    ),
    "уебать": (
        "{actor} уебал {target_acc} 👊",
        "{actor} надавал люлей {target_dat} 👊",
    ),
    "выебать": (
        "{actor} выебал {target_acc} 😏",
        "{actor} отымел {target_acc} 😏",
    ),

    # ── Укусы / лизание ──────────────────────────────────────────────────
    "укусить": (
        "{actor} укусил {target_acc} 🦷",
        "{actor} вцепился зубами в {target_acc} 🦷",
    ),
    "покусать": (
        "{actor} покусал {target_acc} 🦷",
        "{actor} искусал {target_acc} 🦷",
    ),
    "лизнуть": (
        "{actor} лизнул {target_acc} 👅",
        "{actor} облизал {target_acc} 👅",
    ),

    # ── Отношения (ссоры / примирения) ───────────────────────────────────
    "поссориться": (
        "{actor} поссорился с {target_ablt} 💢",
        "{actor} устроил ссору с {target_ablt} 💢",
    ),
    "поругаться": (
        "{actor} поругался с {target_ablt} 💢",
        "{actor} поскандалил с {target_ablt} 💢",
    ),
    "подраться": (
        "{actor} подрался с {target_ablt} 🥊",
        "{actor} сцепился с {target_ablt} 🥊",
    ),
    "помириться": (
        "{actor} помирился с {target_ablt} 🤝",
        "{actor} заключил мир с {target_ablt} 🤝",
    ),

    # ── Комплименты / поддержка ──────────────────────────────────────────
    "похвалить": (
        "{actor} похвалил {target_acc} 👏",
        "{actor} одобрил {target_acc} 👏",
    ),
    "уважить": (
        "{actor} выразил уважение {target_dat} 🤝",
        "{actor} проникся уважением к {target_dat} 🤝",
    ),
    "сделать комплимент": (
        "{actor} сделал комплимент {target_dat} ✨",
        "{actor} сказал приятное {target_dat} ✨",
    ),
    "поклониться": (
        "{actor} поклонился {target_dat} 🙇",
        "{actor} склонил голову перед {target_ablt} 🙇",
    ),

    # ── Мистика / магия ──────────────────────────────────────────────────
    "проклясть": (
        "{actor} проклял {target_acc} 🔮",
        "{actor} наслал порчу на {target_acc} 🔮",
    ),
    "сглазить": (
        "{actor} сглазил {target_acc} 👁",
        "{actor} посмотрел недобрым взглядом на {target_acc} 👁",
    ),
    "благословить": (
        "{actor} благословил {target_acc} 🙏",
        "{actor} дал благословение {target_dat} 🙏",
    ),
    "призвать": (
        "{actor} призвал {target_acc} 🔮",
        "{actor} вызвал {target_acc} из ниоткуда 🔮",
    ),
    "воскресить": (
        "{actor} воскресил {target_acc} ✨",
        "{actor} вернул {target_acc} к жизни ✨",
    ),

    # ── Уничтожение / разрушение ─────────────────────────────────────────
    "взорвать": (
        "{actor} взорвал {target_acc} 💥",
        "{actor} подорвал {target_acc} 💥",
    ),
    "заморозить": (
        "{actor} заморозил {target_acc} 🧊",
        "{actor} обратил {target_acc} в лёд 🧊",
    ),
    "испепелить": (
        "{actor} испепелил {target_acc} 🔥",
        "{actor} сжёг {target_acc} дотла 🔥",
    ),
    "аннигилировать": (
        "{actor} аннигилировал {target_acc} 💥",
        "{actor} стёр {target_acc} в порошок 💥",
    ),

    # ── Воровство ────────────────────────────────────────────────────────
    "украсть сердце": (
        "{actor} украл сердце {target_gent} 💘",
        "{actor} завоевал сердце {target_gent} 💘",
    ),
    "украсть носок": (
        "{actor} украл носок у {target_gent} 🧦",
        "{actor} стащил носок у {target_gent} 🧦",
    ),
    "украсть одеяло": (
        "{actor} украл одеяло у {target_gent} 🛌",
        "{actor} стянул одеяло у {target_gent} 🛌",
    ),
    "украсть почку": (
        "{actor} похитил почку у {target_gent} 🫀",
        "{actor} вырвал почку у {target_gent} 🫀",
    ),

    # ── Превращения ──────────────────────────────────────────────────────
    "превратить в кота": (
        "{actor} превратил {target_acc} в кота 🐈",
        "{actor} обратил {target_acc} в котика 🐈",
    ),
    "превратить в жабу": (
        "{actor} превратил {target_acc} в жабу 🐸",
        "{actor} обратил {target_acc} в лягушонка 🐸",
    ),
    "превратить в дошик": (
        "{actor} превратил {target_acc} в дошик 🍜",
        "{actor} обратил {target_acc} в лапшу 🍜",
    ),

    # ── Разное ───────────────────────────────────────────────────────────
    "загуглить": (
        "{actor} загуглил {target_acc} 🔎",
        "{actor} погуглил про {target_acc} 🔎",
    ),
    "дать дошик": (
        "{actor} дал дошик {target_dat} 🍜",
        "{actor} угостил {target_acc} дошиком 🍜",
    ),
    "накормить": (
        "{actor} накормил {target_acc} 🍕",
        "{actor} угостил {target_acc} вкусняшкой 🍕",
    ),
    "покормить": (
        "{actor} покормил {target_acc} 🍕",
        "{actor} накормил {target_acc} до отвала 🍕",
    ),
    "арестовать": (
        "{actor} арестовал {target_acc} 🚓",
        "{actor} задержал {target_acc} 🚓",
    ),
    "забанить": (
        "{actor} забанил {target_acc} 🔨",
        "{actor} выдал бан {target_dat} 🔨",
    ),
    "закопать": (
        "{actor} закопал {target_acc} ⚰️",
        "{actor} зарыл {target_acc} ⚰️",
    ),
    "убить": (
        "{actor} убил {target_acc} ☠️",
        "{actor} ликвидировал {target_acc} ☠️",
    ),
    "съесть": (
        "{actor} съел {target_acc} 🍽",
        "{actor} схавал {target_acc} целиком 🍽",
    ),
    "зажарить": (
        "{actor} зажарил {target_acc} 🔥",
        "{actor} обжарил {target_acc} до корочки 🔥",
    ),
    "выкинуть": (
        "{actor} выкинул {target_acc} 🗑",
        "{actor} избавился от {target_gent} 🗑",
    ),
    "выкинуть в мусорку": (
        "{actor} выкинул {target_acc} в мусорку 🗑",
        "{actor} утилизировал {target_acc} 🗑",
    ),
    "посадить": (
        "{actor} посадил {target_acc} 🪑",
        "{actor} усадил {target_acc} 🪑",
    ),
    "посадить в тюрьму": (
        "{actor} посадил {target_acc} в тюрьму 🔒",
        "{actor} отдал {target_acc} под стражу 🔒",
    ),
    "изгнать": (
        "{actor} изгнал {target_acc} 🚫",
        "{actor} выгнал {target_acc} прочь 🚫",
    ),
    "выгнать из дома": (
        "{actor} выгнал {target_acc} из дома 🚪",
        "{actor} показал {target_dat} дверь 🚪",
    ),
    "поселить в подвале": (
        "{actor} поселил {target_acc} в подвале 🏚",
        "{actor} заселил {target_acc} в подвал 🏚",
    ),
    "отправить в дурку": (
        "{actor} отправил {target_acc} в дурку 🏥",
        "{actor} сдал {target_acc} в психушку 🏥",
    ),
    "отправить на завод": (
        "{actor} отправил {target_acc} на завод 🏭",
        "{actor} поставил {target_acc} за станок 🏭",
    ),
    "отправить на картошку": (
        "{actor} отправил {target_acc} на картошку 🥔",
        "{actor} сослал {target_acc} копать картошку 🥔",
    ),
    "депортировать": (
        "{actor} депортировал {target_acc} ✈️",
        "{actor} выслал {target_acc} за границу ✈️",
    ),
    "запихнуть в холодильник": (
        "{actor} запихнул {target_acc} в холодильник 🧊",
        "{actor} запер {target_acc} в холодильнике 🧊",
    ),
    "засунуть в шкаф": (
        "{actor} засунул {target_acc} в шкаф 🚪",
        "{actor} запер {target_acc} в шкафу 🚪",
    ),
    "завернуть в плед": (
        "{actor} завернул {target_acc} в плед 🛌",
        "{actor} укутал {target_acc} в плед 🛌",
    ),
    "затащить в кровать": (
        "{actor} затащил {target_acc} в кровать 🛏",
        "{actor} увлёк {target_acc} под одеяло 🛏",
    ),
    "уложить спать": (
        "{actor} уложил {target_acc} спать 😴",
        "{actor} убаюкал {target_acc} 😴",
    ),
    "усыпить": (
        "{actor} усыпил {target_acc} 😴",
        "{actor} погрузил {target_acc} в сон 😴",
    ),
    "разбудить": (
        "{actor} разбудил {target_acc} ⏰",
        "{actor} растолкал {target_acc} ⏰",
    ),
    "напоить": (
        "{actor} напоил {target_acc} 🍺",
        "{actor} угостил {target_acc} напитком 🍺",
    ),
    "дать вайфай": (
        "{actor} дал вайфай {target_dat} 📶",
        "{actor} поделился паролем с {target_ablt} 📶",
    ),
    "отключить интернет": (
        "{actor} отключил интернет {target_dat} 📵",
        "{actor} лишил {target_acc} сети 📵",
    ),
    "сломать телефон": (
        "{actor} сломал телефон {target_gent} 📱",
        "{actor} разбил экран {target_gent} 📱",
    ),
    "удалить аккаунт": (
        "{actor} удалил аккаунт {target_gent} 🗑",
        "{actor} снёс профиль {target_gent} 🗑",
    ),
    "отобрать еду": (
        "{actor} отобрал еду у {target_gent} 🍽",
        "{actor} схватил еду {target_gent} 🍽",
    ),
    "занять сотку": (
        "{actor} занял сотку у {target_gent} 💵",
        "{actor} одолжил у {target_gent} сто рублей 💵",
    ),
    "попросить денег": (
        "{actor} попросил денег у {target_gent} 💸",
        "{actor} сходил за деньгами к {target_dat} 💸",
    ),
    "подарить миллион": (
        "{actor} подарил миллион {target_dat} 💰",
        "{actor} перевёл {target_dat} лям 💰",
    ),
    "продать": (
        "{actor} продал {target_acc} 💸",
        "{actor} сдал {target_acc} под молоток 💸",
    ),
    "купить": (
        "{actor} купил {target_acc} 🛒",
        "{actor} приобрёл {target_acc} 🛒",
    ),
    "сдать в аренду": (
        "{actor} сдал {target_acc} в аренду 🏠",
        "{actor} сдаёт {target_acc} напрокат 🏠",
    ),
    "обменять": (
        "{actor} обменял {target_acc} 🔄",
        "{actor} свапнул {target_acc} 🔄",
    ),
    "усыновить": (
        "{actor} усыновил {target_acc} 👨‍👦",
        "{actor} оформил опеку над {target_ablt} 👨‍👦",
    ),
    "удочерить": (
        "{actor} удочерил {target_acc} 👨‍👧",
        "{actor} взял {target_acc} под опеку 👨‍👧",
    ),
    "сдать бабушке": (
        "{actor} сдал {target_acc} бабушке 👵",
        "{actor} отвез {target_acc} к бабушке 👵",
    ),
    "сделать админом": (
        "{actor} сделал {target_acc} админом 🛡",
        "{actor} выдал {target_dat} права админа 🛡",
    ),
    "снять админку": (
        "{actor} снял админку у {target_gent} 🔓",
        "{actor} лишил {target_acc} админки 🔓",
    ),
    "уменьшить": (
        "{actor} уменьшил {target_acc} 📏",
        "{actor} сжал {target_acc} до минимума 📏",
    ),
    "клонировать": (
        "{actor} клонировал {target_acc} 🧬",
        "{actor} сделал копию {target_gent} 🧬",
    ),
    "наколдовать понос": (
        "{actor} наколдовал {target_dat} понос 💩",
        "{actor} заколдовал {target_acc} 💩",
    ),
    "отомстить": (
        "{actor} отомстил {target_dat} ⚔️",
        "{actor} расквитался с {target_ablt} ⚔️",
    ),
    "предать": (
        "{actor} предал {target_acc} 🗡",
        "{actor} всадил нож в спину {target_dat} 🗡",
    ),
    "осудить": (
        "{actor} осудил {target_acc} ⚖️",
        "{actor} вынес приговор {target_dat} ⚖️",
    ),
    "вызвать полицию": (
        "{actor} вызвал полицию на {target_acc} 🚓",
        "{actor} донёс на {target_acc} в участок 🚓",
    ),
    "соблазнить": (
        "{actor} соблазнил {target_acc} 😏",
        "{actor} покорил {target_acc} 😏",
    ),
    "раздеть": (
        "{actor} раздел {target_acc} 👕",
        "{actor} скинул шмот с {target_gent} 👕",
    ),
    "заскамить": (
        "{actor} заскамил {target_acc} 🎭",
        "{actor} развёл {target_acc} 🎭",
    ),
    "зафрендзонить": (
        "{actor} зафрендзонил {target_acc} 🚧",
        "{actor} загнал {target_acc} в дружбу 🚧",
    ),
    "забуллить": (
        "{actor} забуллил {target_acc} 😈",
        "{actor} начал травлю {target_gent} 😈",
    ),
    "понюхать": (
        "{actor} понюхал {target_acc} 👃",
        "{actor} вдохнул аромат {target_gent} 👃",
    ),
    "понюхать волосы": (
        "{actor} понюхал волосы {target_gent} 👃",
        "{actor} вдохнул запах волос {target_gent} 👃",
    ),
    "потыкать": (
        "{actor} потыкал {target_acc} 👉",
        "{actor} ткнул пальцем в {target_acc} 👉",
    ),
}


# ── Fallback для действий, которых нет в словаре ─────────────────────────
DEFAULT_ACTION_TEMPLATES: Final[tuple[str, ...]] = (
    "{actor} использовал действие на {target_acc}",
    "{actor} применил это к {target_dat}",
)


__all__ = [
    "ACTION_TEMPLATES",
    "DEFAULT_ACTION_TEMPLATES",
    "CASE_NOMN",
    "CASE_GENT",
    "CASE_DATV",
    "CASE_ACCS",
    "CASE_ABLT",
]