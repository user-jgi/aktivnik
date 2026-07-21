"""Извлечение даты и времени из текста поста — без ИИ, на регулярках.

Нужно, чтобы не гонять в Groq посты, в которых заведомо нет анонса:
у настоящего анонса почти всегда есть «когда». Заодно нормализуем дату,
чтобы ловить дубли «та же группа + та же дата + то же время».

Относительные даты («сегодня», «в пятницу») считаются от даты САМОГО ПОСТА,
а не от момента обработки — иначе пост, разобранный на сутки позже, съедет.
"""
import re
from datetime import date as _date
from datetime import datetime, timedelta

# ── словари ──────────────────────────────────────────────────────────────────

_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6,
    "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
# порядок важен: длинные корни раньше, иначе «ма» съест «март»/«май»
_MONTH_RE = (
    r"(январ|феврал|март|апрел|май|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)"
)

_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3,
    "пятниц": 4, "суббот": 5, "воскресень": 6,
}

_ALLDAY_RE = re.compile(
    r"\b(весь день|целый день|в течение (?:всего )?дня|весь вечер)\b", re.I
)

# 01.08 / 1.8.2026 / 01/08 / 1-8
_NUM_DATE_RE = re.compile(r"\b(\d{1,2})[./\-](\d{1,2})(?:[./\-](\d{2,4}))?\b")

# 1 августа / 1-го августа / 01 авг
_WORD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:-?\s*(?:го|е|ое|ого))?\s*" + _MONTH_RE, re.I
)

# 18:00 — двоеточие однозначно время
_TIME_COLON_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
# «в 18.00» / «в 18:30» — точка как время только после предлога
_TIME_PREP_RE = re.compile(r"\bв\s+([01]?\d|2[0-3])[.:]([0-5]\d)\b", re.I)
# «в 18 часов» / «в 19 ч» / «18 часов»
_TIME_HOUR_RE = re.compile(
    r"\b(?:в\s+)?([01]?\d|2[0-3])\s*(?:ч\.?|час(?:ов|а)?)\b", re.I
)

# «вчера» тоже разбираем — такая дата уедет в прошлое и пост отсеется как отчёт
_REL = {"позавчера": -2, "вчера": -1, "сегодня": 0, "послезавтра": 2, "завтра": 1}


# ── разбор ───────────────────────────────────────────────────────────────────

def _month_num(stem: str) -> int | None:
    s = stem.lower()
    if s.startswith("мая") or s == "май":
        return 5
    for pref, num in _MONTHS.items():
        if s.startswith(pref):
            return num
    return None


def _safe_date(year: int, month: int, day: int) -> _date | None:
    try:
        return _date(year, month, day)
    except ValueError:
        return None


def extract_date(text: str, base: _date) -> _date | None:
    """Первая осмысленная дата из текста. base — дата публикации поста."""
    low = text.lower()

    # 1) относительные
    for word, delta in _REL.items():
        if re.search(rf"\b{word}\b", low):
            return base + timedelta(days=delta)

    # 2) «1 августа»
    m = _WORD_DATE_RE.search(text)
    if m:
        day = int(m.group(1))
        mon = _month_num(m.group(2))
        if mon:
            d = _safe_date(base.year, mon, day)
            # если месяц уже прошёл — вероятно, речь про следующий год
            if d and d < base - timedelta(days=180):
                d = _safe_date(base.year + 1, mon, day)
            if d:
                return d

    # 3) «01.08» / «1.8.2026»
    for m in _NUM_DATE_RE.finditer(text):
        day, mon = int(m.group(1)), int(m.group(2))
        if not (1 <= mon <= 12 and 1 <= day <= 31):
            continue  # это не дата (например, «18.00» — время)
        year = base.year
        if m.group(3):
            y = int(m.group(3))
            year = y if y > 1000 else 2000 + y
        d = _safe_date(year, mon, day)
        if d and not m.group(3) and d < base - timedelta(days=180):
            d = _safe_date(year + 1, mon, day)
        if d:
            return d

    # 4) день недели — только с предлогом («в пятницу», «во вторник»),
    #    иначе ловим мусор вроде «вайб понедельника»
    for stem, idx in _WEEKDAYS.items():
        if re.search(rf"\bво?\s+{stem}\w*", low):
            ahead = (idx - base.weekday()) % 7
            return base + timedelta(days=ahead or 7)

    return None


def extract_time(text: str) -> str | None:
    """Время в виде HH:MM, либо 'весь день'."""
    m = _TIME_PREP_RE.search(text) or _TIME_COLON_RE.search(text)
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    if _ALLDAY_RE.search(text):
        return "весь день"
    m = _TIME_HOUR_RE.search(text)
    if m:
        return f"{int(m.group(1)):02d}:00"
    return None


def parse(text: str, post_date: datetime | str) -> dict:
    """Возвращает {'date': 'YYYY-MM-DD'|None, 'time': 'HH:MM'|None, 'has': bool}."""
    if isinstance(post_date, str):
        try:
            post_date = datetime.fromisoformat(post_date)
        except ValueError:
            post_date = datetime.now()
    base = post_date.date()

    d = extract_date(text, base)
    t = extract_time(text)
    return {
        "date": d.isoformat() if d else None,
        "time": t,
        "has": bool(d or t),
    }
