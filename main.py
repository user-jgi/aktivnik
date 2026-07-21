"""Активник Нижний Новгород — агрегатор студенческих анонсов.

Конвейер: источники (t.me/s/) → ИИ-модерация (Groq) → публикация в @aktivnik_nn.
Сомнительные посты уходят владельцу в ЛС с кнопками Опубликовать/Отклонить.
Там же работает админка: /add, /del, /list, /stats, /help.

Запуск:  python main.py
При старте старые посты только помечаются как известные (без модерации и без
трат лимитов Groq); публикуются только посты, появившиеся после запуска.
"""
import logging
import sys
import threading
import time
from datetime import datetime

import config
import dateparse
import db
import fetcher
import moderator
import publisher
import vk_fetcher
import vk_publisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("aktivnik.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")


def _publish_vk(post: dict):
    """Дублирует пост в VK-сообщество, если автопостинг настроен.

    Ошибка VK не должна ломать основной поток — Telegram уже опубликован.
    """
    if not vk_publisher.enabled():
        return
    try:
        vk_publisher.publish(post)
    except Exception as e:
        log.error("VK-постинг упал (%s): %s", post.get("url", ""), e)


def send_review(post: dict, reason: str, event: dict | None = None,
                dt: dict | None = None) -> str:
    row_id = db.add_post(post, "pending_review", reason, event, dt)
    if row_id:
        msg_id = publisher.send_for_review(row_id, post, reason)
        if msg_id:
            db.set_review_msg(row_id, msg_id)
    return "pending_review"


def process_post(post: dict) -> str:
    """Полный цикл одного поста. Возвращает итоговый статус."""
    if db.seen(post["source"], post["post_id"]):
        return "known"

    if post.get("round"):
        db.add_post(post, "rejected", "кружок (video note)")
        return "rejected"

    if not post["text"].strip():
        # Пост без текста: афишу-картинку решает человек, остальное — мимо
        if post["photos"]:
            return send_review(post, "картинка без текста")
        db.add_post(post, "rejected", "пост без текста и без картинки")
        return "rejected"

    thash = db.text_hash(post["text"])
    if db.duplicate_published(thash):
        db.add_post(post, "duplicate", "тот же текст уже публиковался")
        return "duplicate"

    # ── предфильтр без ИИ: экономим лимиты Groq ──────────────────────────────
    dt = dateparse.parse(post["text"], post["date"])

    # 1. Нет ни даты, ни времени — это не анонс, ИИ не нужен
    if not dt["has"]:
        db.add_post(post, "rejected", "нет даты и времени — не анонс", dt=dt)
        return "rejected"

    # 2. Мероприятие уже прошло
    if dt["date"] and dt["date"] < datetime.now().date().isoformat():
        db.add_post(post, "rejected", f"дата уже прошла ({dt['date']})", dt=dt)
        return "rejected"

    # 3. Та же группа, та же дата и время — очевидный повтор
    dup = db.duplicate_datetime(post["source"], dt["date"], dt["time"])
    if dup:
        db.add_post(post, "duplicate",
                    f"та же дата и время у {post['source']}: {dup}", dt=dt)
        return "duplicate"

    # 4. Ровно такой же текст уже модерировался — переиспользуем вердикт
    prior = db.prior_decision(thash)
    if prior and prior[0] == "rejected":
        db.add_post(post, "rejected", f"повтор ранее отклонённого: {prior[1]}", dt=dt)
        return "rejected"

    decision, reason, event = moderator.moderate(post["text"], dt)

    if decision == "review" and reason.startswith("ошибка ИИ"):
        # Groq недоступен — не записываем, попробуем в следующем цикле
        return "error"

    # Дедуп афиш: то же мероприятие (название+дата) уже публиковалось
    if decision == "publish" and event.get("title"):
        dup_url = db.duplicate_event(event["title"], event["date"])
        if dup_url:
            db.add_post(post, "duplicate",
                        f"та же афиша уже публиковалась: {dup_url}", event, dt)
            return "duplicate"

    if decision == "publish":
        ok = publisher.publish(post)
        if ok:
            _publish_vk(post)
        status = "published" if ok else "skipped"
        db.add_post(post, status, reason, event, dt)
        return status

    # Текстовые посты человеку не шлём: не уверен ИИ — значит отказ
    db.add_post(post, "rejected", reason, event, dt)
    return "rejected"


def _fetch_source(src: dict) -> list[dict]:
    """Читает посты источника нужным парсером (telegram / vk)."""
    if src.get("type") == "vk":
        return vk_fetcher.fetch_since(src["id"], config.FRESH_WINDOW_DAYS)
    return fetcher.fetch_since(src["id"], config.FRESH_WINDOW_DAYS)


def baseline_source(src: dict) -> int:
    """Помечает текущие посты источника как известные БЕЗ модерации.

    Так при запуске/добавлении источника старьё не публикуется и лимиты
    Groq не тратятся. Возвращает число добавленных в базу постов."""
    if src.get("type") not in ("telegram", "vk"):
        return 0
    n = 0
    try:
        for p in _fetch_source(src):
            if not db.seen(p["source"], p["post_id"]):
                db.add_post(p, "baseline", "старый пост, помечен при запуске")
                n += 1
    except Exception as e:
        log.error("Источник %s: ошибка baseline: %s", src["id"], e)
    return n


def run_source(src: dict) -> dict:
    """Обрабатывает новые посты источника (окно FRESH_WINDOW_DAYS)."""
    counts: dict[str, int] = {}
    if src.get("type") not in ("telegram", "vk"):
        return counts
    try:
        posts = _fetch_source(src)
    except Exception as e:
        log.error("Источник %s: ошибка чтения: %s", src["id"], e)
        return counts
    for p in posts:
        try:
            st = process_post(p)
        except Exception as e:
            log.error("Пост %s: необработанная ошибка: %s", p["url"], e)
            st = "error"
        counts[st] = counts.get(st, 0) + 1
    return counts


def fmt_counts(c: dict) -> str:
    order = ["published", "rejected", "pending_review", "duplicate", "known",
             "skipped", "baseline", "error"]
    names = {
        "published": "опубликовано", "rejected": "отклонено",
        "pending_review": "на ревью", "duplicate": "дубли",
        "known": "уже известны", "skipped": "пропущено",
        "baseline": "старые (без обработки)", "error": "ошибки",
    }
    return ", ".join(f"{names[k]}: {c[k]}" for k in order if c.get(k)) or "ничего нового"


# ── Админка и кнопки ревью ────────────────────────────────────────────────────

HELP = (
    "Команды:\n"
    "/menu — главное меню с кнопками\n"
    "/add username — добавить TG-канал (или ссылку vk.com/... / t.me/...)\n"
    "/del username — убрать источник из списка\n"
    "/list — список источников\n"
    "/stats — статистика по постам\n"
    "/review — прислать посты, ждущие решения\n"
    "/help — эта справка\n\n"
    "Афиши-картинки без текста приходят сюда с кнопками ✅/❌."
)


def src_link(src: dict) -> str:
    """Ссылка на источник по его типу."""
    if src.get("type") == "vk":
        return f"vk.com/{src['id']}"
    return f"t.me/{src['id']}"


def _parse_add_arg(arg: str) -> tuple[str, str]:
    """Из аргумента /add достаёт (type, id). Понимает ссылки vk.com/... и t.me/..."""
    a = arg.strip().rstrip("/")
    low = a.lower()
    if "vk.com/" in low:
        return "vk", a.split("/")[-1]
    if "t.me/" in low or "telegram.me/" in low:
        return "telegram", a.split("/")[-1].lstrip("@")
    if low.startswith("vk:"):
        return "vk", a[3:]
    return "telegram", a.lstrip("@")


def resend_pending() -> str:
    """Повторно отправляет владельцу все посты со статусом «на ревью»."""
    pending = db.list_pending()
    if not pending:
        return "На ревью пусто — всё разобрано. 🎉"
    for post in pending:
        msg_id = publisher.send_for_review(post["id"], post, post["ai_reason"])
        if msg_id:
            db.set_review_msg(post["id"], msg_id)
        time.sleep(1.1)
    return f"Отправил {len(pending)} пост(ов) на решение."


def handle_command(text: str) -> str | None:
    """Возвращает текст ответа или None, если ответ уже отправлен иначе."""
    parts = text.strip().split()
    cmd = parts[0].lower().lstrip("/").split("@")[0] if parts else ""
    arg = parts[1].lstrip("@").strip() if len(parts) > 1 else ""

    if cmd in ("start", "menu"):
        publisher.send_menu()
        return None

    if cmd == "help":
        return HELP

    if cmd == "review":
        return resend_pending()

    if cmd == "list":
        sources = config.load_sources()
        if not sources:
            return "Список источников пуст."
        lines = [
            f"{i+1}. {src_link(s)} — {s.get('title', '')}".strip(" —")
            for i, s in enumerate(sources)
        ]
        return "Источники:\n" + "\n".join(lines)

    if cmd == "stats":
        lines = ["Статистика (всего):", fmt_counts(db.stats()), ""]
        for source, s in sorted(db.stats_by_source().items()):
            if source.startswith("vk:"):
                label = "vk.com/" + source[3:]
            else:
                label = "t.me/" + source
            lines.append(f"{label}:")
            lines.append("  " + fmt_counts(s))
        return "\n".join(lines)

    if cmd == "add":
        if not arg:
            return "Формат: /add username  (или ссылка vk.com/... / t.me/...)"
        stype, sid = _parse_add_arg(arg)
        if stype == "vk" and not config.VK_SERVICE_TOKEN:
            return ("Чтобы добавлять VK-группы, задай VK_SERVICE_TOKEN "
                    "(сервисный ключ приложения с dev.vk.com).")
        sources = config.load_sources()
        if any(s["id"].lower() == sid.lower() and s.get("type", "telegram") == stype
               for s in sources):
            return f"{src_link({'type': stype, 'id': sid})} уже в списке."
        src = {"type": stype, "id": sid, "title": ""}
        # Проверяем доступность источника перед добавлением
        try:
            if stype == "vk":
                probe = vk_fetcher.fetch_since(sid, 30)
            else:
                probe = fetcher.fetch_page(sid)
        except Exception as e:
            return f"Не удалось прочитать {src_link(src)}: {e}"
        if stype == "telegram" and not probe:
            return (f"У t.me/{sid} нет публичного веб-превью — "
                    f"канал закрытый или не существует. Добавить нельзя.")
        sources.append(src)
        config.save_sources(sources)
        n = baseline_source(src)
        return (f"✅ Добавил {src_link(src)}. Старые посты ({n}) публиковаться "
                f"не будут, слежу за новыми.")

    if cmd == "del":
        if not arg:
            return "Формат: /del username  (для VK: vk.com/... или vk:name)"
        dtype, sid = _parse_add_arg(arg)
        # Если явно указан vk — удаляем только vk-источник; иначе по id
        has_vk = "vk.com/" in arg.lower() or arg.lower().startswith("vk:")
        sources = config.load_sources()
        if has_vk:
            new = [s for s in sources
                   if not (s["id"].lower() == sid.lower() and s.get("type") == "vk")]
        else:
            new = [s for s in sources if s["id"].lower() != sid.lower()]
        if len(new) == len(sources):
            return f"{sid} нет в списке."
        config.save_sources(new)
        return f"🗑 Убрал {sid}. Осталось источников: {len(new)}."

    return "Не понял. /help — список команд."


def bot_loop():
    """Фоновый поток: команды админки и кнопки Опубликовать/Отклонить."""
    offset = 0
    while True:
        for upd in publisher.get_updates(offset):
            offset = upd["update_id"] + 1
            try:
                msg = upd.get("message")
                if msg and msg["from"]["id"] == config.OWNER_ID and msg.get("text"):
                    reply = handle_command(msg["text"])
                    if reply:
                        publisher.notify_owner(reply)
                    continue

                cb = upd.get("callback_query")
                if not cb or cb["from"]["id"] != config.OWNER_ID:
                    continue
                action, _, sid = cb.get("data", "").partition(":")

                # Кнопки главного меню
                if action == "menu":
                    publisher.answer_callback(cb["id"], "")
                    reply = handle_command("/" + sid)
                    if reply:
                        publisher.notify_owner(reply)
                    continue
                post = db.get_post(int(sid)) if sid.isdigit() else None
                if not post or post["status"] != "pending_review":
                    publisher.answer_callback(cb["id"], "Уже обработано")
                    continue
                if action == "pub":
                    ok = publisher.publish(post)
                    if ok:
                        _publish_vk(post)
                    db.set_status(post["id"], "published" if ok else "skipped")
                    publisher.answer_callback(cb["id"], "Опубликовано" if ok else "Ошибка")
                    result = "✅ Опубликовано" if ok else "⚠️ Не удалось опубликовать"
                else:
                    db.set_status(post["id"], "rejected")
                    publisher.answer_callback(cb["id"], "Отклонено")
                    result = "❌ Отклонено"
                if post.get("review_msg_id"):
                    publisher.edit_review_message(post["review_msg_id"], result)
            except Exception as e:
                log.error("bot_loop: ошибка обработки апдейта: %s", e)


def main():
    db.init_db()
    sources = config.load_sources()
    log.info("Старт. Источников: %d", len(sources))

    log.info(vk_publisher.selfcheck())

    publisher.set_my_commands()
    threading.Thread(target=bot_loop, daemon=True).start()

    # Старые посты не публикуем и не модерируем — только запоминаем
    marked = sum(baseline_source(src) for src in sources)
    log.info("Отмечено старых постов: %d", marked)
    publisher.notify_owner(
        f"🚀 Активник запущен. Источников: {len(sources)}.\n"
        f"Старые посты не трогаю ({marked} отмечено), публикую только новое.\n"
        f"/help — команды администратора."
    )

    while True:
        time.sleep(config.CHECK_INTERVAL_SECONDS)
        stale = db.reject_stale_reviews(3)
        if stale:
            log.info("Авто-отклонено просроченных ревью: %d", stale)
        for src in config.load_sources():
            c = run_source(src)
            if any(k not in ("known",) for k in c):
                log.info("Проверка %s: %s", src["id"], fmt_counts(c))


if __name__ == "__main__":
    main()
