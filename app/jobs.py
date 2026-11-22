"""Постоянная очередь задач в SQLite: идемпотентность, повторы, отмена, кеш платных шагов."""
import sqlite3

from . import db


class Cancelled(Exception):
    pass


def enqueue(conn, user_id: int, kind: str, idea_id: int | None = None, payload: dict | None = None,
            dedupe_key: str | None = None, max_attempts: int = 3) -> int | None:
    """Ставит задачу. Если задача с таким dedupe_key уже есть — возвращает её id, не создавая дубль."""
    if dedupe_key:
        row = conn.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if row:
            return row["id"]
    try:
        cur = conn.execute(
            "INSERT INTO jobs(user_id,idea_id,kind,payload,dedupe_key,max_attempts,created_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, idea_id, kind, db.jdump(payload or {}), dedupe_key, max_attempts, db.now_iso()))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT id FROM jobs WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        return row["id"] if row else None


def claim(conn):
    """Атомарно берёт следующую задачу."""
    with db.tx(conn):
        row = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' AND run_after<=? ORDER BY id LIMIT 1", (db.ts(),)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=? WHERE id=?",
                     (db.now_iso(), row["id"]))
    return conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()


def finish(conn, job_id: int, status: str = "done", error: str | None = None) -> None:
    conn.execute("UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                 (status, error, db.now_iso(), job_id))


def retry_later(conn, job_id: int, delay_s: float, error: str) -> None:
    conn.execute("UPDATE jobs SET status='queued', run_after=?, error=? WHERE id=?",
                 (db.ts() + delay_s, error, job_id))


def request_cancel(conn, job_id: int, user_id: int) -> bool:
    row = conn.execute("SELECT status FROM jobs WHERE id=? AND user_id=?", (job_id, user_id)).fetchone()
    if not row or row["status"] not in ("queued", "running"):
        return False
    if row["status"] == "queued":
        conn.execute("UPDATE jobs SET status='cancelled', cancel_requested=1, finished_at=? WHERE id=?",
                     (db.now_iso(), job_id))
    else:
        conn.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
    return True


def check_cancel(job_id: int | None) -> None:
    if not job_id:
        return
    with db.db() as conn:
        row = conn.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row and row["cancel_requested"]:
        raise Cancelled()


def recover_stale(conn) -> int:
    """После перезапуска воркера: зависшие running возвращаются в очередь (шаги берутся из кеша)."""
    cur = conn.execute("UPDATE jobs SET status='queued' WHERE status='running'")
    return cur.rowcount


def cached_step(job_id: int | None, step: str):
    if not job_id:
        return None
    with db.db() as conn:
        row = conn.execute("SELECT result FROM step_cache WHERE job_id=? AND step=?", (job_id, step)).fetchone()
    return db.jload(row["result"]) if row else None


def store_step(job_id: int | None, step: str, result) -> None:
    if not job_id:
        return
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO step_cache(job_id,step,result,created_at) VALUES(?,?,?,?)",
                     (job_id, step, db.jdump(result), db.now_iso()))


def active_jobs(conn, user_id: int):
    return conn.execute(
        "SELECT j.*, i.title FROM jobs j LEFT JOIN ideas i ON i.id=j.idea_id "
        "WHERE j.user_id=? AND j.status IN ('queued','running') ORDER BY j.id", (user_id,)).fetchall()
