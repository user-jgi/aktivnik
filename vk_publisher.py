"""Автопостинг в VK-сообщество через wall.post (токен сообщества).

Дублирует то, что уходит в Telegram-канал: текст + фото. Фото сначала
загружаются на сервер VK (photos.getWallUploadServer → upload → saveWallPhoto),
затем прикрепляются к записи.
"""
import logging
import time

import requests

from config import VK_GROUP_ID, VK_POST_TOKEN

log = logging.getLogger(__name__)

VK_API = "https://api.vk.com/method/"
VK_VER = "5.199"

_MAX_PHOTOS = 10


class VKPostError(Exception):
    pass


def enabled() -> bool:
    return bool(VK_POST_TOKEN and VK_GROUP_ID)


def _api(method: str, **params) -> dict:
    params.update(access_token=VK_POST_TOKEN, v=VK_VER)
    r = requests.post(VK_API + method, data=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        err = data["error"]
        raise VKPostError(f"{method}: {err.get('error_code')} {err.get('error_msg')}")
    return data["response"]


def selfcheck() -> str:
    """Короткий статус VK-постинга при старте. НИЧЕГО не публикует.

    Проверено вживую: wall.post с ключом сообщества работает. Тестовый постинг
    при старте убран намеренно — wall.delete недоступен для group-токена
    (ошибка 27), поэтому тестовые записи пришлось бы удалять руками.
    """
    if not enabled():
        return "VK-постинг выключен (нет VK_POST_TOKEN)"
    try:
        group = _api("groups.getById", group_id=VK_GROUP_ID)
        items = group.get("groups", group) if isinstance(group, dict) else group
        title = items[0].get("name", "?") if items else "?"
        return f"VK-постинг включён → «{title}» (vk.com/aktivnik_nn)"
    except (VKPostError, requests.RequestException, KeyError, IndexError) as e:
        return f"VK-постинг включён, но сообщество не опрошено: {e}"


def _download(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content
    except requests.RequestException as e:
        log.warning("VK: не скачалось фото %s: %s", url, e)
        return None


def _upload_photo(image: bytes) -> str | None:
    """Загружает одно фото на стену сообщества. Возвращает attachment вида photo-1_2."""
    try:
        server = _api("photos.getWallUploadServer", group_id=VK_GROUP_ID)
        up = requests.post(
            server["upload_url"],
            files={"photo": ("photo.jpg", image, "image/jpeg")},
            timeout=90,
        ).json()
        if not up.get("photo") or up.get("photo") == "[]":
            return None
        saved = _api(
            "photos.saveWallPhoto",
            group_id=VK_GROUP_ID,
            server=up["server"], photo=up["photo"], hash=up["hash"],
        )
        p = saved[0]
        return f"photo{p['owner_id']}_{p['id']}"
    except (VKPostError, requests.RequestException, KeyError, IndexError) as e:
        log.warning("VK: фото не загрузилось: %s", e)
        return None


def publish(post: dict) -> bool:
    """Публикует пост в VK-сообщество. Возвращает True при успехе."""
    if not enabled():
        return False

    text = post.get("text", "").strip()
    # Ссылка на оригинал: часть ссылок (регистрация в кнопках) не переносится
    src = post.get("source", "")
    label = "vk.com/" + src[3:] if src.startswith("vk:") else "t.me/" + src
    if post.get("url"):
        text = f"{text}\n\n🔗 Источник: {post['url']} ({label})".strip()

    attachments = []
    for url in post.get("photos", [])[:_MAX_PHOTOS]:
        data = _download(url)
        if not data:
            continue
        att = _upload_photo(data)
        if att:
            attachments.append(att)
        time.sleep(0.4)  # VK: не чаще ~3 запросов/сек

    if not text and not attachments:
        return False

    try:
        _api(
            "wall.post",
            owner_id=-int(VK_GROUP_ID),   # для сообщества id отрицательный
            from_group=1,                 # от имени сообщества
            message=text[:15000],
            attachments=",".join(attachments),
        )
        log.info("VK: опубликовано (%s)", post.get("url", ""))
        return True
    except (VKPostError, requests.RequestException) as e:
        log.error("VK: публикация не удалась (%s): %s", post.get("url", ""), e)
        return False
