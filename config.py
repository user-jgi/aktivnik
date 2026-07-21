import json
import os
import shutil
import threading
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHANNEL   = os.getenv("CHANNEL", "@aktivnik_nn")
OWNER_ID  = int(os.getenv("OWNER_ID", "965040732"))

# Каталог для данных, переживающих перезапуск. На Railway сюда монтируется том
# (DATA_DIR=/data); локально по умолчанию — папка проекта.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH      = DATA_DIR / "aktivnik.db"
SOURCES_PATH = DATA_DIR / "sources.json"
# Начальный список каналов, лежащий в репозитории (сид для первого запуска).
SEED_SOURCES = BASE_DIR / "sources.json"

def _sync_sources_from_seed():
    """Подтягивает новые источники из репозитория в список на томе.

    Первый запуск (том пуст) — просто копируем сид. На последующих деплоях
    добавляем те источники, которых ещё нет (по паре тип+id), не трогая
    добавленные вручную через /add.
    """
    if not SEED_SOURCES.exists():
        return
    if not SOURCES_PATH.exists():
        shutil.copy(SEED_SOURCES, SOURCES_PATH)
        return
    try:
        with open(SEED_SOURCES, encoding="utf-8") as f:
            seed = json.load(f)
        with open(SOURCES_PATH, encoding="utf-8") as f:
            current = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    have = {(s.get("type", "telegram"), s["id"].lower()) for s in current}
    added = [s for s in seed
             if (s.get("type", "telegram"), s["id"].lower()) not in have]
    if not added:
        return
    current.extend(added)
    tmp = SOURCES_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, indent=2)
    tmp.replace(SOURCES_PATH)


_sync_sources_from_seed()

# Окно, в котором пост считается «новым» при периодической проверке, дней.
# Старые посты при запуске НЕ обрабатываются (только помечаются как известные),
# чтобы не жечь лимиты Groq.
FRESH_WINDOW_DAYS = int(os.getenv("FRESH_WINDOW_DAYS", "2"))

# Периодическая проверка новых постов, секунд
CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "900"))

# Пауза между публикациями в канал (лимит Telegram ~20 сообщений/мин на канал)
PUBLISH_DELAY_SECONDS = 4

TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY", "")

# Сервисный ключ VK (dev.vk.com → приложение → «Сервисный ключ доступа»).
# Нужен для чтения открытых групп через wall.get.
VK_SERVICE_TOKEN = os.getenv("VK_SERVICE_TOKEN", "")

# Автопостинг в своё VK-сообщество (vk.com/aktivnik_nn).
# VK_POST_TOKEN — ключ доступа сообщества с правом «Стена»
# (Управление → Работа с API → Создать ключ). Пусто = постинг в VK выключен.
VK_POST_TOKEN = os.getenv("VK_POST_TOKEN", "")
VK_GROUP_ID   = os.getenv("VK_GROUP_ID", "240406559")

# Groq: до 5 ключей, ротация при 429 (как в hrbot3)
def _collect_groq_keys() -> list:
    raw = [os.getenv("GROQ_API_KEY", "")] + [
        os.getenv(f"GROQ_API_KEY_{i}", "") for i in range(2, 6)
    ]
    return [k.strip() for k in raw if k.strip() and k.strip() != "123"]

GROQ_API_KEYS = _collect_groq_keys()


_sources_lock = threading.Lock()


def load_sources() -> list[dict]:
    """Список источников из sources.json.

    Формат элемента: {"type": "telegram", "id": "username", "title": "..."}
    (type "vk" зарезервирован на будущее — сейчас обрабатывается только telegram).
    """
    with _sources_lock, open(SOURCES_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_sources(sources: list[dict]):
    with _sources_lock:
        tmp = SOURCES_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sources, f, ensure_ascii=False, indent=2)
        tmp.replace(SOURCES_PATH)
