"""Чтение открытых VK-групп через API wall.get (сервисный ключ).

Возвращает посты в том же формате, что и telegram-fetcher, чтобы их обрабатывал
общий конвейер (модерация → публикация). Видео VK не переносятся (нет прямого
mp4), их наличие помечается в тексте.
"""
import html as _html
import logging
from datetime import datetime, timedelta, timezone

import requests

from config import VK_SERVICE_TOKEN

log = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method/wall.get"
VK_VER = "5.199"


class VKError(Exception):
    pass


def _photo_url(photo: dict) -> str | None:
    """Самая большая версия фото из attachment."""
    sizes = photo.get("sizes") or []
    if sizes:
        best = max(sizes, key=lambda s: s.get("width", 0) * s.get("height", 0))
        return best.get("url")
    return None


def _collect_media(atts: list, photos: list) -> bool:
    """Добавляет фото из attachments в photos. Возвращает True, если есть видео."""
    has_video = False
    for att in atts or []:
        t = att.get("type")
        if t == "photo":
            u = _photo_url(att.get("photo", {}))
            if u:
                photos.append(u)
        elif t == "video":
            has_video = True
    return has_video


def _extract(item: dict, domain: str) -> dict:
    owner_id = item.get("owner_id") or item.get("from_id")
    post_id = item["id"]
    text = item.get("text", "")
    photos: list[str] = []
    has_video = _collect_media(item.get("attachments"), photos)

    # Репост: если своего текста/медиа нет — берём из оригинала
    for rep in item.get("copy_history", []) or []:
        if not text.strip():
            text = rep.get("text", "")
        has_video = _collect_media(rep.get("attachments"), photos) or has_video

    if has_video and text:
        text += "\n\n📹 [видео — смотри в оригинале VK]"

    return {
        # префикс vk: чтобы не пересекаться с TG-каналом того же screen name
        "source": f"vk:{domain}",
        "post_id": post_id,
        "url": f"https://vk.com/wall{owner_id}_{post_id}",
        "date": datetime.fromtimestamp(item["date"], tz=timezone.utc).isoformat(),
        "text": text,
        "html": _html.escape(text),   # VK-текст простой; экранируем для parse_mode=HTML
        "photos": photos[:10],
        "videos": [],
        "round": False,
    }


def fetch_since(domain: str, days: int) -> list[dict]:
    """Посты открытой VK-группы за последние `days` дней (одна выдача wall.get)."""
    if not VK_SERVICE_TOKEN:
        log.warning("vk/%s пропущен: VK_SERVICE_TOKEN не задан", domain)
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    r = requests.get(VK_API, params={
        "domain": domain,
        "count": 100,
        "access_token": VK_SERVICE_TOKEN,
        "v": VK_VER,
    }, timeout=20)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise VKError(f"{err.get('error_code')}: {err.get('error_msg')}")

    items = data.get("response", {}).get("items", [])
    result = []
    for item in items:
        if item.get("marked_as_ads"):
            continue
        p = _extract(item, domain)
        if datetime.fromisoformat(p["date"]) >= cutoff:
            result.append(p)
    result.sort(key=lambda p: (p["date"], p["post_id"]))
    log.info("vk/%s: собрано %d постов за %d дн.", domain, len(result), days)
    return result
