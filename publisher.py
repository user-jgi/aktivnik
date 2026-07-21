"""Публикация в канал и отправка сомнительных постов владельцу на ревью."""
import json
import logging
import time

import requests

from config import BOT_TOKEN, CHANNEL, OWNER_ID, PUBLISH_DELAY_SECONDS

log = logging.getLogger(__name__)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

_MAX_FILE = 45 * 1024 * 1024  # лимит загрузки ботом — 50 МБ, с запасом


def _call(method: str, files: dict | None = None, **params) -> dict:
    last_exc: Exception | None = None
    for attempt in range(3):
        if attempt:
            time.sleep(5 * attempt)
        try:
            if files:
                # multipart: сложные параметры сериализуем в JSON
                form = {
                    k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                    for k, v in params.items()
                }
                r = requests.post(f"{API}/{method}", data=form, files=files, timeout=120)
            else:
                r = requests.post(f"{API}/{method}", json=params, timeout=35)
        except requests.RequestException as e:
            last_exc = e
            log.warning("%s: сетевая ошибка (попытка %d/3): %s", method, attempt + 1, e)
            continue
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"{method}: {data.get('description')}")
        return data["result"]
    raise RuntimeError(f"{method}: network error: {last_exc}")


def _download(url: str) -> bytes | None:
    """Скачивает медиафайл с CDN Telegram (напрямую бот-API их не принимает)."""
    try:
        r = requests.get(url, timeout=60, stream=True)
        r.raise_for_status()
        buf = b""
        for chunk in r.iter_content(1 << 16):
            buf += chunk
            if len(buf) > _MAX_FILE:
                log.warning("Файл больше %d МБ — пропуск: %s", _MAX_FILE >> 20, url)
                return None
        return buf
    except requests.RequestException as e:
        log.warning("Не удалось скачать медиа %s: %s", url, e)
        return None


def _send_text(chat_id, html: str, **kw):
    return _call(
        "sendMessage", chat_id=chat_id, text=html[:4096],
        parse_mode="HTML", **kw,
    )


def publish(post: dict) -> bool:
    """Тупой репост: текст (с сохранением ссылок) + медиа ОДНИМ сообщением.

    - фото + текст ≤1024 → альбом с подписью;
    - фото + текст >1024 → текст с превью-картинкой сверху (link preview);
    - только видео + текст ≤1024 → видео с подписью.
    Медиа скачиваются с CDN и загружаются файлами: прямые ссылки на
    cdn-telegram.org бот-API отклоняет (WEBPAGE_CURL_FAILED).
    """
    html = post["html"] or post["text"]
    photos = post["photos"][:10]
    videos = post["videos"][:3]
    caption_ok = html and len(html) <= 1024

    try:
        if photos and not caption_ok:
            # Длинный текст: одно текстовое сообщение, первое фото — превью сверху
            _call(
                "sendMessage", chat_id=CHANNEL, text=html[:4096],
                parse_mode="HTML",
                link_preview_options={
                    "url": photos[0],
                    "prefer_large_media": True,
                    "show_above_text": True,
                },
            )
        elif photos:
            blobs = []
            for i, u in enumerate(photos):
                data = _download(u)
                if data:
                    blobs.append((f"p{i}.jpg", data))
            if blobs:
                media = [
                    {"type": "photo", "media": f"attach://{name}"}
                    for name, _ in blobs
                ]
                media[0]["caption"] = html
                media[0]["parse_mode"] = "HTML"
                files = {name: (name, data, "image/jpeg") for name, data in blobs}
                _call("sendMediaGroup", files=files, chat_id=CHANNEL, media=media)
            elif html:
                _send_text(CHANNEL, html)  # все фото протухли (404)
            else:
                return False
        elif videos:
            data = _download(videos[0])
            if data:
                params = {"chat_id": CHANNEL, "video": "attach://video"}
                if caption_ok:
                    params["caption"] = html
                    params["parse_mode"] = "HTML"
                _call("sendVideo",
                      files={"video": ("v0.mp4", data, "video/mp4")}, **params)
                if html and not caption_ok:
                    _send_text(CHANNEL, html)
            elif html:
                _send_text(CHANNEL, html)
            else:
                return False
        elif html:
            _send_text(CHANNEL, html)
        else:
            return False  # нечего публиковать

        time.sleep(PUBLISH_DELAY_SECONDS)
        return True
    except RuntimeError as e:
        log.error("Публикация не удалась (%s): %s", post["url"], e)
        return False


def send_for_review(row_id: int, post: dict, reason: str) -> int | None:
    """Отправляет сомнительный пост владельцу с кнопками Опубликовать/Отклонить."""
    preview = post["text"][:800] or "(без текста — открой оригинал)"
    text = (
        f"🤔 <b>Требует решения</b>\n"
        f"Источник: {post['source']} | <a href=\"{post['url']}\">оригинал</a>\n"
        f"Причина: {reason}\n\n{preview}"
    )
    kb = {
        "inline_keyboard": [[
            {"text": "✅ Опубликовать", "callback_data": f"pub:{row_id}"},
            {"text": "❌ Отклонить",   "callback_data": f"rej:{row_id}"},
        ]]
    }
    try:
        msg = _call(
            "sendMessage", chat_id=OWNER_ID, text=text[:4096],
            parse_mode="HTML", reply_markup=kb,
            disable_web_page_preview=False,
        )
        return msg["message_id"]
    except RuntimeError as e:
        log.error("Не удалось отправить на ревью: %s", e)
        return None


def notify_owner(text: str):
    try:
        _send_text(OWNER_ID, text, disable_web_page_preview=True)
    except RuntimeError as e:
        log.warning("notify_owner: %s", e)


def answer_callback(cb_id: str, text: str):
    try:
        _call("answerCallbackQuery", callback_query_id=cb_id, text=text)
    except RuntimeError:
        pass


def edit_review_message(msg_id: int, suffix: str):
    """Убирает кнопки и дописывает результат к сообщению ревью."""
    try:
        _call(
            "editMessageReplyMarkup", chat_id=OWNER_ID, message_id=msg_id,
            reply_markup={"inline_keyboard": []},
        )
    except RuntimeError:
        pass
    try:
        _call(
            "sendMessage", chat_id=OWNER_ID, text=suffix,
            reply_to_message_id=msg_id,
        )
    except RuntimeError:
        pass


MENU_KB = {
    "inline_keyboard": [
        [{"text": "📊 Статистика", "callback_data": "menu:stats"},
         {"text": "📋 Источники", "callback_data": "menu:list"}],
        [{"text": "🕵️ На ревью", "callback_data": "menu:review"},
         {"text": "❓ Помощь", "callback_data": "menu:help"}],
    ]
}


def send_menu(text: str = "Меню:"):
    try:
        _call("sendMessage", chat_id=OWNER_ID, text=text,
              reply_markup=MENU_KB, disable_web_page_preview=True)
    except RuntimeError as e:
        log.warning("send_menu: %s", e)


def set_my_commands():
    """Регистрирует команды в интерфейсе Telegram (кнопка «Меню» у бота)."""
    commands = [
        {"command": "menu",   "description": "Главное меню"},
        {"command": "stats",  "description": "Статистика по постам"},
        {"command": "list",   "description": "Список источников"},
        {"command": "review", "description": "Прислать посты, ждущие решения"},
        {"command": "add",    "description": "Добавить канал: /add username"},
        {"command": "del",    "description": "Убрать канал: /del username"},
        {"command": "help",   "description": "Справка"},
    ]
    try:
        _call("setMyCommands", commands=commands)
    except RuntimeError as e:
        log.warning("setMyCommands: %s", e)


def get_updates(offset: int) -> list[dict]:
    try:
        return _call("getUpdates", offset=offset, timeout=25,
                     allowed_updates=["message", "callback_query"])
    except (RuntimeError, requests.RequestException) as e:
        log.warning("getUpdates: %s", e)
        time.sleep(5)
        return []
