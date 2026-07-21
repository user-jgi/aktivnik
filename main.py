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

import config
import db
import fetcher
import moderator
import publisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("aktivnik.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("main")


def send_review(post: dict, reason: str, event: dict | None = None) -> str:
    row_id = db.add_post(post, "pending_review", reason, event)
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

    if db.duplicate_published(db.text_hash(post["text"])):
        db.add_post(post, "duplicate", "тот же текст уже публиковался")
        return "duplicate"

    decision, reason, event = moderator.moderate(post["text"])

    if decision == "review" and reason.startswith("ошибка ИИ"):
        # Groq недоступен — не записываем, попробуем в следующем цикле
        return "error"

    # Дедуп афиш: то же мероприятие (название+дата) уже публиковалось
    if decision == "publish" and event.get("title"):
        dup_url = db.duplicate_event(event["title"], event["date"])
        if dup_url:
            db.add_post(post, "duplicate",
                        f"та же афиша уже публиковалась: {dup_url}", event)
            return "duplicate"

    if decision == "publish":
        ok = publisher.publish(post)
        status = "published" if ok else "skipped"
        db.add_post(post, status, reason, event)
        return status

    # Текстовые посты человеку не шлём: не уверен ИИ — значит отказ
    db.add_post(post, "rejected", reason, event)
    return "rejected"


def baseline_source(src: dict) -> int:
    """Помечает текущие посты источника как известные БЕЗ модерации.

    Так при запуске/добавлении источника старьё не публикуется и лимиты
    Groq не тратятся. Возвращает число добавленных в базу постов."""
    if src.get("type") != "telegram":
        return 0
    n = 0
    try:
        for p in fetcher.fetch_since(src["id"], config.FRESH_WINDOW_DAYS):
            if not db.seen(p["source"], p["post_id"]):
                db.add_post(p, "baseline", "старый пост, помечен при запуске")
                n += 1
    except Exception as e:
        log.error("Источник %s: ошибка baseline: %s", src["id"], e)
    return n


def run_source(src: dict) -> dict:
    """Обрабатывает новые посты источника (окно FRESH_WINDOW_DAYS)."""
    counts: dict[str, int] = {}
    if src.get("type") != "telegram":
        return counts
    try:
        posts = fetcher.fetch_since(src["id"], config.FRESH_WINDOW_DAYS)
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
    "/add username — добавить TG-канал (публичный, по username без @)\n"
    "/del username — убрать канал из списка\n"
    "/list — список источников\n"
    "/stats — статистика по постам\n"
    "/review — прислать посты, ждущие решения\n"
    "/help — эта справка\n\n"
    "Афиши-картинки без текста приходят сюда с кнопками ✅/❌."
)


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
            f"{i+1}. t.me/{s['id']} — {s.get('title', '')}".strip(" —")
            for i, s in enumerate(sources)
        ]
        return "Источники:\n" + "\n".join(lines)

    if cmd == "stats":
        lines = ["Статистика (всего):", fmt_counts(db.stats()), ""]
        for source, s in sorted(db.stats_by_source().items()):
            lines.append(f"t.me/{source}:")
            lines.append("  " + fmt_counts(s))
        return "\n".join(lines)

    if cmd == "add":
        if not arg:
            return "Формат: /add username"
        sources = config.load_sources()
        if any(s["id"].lower() == arg.lower() for s in sources):
            return f"t.me/{arg} уже в списке."
        try:
            page = fetcher.fetch_page(arg)
        except Exception as e:
            return f"Не удалось прочитать t.me/s/{arg}: {e}"
        if not page:
            return (f"У t.me/{arg} нет публичного веб-превью — "
                    f"канал закрытый или не существует. Добавить нельзя.")
        src = {"type": "telegram", "id": arg, "title": ""}
        sources.append(src)
        config.save_sources(sources)
        n = baseline_source(src)
        return (f"✅ Добавил t.me/{arg}. Старые посты ({n}) публиковаться не будут, "
                f"слежу за новыми.")

    if cmd == "del":
        if not arg:
            return "Формат: /del username"
        sources = config.load_sources()
        new = [s for s in sources if s["id"].lower() != arg.lower()]
        if len(new) == len(sources):
            return f"t.me/{arg} нет в списке."
        config.save_sources(new)
        return f"🗑 Убрал t.me/{arg}. Осталось источников: {len(new)}."

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
