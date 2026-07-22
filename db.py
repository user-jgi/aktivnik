"""SQLite: учёт постов, дедупликация, статусы модерации."""
import difflib
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,
    post_id    INTEGER NOT NULL,
    url        TEXT NOT NULL,
    date       TEXT NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    html       TEXT NOT NULL DEFAULT '',
    photos     TEXT NOT NULL DEFAULT '[]',
    videos     TEXT NOT NULL DEFAULT '[]',
    text_hash  TEXT NOT NULL DEFAULT '',
    -- pending_ai → ждёт модерации; pending_review → ждёт решения владельца;
    -- published / rejected / duplicate / skipped
    status     TEXT NOT NULL DEFAULT 'pending_ai',
    ai_reason  TEXT NOT NULL DEFAULT '',
    review_msg_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(source, post_id)
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _conn() as c:
        c.executescript(_SCHEMA)
        # Миграция: поля события для дедупликации афиш
        cols = {r["name"] for r in c.execute("PRAGMA table_info(posts)")}
        # event_* заполняет ИИ; dt_* — регулярки до обращения к ИИ
        for col in ("event_title", "event_date", "event_time", "dt_date", "dt_time"):
            if col not in cols:
                c.execute(f"ALTER TABLE posts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_dt "
                  "ON posts(source, dt_date, dt_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_posts_hash ON posts(text_hash)")


def text_hash(text: str) -> str:
    norm = " ".join(text.lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def seen(source: str, post_id: int) -> bool:
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM posts WHERE source=? AND post_id=?", (source, post_id)
        ).fetchone()
    return row is not None


def duplicate_published(thash: str) -> bool:
    """Тот же текст уже публиковался (кросспостинг между источниками)."""
    if not thash:
        return False
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM posts WHERE text_hash=? AND status='published'", (thash,)
        ).fetchone()
    return row is not None


def _norm_title(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", title.lower(), flags=re.UNICODE)
    return " ".join(t.split())


def duplicate_event(title: str, date: str) -> str | None:
    """Та же афиша уже публиковалась: совпадает дата, а название похоже
    (нечёткое сравнение — формулировки в разных постах различаются).
    Возвращает url ранее опубликованного поста или None."""
    if not title or not date:
        return None
    norm = _norm_title(title)
    if not norm:
        return None
    with _conn() as c:
        rows = c.execute(
            """SELECT url, event_title FROM posts
               WHERE event_date=? AND status IN ('published','pending_review')
                 AND event_title != ''""",
            (date,),
        ).fetchall()
    for r in rows:
        other = _norm_title(r["event_title"])
        if not other:
            continue
        ratio = difflib.SequenceMatcher(None, norm, other).ratio()
        short, long_ = sorted((norm, other), key=len)
        contained = len(short) >= 8 and short in long_
        if ratio >= 0.75 or contained:
            return r["url"]
    return None


def add_post(p: dict, status: str, ai_reason: str = "",
             event: dict | None = None, dt: dict | None = None) -> int:
    ev = event or {}
    d = dt or {}
    with _conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO posts
               (source, post_id, url, date, text, html, photos, videos,
                text_hash, status, ai_reason, created_at,
                event_title, event_date, event_time, dt_date, dt_time)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p["source"], p["post_id"], p["url"], p["date"],
                p["text"], p["html"],
                json.dumps(p["photos"]), json.dumps(p["videos"]),
                text_hash(p["text"]), status, ai_reason,
                datetime.now(timezone.utc).isoformat(),
                ev.get("title", ""), ev.get("date", ""), ev.get("time", ""),
                d.get("date") or "", d.get("time") or "",
            ),
        )
        return cur.lastrowid or 0


def duplicate_datetime(source: str, dt_date: str, dt_time: str) -> str | None:
    """Та же группа уже присылала пост на ту же дату и время.

    Ловит повторы-напоминания об одном мероприятии без обращения к ИИ.
    Срабатывает только когда известны И дата, И время — иначе слишком грубо.
    """
    if not dt_date or not dt_time:
        return None
    with _conn() as c:
        row = c.execute(
            """SELECT url FROM posts
               WHERE source=? AND dt_date=? AND dt_time=?
                 AND status IN ('published','pending_review')
               LIMIT 1""",
            (source, dt_date, dt_time),
        ).fetchone()
    return row["url"] if row else None


def drop_baseline(source: str, post_id: int) -> bool:
    """Снимает пометку «старый пост» — чтобы обработать его при backfill."""
    with _conn() as c:
        cur = c.execute(
            "DELETE FROM posts WHERE source=? AND post_id=? AND status='baseline'",
            (source, post_id),
        )
        return cur.rowcount > 0


def _row_to_post(row) -> dict:
    d = dict(row)
    d["photos"] = json.loads(d["photos"])
    d["videos"] = json.loads(d["videos"])
    return d


def list_deferred(max_age_days: int) -> list[dict]:
    """Посты, отложенные из-за недоступности Groq, ещё не протухшие."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE status='deferred' AND created_at >= ? "
            "ORDER BY date",
            (cutoff,),
        ).fetchall()
    return [_row_to_post(r) for r in rows]


def expire_deferred(max_age_days: int) -> int:
    """Слишком старые отложенные посты снимаем с повторов."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    with _conn() as c:
        cur = c.execute(
            "UPDATE posts SET status='expired', "
            "ai_reason='протух: Groq был недоступен дольше ' || ? || ' дн.' "
            "WHERE status='deferred' AND created_at < ?",
            (max_age_days, cutoff),
        )
        return cur.rowcount


def finalize_deferred(row_id: int, status: str, reason: str,
                      event: dict | None = None):
    """Записывает итоговое решение по ранее отложенному посту."""
    ev = event or {}
    with _conn() as c:
        c.execute(
            "UPDATE posts SET status=?, ai_reason=?, event_title=? WHERE id=?",
            (status, reason, ev.get("title", ""), row_id),
        )


def prior_decision(thash: str) -> tuple[str, str] | None:
    """Решение по уже виденному ровно такому же тексту (кросспостинг).

    Позволяет не платить за повторную модерацию идентичного текста.
    """
    if not thash:
        return None
    with _conn() as c:
        row = c.execute(
            """SELECT status, ai_reason FROM posts
               WHERE text_hash=? AND status IN ('published','rejected','pending_review')
               LIMIT 1""",
            (thash,),
        ).fetchone()
    return (row["status"], row["ai_reason"]) if row else None


def set_status(row_id: int, status: str):
    with _conn() as c:
        c.execute("UPDATE posts SET status=? WHERE id=?", (status, row_id))


def set_review_msg(row_id: int, msg_id: int):
    with _conn() as c:
        c.execute("UPDATE posts SET review_msg_id=? WHERE id=?", (msg_id, row_id))


def get_post(row_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM posts WHERE id=?", (row_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["photos"] = json.loads(d["photos"])
    d["videos"] = json.loads(d["videos"])
    return d


def list_pending() -> list[dict]:
    """Посты, ожидающие решения владельца."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM posts WHERE status='pending_review' ORDER BY date"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["photos"] = json.loads(d["photos"])
        d["videos"] = json.loads(d["videos"])
        result.append(d)
    return result


def list_stuck() -> list[dict]:
    """Строки, зависшие из-за сбоев: ревью не доставлено или публикация упала."""
    with _conn() as c:
        rows = c.execute(
            """SELECT * FROM posts
               WHERE (status='pending_review' AND review_msg_id IS NULL)
                  OR status='skipped'
               ORDER BY date"""
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["photos"] = json.loads(d["photos"])
        d["videos"] = json.loads(d["videos"])
        result.append(d)
    return result


def set_decision(row_id: int, status: str, ai_reason: str):
    with _conn() as c:
        c.execute(
            "UPDATE posts SET status=?, ai_reason=? WHERE id=?",
            (status, ai_reason, row_id),
        )


def reject_stale_reviews(days: int = 3) -> int:
    """Авто-отклоняет посты, висящие на ревью дольше `days` дней."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _conn() as c:
        cur = c.execute(
            """UPDATE posts
               SET status='rejected',
                   ai_reason='авто-отклонено: на ревью дольше ' || ? || ' дн.'
               WHERE status='pending_review' AND created_at < ?""",
            (days, cutoff),
        )
        return cur.rowcount


def stats() -> dict:
    with _conn() as c:
        rows = c.execute(
            "SELECT status, COUNT(*) AS n FROM posts "
            "WHERE status != 'baseline' GROUP BY status"
        ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def stats_by_source() -> dict[str, dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT source, status, COUNT(*) AS n FROM posts "
            "WHERE status != 'baseline' GROUP BY source, status"
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["source"], {})[r["status"]] = r["n"]
    return out
