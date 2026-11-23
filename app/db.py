"""SQLite: схема и вспомогательные функции. Один файл БД на всех процессах (WAL)."""
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    tg_chat_id INTEGER UNIQUE,
    tg_bind_code TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    data TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    origin TEXT NOT NULL,              -- telegram | web | daily
    title TEXT,
    input_text TEXT,
    input_links TEXT DEFAULT '[]',
    input_media TEXT DEFAULT '[]',
    region_hint TEXT,
    status TEXT NOT NULL DEFAULT 'new', -- new|queued|researching|ready|partial|failed|cancelled|rejected_daily
    stage TEXT,
    score REAL,
    verdict TEXT,
    market TEXT,
    is_project INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    is_demo INTEGER DEFAULT 0,
    current_version INTEGER DEFAULT 0,
    fingerprint TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ideas_user ON ideas(user_id, updated_at);
CREATE TABLE IF NOT EXISTS idea_versions (
    id INTEGER PRIMARY KEY,
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    version INTEGER NOT NULL,
    report TEXT NOT NULL,
    econ_inputs TEXT NOT NULL DEFAULT '{}',
    econ_results TEXT NOT NULL DEFAULT '{}',
    scoring TEXT NOT NULL DEFAULT '{}',
    change_request TEXT,
    change_summary TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(idea_id, version)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    idea_id INTEGER,
    kind TEXT NOT NULL,                 -- research | refine | daily
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued', -- queued|running|done|failed|cancelled
    cancel_requested INTEGER DEFAULT 0,
    dedupe_key TEXT UNIQUE,
    attempts INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    error TEXT,
    run_after REAL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, run_after);
CREATE TABLE IF NOT EXISTS step_cache (       -- результаты платных шагов: перезапуск не повторяет оплату
    job_id INTEGER NOT NULL,
    step TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, step)
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    dedupe_key TEXT UNIQUE NOT NULL,
    user_id INTEGER NOT NULL,
    idea_id INTEGER,
    tg_message_id INTEGER,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_updates (
    update_id INTEGER PRIMARY KEY,
    received_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_message_map (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    idea_id INTEGER NOT NULL,
    PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS tg_pending (        -- черновики ввода (альбомы, голосовые на подтверждение)
    key TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    kind TEXT NOT NULL,                 -- trends_csv | manual | comtrade
    source TEXT NOT NULL,
    market TEXT,
    url TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    period TEXT,
    retrieved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    user_id INTEGER,
    job_id INTEGER,
    provider TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    searches INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_checks (
    source TEXT PRIMARY KEY,
    last_ok_at TEXT,
    last_error_at TEXT,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS error_log (
    id INTEGER PRIMARY KEY,
    place TEXT,
    message TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_tokens (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS qa (
    id INTEGER PRIMARY KEY,
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_runs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    run_date TEXT NOT NULL,
    idea_id INTEGER,
    summary TEXT,
    candidates TEXT DEFAULT '[]',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, run_date)
);
CREATE TABLE IF NOT EXISTS idea_feedback (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    version INTEGER NOT NULL,
    rating TEXT NOT NULL,
    comment TEXT NOT NULL,
    remember INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_owner ON idea_feedback(user_id, id);
CREATE TABLE IF NOT EXISTS project_progress (
    idea_id INTEGER NOT NULL REFERENCES ideas(id),
    step_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    note TEXT NOT NULL DEFAULT '',
    actual_cost REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(idea_id,step_key)
);
CREATE TABLE IF NOT EXISTS source_cache (
    cache_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    expires_at REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS budget_reservations (
    reservation_key TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    job_id INTEGER,
    budget_group INTEGER,
    amount REAL NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "budget_group" not in columns:
            try:
                conn.execute("ALTER TABLE jobs ADD COLUMN budget_group INTEGER")
            except sqlite3.OperationalError:
                if "budget_group" not in {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}:
                    raise
        usage_columns = {row["name"] for row in conn.execute("PRAGMA table_info(usage)")}
        if "step" not in usage_columns:
            try:
                conn.execute("ALTER TABLE usage ADD COLUMN step TEXT")
            except sqlite3.OperationalError:
                if "step" not in {row["name"] for row in conn.execute("PRAGMA table_info(usage)")}:
                    raise


def jdump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def jload(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def log_error(place: str, message: str) -> None:
    try:
        with db() as conn:
            conn.execute("INSERT INTO error_log(place, message, created_at) VALUES (?,?,?)",
                         (place, str(message)[:4000], now_iso()))
    except Exception:
        pass


def source_ok(source: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO source_checks(source,last_ok_at) VALUES(?,?) "
            "ON CONFLICT(source) DO UPDATE SET last_ok_at=excluded.last_ok_at", (source, now_iso()))


def source_fail(source: str, err: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO source_checks(source,last_error_at,last_error) VALUES(?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET last_error_at=excluded.last_error_at, last_error=excluded.last_error",
            (source, now_iso(), str(err)[:500]))


# ---------- пользователи, профиль, настройки ----------

DEFAULT_SETTINGS = {
    "daily_enabled": True,
    "daily_time": "09:00",
    "timezone": "Asia/Dubai",
    "language": "ru",
    "monthly_budget_usd": 50.0,      # лимит расходов на модель/поиск в месяц
    "per_idea_budget_usd": 1.5,      # лимит на одно исследование
    "base_currency": "USD",
    "market_priority": "USA",
    "evergreen_first": True,
    "research_mode": "standard",
    "refresh_enabled": True,
    "refresh_days": 7,
}

PROFILE_FIELDS = [
    ("budget_start", "Бюджет старта (USD)", "number"),
    ("max_test_loss", "Максимальная приемлемая потеря на тесте (USD)", "number"),
    ("target_income", "Желаемый доп. доход в месяц (USD)", "number"),
    ("hours_per_week", "Доступные часы в неделю", "number"),
    ("hourly_rate", "Стоимость нашего часа для расчёта (USD)", "number"),
    ("skills", "Навыки", "text"),
    ("space", "Помещение и место хранения", "text"),
    ("equipment_ready", "Готовность работать с оборудованием", "text"),
    ("launch_deadline", "Желаемый срок запуска", "text"),
    ("categories", "Допустимые категории товаров", "text"),
    ("operate_from", "Из какой страны управляем бизнесом", "text"),
    ("production_access", "Где доступны производство и склад (вкл. контакты фабрик)", "text"),
    ("company", "Есть ли компания (страна, тип)", "text"),
    ("bank_payments", "Банковский счёт и платёжное подключение (Shopify Payments / Stripe / др.)", "text"),
    ("infra_abroad", "Готовы ли создавать инфраструктуру в другой стране", "text"),
]


def get_profile(conn, user_id: int) -> dict:
    row = conn.execute("SELECT data FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    return jload(row["data"], {}) if row else {}


def save_profile(conn, user_id: int, data: dict) -> None:
    conn.execute(
        "INSERT INTO profiles(user_id,data,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
        (user_id, jdump(data), now_iso()))


def get_settings(conn, user_id: int) -> dict:
    row = conn.execute("SELECT data FROM settings WHERE user_id=?", (user_id,)).fetchone()
    s = dict(DEFAULT_SETTINGS)
    if row:
        s.update(jload(row["data"], {}))
    return s


def save_settings(conn, user_id: int, data: dict) -> None:
    conn.execute(
        "INSERT INTO settings(user_id,data) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
        (user_id, jdump(data)))


def month_spend(conn, user_id: int) -> float:
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE user_id=? AND created_at>=?",
                       (user_id, start)).fetchone()
    return float(row["s"])


def job_spend(conn, job_id: int) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd),0) s FROM usage WHERE job_id=?", (job_id,)).fetchone()
    return float(row["s"])


def group_spend(conn, group_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(u.cost_usd),0) s FROM usage u LEFT JOIN jobs j ON j.id=u.job_id "
        "WHERE u.job_id=? OR j.budget_group=?", (group_id, group_id)).fetchone()
    return float(row["s"])


def feedback(conn, user_id: int, idea_id: int | None = None, remembered=False):
    sql = "SELECT f.*, i.title FROM idea_feedback f JOIN ideas i ON i.id=f.idea_id WHERE f.user_id=?"
    args = [user_id]
    if idea_id is not None:
        sql += " AND f.idea_id=?"
        args.append(idea_id)
    if remembered:
        sql += " AND f.remember=1"
    return conn.execute(sql + " ORDER BY f.id DESC LIMIT 30", args).fetchall()


def save_feedback(conn, user_id: int, idea_id: int, rating: str, comment: str, remember=True, version=None):
    idea = get_idea(conn, idea_id, user_id)
    if not idea or rating not in ("like", "revise", "dislike") or not comment.strip():
        raise ValueError("Выберите оценку и напишите причину")
    version = idea["current_version"] if version is None else version
    if version and not conn.execute("SELECT 1 FROM idea_versions WHERE idea_id=? AND version=?",(idea_id,version)).fetchone():
        raise ValueError("Версия разбора не найдена")
    with tx(conn):
        previous = conn.execute("SELECT id FROM idea_feedback WHERE user_id=? AND idea_id=? AND version=? AND rating=? AND comment=?",
                                (user_id,idea_id,version,rating,comment.strip()[:3000])).fetchone()
        if previous:
            return previous["id"]
        cursor = conn.execute(
            "INSERT INTO idea_feedback(user_id,idea_id,version,rating,comment,remember,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, idea_id, version, rating, comment.strip()[:3000], int(remember), now_iso()))
        return cursor.lastrowid


def cache_get(key: str):
    with db() as conn:
        row = conn.execute("SELECT payload FROM source_cache WHERE cache_key=? AND expires_at>?", (key, ts())).fetchone()
    return jload(row["payload"]) if row else None


def cache_put(key: str, payload, ttl: float):
    with db() as conn:
        conn.execute(
            "INSERT INTO source_cache(cache_key,payload,expires_at,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at,updated_at=excluded.updated_at",
            (key, jdump(payload), ts() + ttl, now_iso()))


# ---------- идеи ----------

def get_idea(conn, idea_id: int, user_id: int):
    return conn.execute("SELECT * FROM ideas WHERE id=? AND user_id=?", (idea_id, user_id)).fetchone()


def latest_version(conn, idea_id: int):
    return conn.execute("SELECT * FROM idea_versions WHERE idea_id=? ORDER BY version DESC LIMIT 1",
                        (idea_id,)).fetchone()


def create_idea(conn, user_id: int, origin: str, text: str, links=None, media=None,
                region_hint: str | None = None, title: str | None = None) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO ideas(user_id,origin,title,input_text,input_links,input_media,region_hint,status,"
        "is_demo,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, origin, title or (text or "Идея")[:80], text, jdump(links or []), jdump(media or []),
         region_hint, "queued", 1 if config.FAKE_LLM else 0, ts, ts))
    return cur.lastrowid


def set_idea(conn, idea_id: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE ideas SET {cols} WHERE id=?", (*fields.values(), idea_id))


def save_version(conn, idea_id: int, report: dict, econ_inputs: dict, econ_results: dict,
                 scoring: dict, change_request: str | None, change_summary: str | None) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version),0) v FROM idea_versions WHERE idea_id=?", (idea_id,)).fetchone()
    v = int(row["v"]) + 1
    conn.execute(
        "INSERT INTO idea_versions(idea_id,version,report,econ_inputs,econ_results,scoring,change_request,"
        "change_summary,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (idea_id, v, jdump(report), jdump(econ_inputs), jdump(econ_results), jdump(scoring),
         change_request, change_summary, now_iso()))
    conn.execute("UPDATE ideas SET current_version=?, updated_at=? WHERE id=?", (v, now_iso(), idea_id))
    return v


def add_usage(conn, user_id, job_id, provider, model, in_tok, out_tok, searches, cost, step=None) -> None:
    conn.execute(
        "INSERT INTO usage(user_id,job_id,provider,model,input_tokens,output_tokens,searches,cost_usd,created_at,step) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)", (user_id, job_id, provider, model, in_tok, out_tok, searches, cost, now_iso(),step))


def ts() -> float:
    return time.time()

