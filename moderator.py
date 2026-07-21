"""ИИ-модератор на Groq: анонс ли это, подходит ли по тематике.

Возвращает решение: publish / reject / review (сомнительное — человеку).
"""
import json
import logging
import time

import requests

from config import GROQ_API_KEYS, TELEGRAM_PROXY

log = logging.getLogger(__name__)

GROQ_URL   = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

_PROXIES = {"http": TELEGRAM_PROXY, "https": TELEGRAM_PROXY} if TELEGRAM_PROXY else {}


class GroqError(Exception):
    pass


# Минимальный интервал между запросами (free tier ~30 req/min)
_MIN_INTERVAL = 2.5
_last_call = 0.0


def _throttle():
    global _last_call
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def _try_all_keys(payload: dict, timeout: int) -> tuple[str | None, str, float]:
    """Один проход по всем ключам. Возвращает (result|None, last_err, retry_after)."""
    routes = [("proxy", _PROXIES), ("direct", {})] if _PROXIES else [("direct", {})]
    last_err, retry_after = "unknown", 0.0

    for idx, key in enumerate(GROQ_API_KEYS, start=1):
        for label, proxies in routes:
            try:
                r = requests.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    json=payload,
                    proxies=proxies,
                    timeout=timeout,
                )
            except Exception as e:
                last_err = f"connection ({label}): {e}"
                continue
            if r.status_code == 200:
                return r.json()["choices"][0]["message"]["content"].strip(), "", 0.0
            if r.status_code == 429:
                last_err = f"429 on key #{idx}"
                try:
                    retry_after = max(retry_after, float(r.headers.get("retry-after", 0)))
                except ValueError:
                    pass
                break  # следующий ключ
            last_err = f"HTTP {r.status_code}: {r.text[:200]}"
    return None, last_err, retry_after


def _groq_chat(payload: dict, timeout: int = 30) -> str:
    """Вызов Groq: троттлинг, ротация ключей, ожидание и повтор при 429."""
    if not GROQ_API_KEYS:
        raise GroqError("GROQ_API_KEY не задан в .env")

    last_err = "unknown"
    for attempt in range(6):
        _throttle()
        result, last_err, retry_after = _try_all_keys(payload, timeout)
        if result is not None:
            return result
        if "429" not in last_err:
            raise GroqError(last_err)
        pause = max(retry_after, 15.0)
        log.warning("Groq: все ключи в 429, жду %.0f с (попытка %d/6)", pause, attempt + 1)
        time.sleep(pause)
    raise GroqError(last_err)


_PROMPT = """Ты строгий редактор телеграм-канала «Активник Нижний Новгород» — \
агрегатора ЛУЧШИХ студенческих мероприятий вузов Нижнего Новгорода. Канал читают \
студенты всех вузов города, чтобы найти, куда сходить. Место в ленте дорого: \
публикуется только то, на что реально захочется пойти.

Пост из телеграм-канала вуза:
=== НАЧАЛО ПОСТА ===
{POST}
=== КОНЕЦ ПОСТА ===

ПУБЛИКОВАТЬ только если ВСЁ выполняется:
1. Это анонс КОНКРЕТНОГО предстоящего мероприятия: понятно ЧТО будет и КОГДА \
(дата или день недели). Концерт, фестиваль, вечеринка, квиз, хакатон, спортивный \
турнир, открытая лекция интересного спикера, мастер-класс, ярмарка, кино-показ, \
набор в крутой движ и т.п.
2. Мероприятие открыто для обычных студентов (можно прийти или зарегистрироваться), \
а не только для узкой группы (актива, одной кафедры, участников программы).
3. На это мероприятие среднему студенту реально может захотеться пойти в свободное \
время. Оценка интересности interest от 1 до 10 — публикуем только 6 и выше.

ОТКЛОНЯТЬ безжалостно:
- итоги/отчёты/фотоотчёты о прошедшем, поздравления, мемы;
- учебное и административное: расписания, сессии, пересдачи, стипендии, общежития, \
приёмная кампания, дни открытых дверей для абитуриентов;
- скучная бюрократия под видом события: заседания, собрания актива, конференции \
c докладами по отчётности, «стратегические сессии»;
- опросы, конкурсы репостов, розыгрыши, голосования за кого-то;
- вакансии, стажировки, гранты, конкурсы научных работ без очного события;
- вебинары и онлайн-курсы без «вау»-повода;
- политика и агитация в любом виде;
- коммерческая реклама сторонних товаров/услуг;
- анонсы без конкретики («скоро что-то будет», «следите за новостями»).

Ответь СТРОГО одним JSON без пояснений:
{{"decision": "publish" | "reject" | "unsure",
  "interest": <1-10>,
  "reason": "краткое объяснение по-русски",
  "event_title": "короткое название мероприятия или пустая строка",
  "event_date": "дата в формате YYYY-MM-DD, если известна, иначе пустая строка",
  "event_time": "время ЧЧ:ММ, если известно, иначе пустая строка"}}

"unsure" используй, только если действительно нельзя определить. Сегодня {TODAY}."""


def moderate(text: str) -> tuple[str, str, dict]:
    """Возвращает (decision, reason, event);
    decision ∈ publish/reject/review; event = {title, date, time}."""
    empty_event = {"title": "", "date": "", "time": ""}
    if not text.strip():
        # Пост без текста (только медиа) — решает человек
        return "review", "пост без текста", empty_event

    from datetime import date as _date
    payload = {
        "model": GROQ_MODEL,
        "messages": [{
            "role": "user",
            "content": _PROMPT.format(POST=text[:4000], TODAY=_date.today().isoformat()),
        }],
        "max_tokens": 300,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    try:
        raw = _groq_chat(payload)
        data = json.loads(raw)
        decision = data.get("decision", "unsure")
        interest = int(data.get("interest", 0) or 0)
        reason = str(data.get("reason", ""))[:300]
        event = {
            "title": str(data.get("event_title", ""))[:200],
            "date": str(data.get("event_date", ""))[:10],
            "time": str(data.get("event_time", ""))[:5],
        }
    except (GroqError, json.JSONDecodeError, KeyError, ValueError) as e:
        log.error("Модерация не удалась: %s", e)
        return "review", f"ошибка ИИ: {e}", empty_event

    if decision == "publish" and interest >= 6:
        return "publish", f"[{interest}/10] {reason}", event
    if decision == "publish":
        return "reject", f"[{interest}/10, ниже планки] {reason}", event
    if decision == "reject":
        return "reject", reason, event
    return "review", reason or "ИИ не уверен", event
