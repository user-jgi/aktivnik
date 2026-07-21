"""Чтение публичных Telegram-каналов через веб-превью t.me/s/<username>.

Не требует ни бота в канале, ни юзер-аккаунта. Работает только для
публичных каналов с включённым превью.
"""
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

# Разрешённые в Telegram HTML-подписи теги
_KEEP_TAGS = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "a"}


def _html_to_tg(el: Tag) -> str:
    """Конвертирует HTML текста поста из превью в Telegram-совместимый HTML,
    сохраняя ссылки и базовое форматирование."""
    out: list[str] = []

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def walk(node):
        for child in node.children:
            if isinstance(child, NavigableString):
                out.append(esc(str(child)))
            elif isinstance(child, Tag):
                name = child.name.lower()
                if name == "br":
                    out.append("\n")
                elif name == "a" and child.get("href"):
                    out.append(f'<a href="{esc(child["href"])}">')
                    walk(child)
                    out.append("</a>")
                elif name in _KEEP_TAGS:
                    tag = {"strong": "b", "em": "i", "del": "s"}.get(name, name)
                    out.append(f"<{tag}>")
                    walk(child)
                    out.append(f"</{tag}>")
                else:  # tg-emoji, span и прочее — только содержимое
                    walk(child)

    walk(el)
    return "".join(out).strip()


def _parse_message(wrap: Tag, source: str) -> dict | None:
    msg = wrap.find(class_="tgme_widget_message")
    if not msg or not msg.get("data-post"):
        return None
    try:
        post_id = int(msg["data-post"].split("/")[-1])
    except ValueError:
        return None

    time_el = msg.select_one(".tgme_widget_message_date time[datetime]") or msg.find(
        "time", attrs={"datetime": True}
    )
    if not time_el:
        return None
    date = datetime.fromisoformat(time_el["datetime"])

    text_el = msg.find(class_="tgme_widget_message_text")
    html = _html_to_tg(text_el) if text_el else ""
    text = text_el.get_text(" ", strip=True) if text_el else ""

    photos = []
    for a in msg.find_all(class_="tgme_widget_message_photo_wrap"):
        m = re.search(r"background-image:url\('([^']+)'\)", a.get("style", ""))
        if m:
            photos.append(m.group(1))

    videos = []
    round_video = False
    for v in msg.find_all("video"):
        classes = " ".join(v.get("class", []))
        if "roundvideo" in classes:
            round_video = True
            continue  # кружочки не переносим
        if v.get("src"):
            videos.append(v["src"])

    return {
        "source": source,
        "post_id": post_id,
        "url": f"https://t.me/{source}/{post_id}",
        "date": date.isoformat(),
        "text": text,
        "html": html,
        "photos": photos,
        "videos": videos,
        "round": round_video,
    }


def fetch_page(source: str, before: int | None = None) -> list[dict]:
    """Одна страница превью (~20 постов), от старых к новым."""
    url = f"https://t.me/s/{source}"
    params = {"before": before} if before else {}
    r = requests.get(url, params=params, headers=_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    posts = []
    for wrap in soup.find_all(class_="tgme_widget_message_wrap"):
        p = _parse_message(wrap, source)
        if p:
            posts.append(p)
    return posts


def fetch_since(source: str, days: int) -> list[dict]:
    """Все посты источника за последние `days` дней (пагинация назад)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    collected: dict[int, dict] = {}
    before: int | None = None

    for _ in range(50):  # предохранитель от бесконечного цикла
        page = fetch_page(source, before)
        if not page:
            break
        for p in page:
            collected[p["post_id"]] = p
        oldest = min(page, key=lambda p: p["post_id"])
        if datetime.fromisoformat(oldest["date"]) < cutoff:
            break
        if before is not None and oldest["post_id"] >= before:
            break  # страница не сдвинулась
        before = oldest["post_id"]
        time.sleep(1)

    result = [
        p for p in collected.values()
        if datetime.fromisoformat(p["date"]) >= cutoff
    ]
    result.sort(key=lambda p: (p["date"], p["post_id"]))
    log.info("%s: собрано %d постов за %d дн.", source, len(result), days)
    return result
