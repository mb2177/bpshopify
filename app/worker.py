"""Фоновый исполнитель: очередь задач + планировщик ежедневного исследования."""
import logging
import time
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

from . import db, jobs, llm, research
from .web.auth import ensure_admin

log = logging.getLogger("worker")

TRANSIENT = ("APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError",
             "OverloadedError", "ConnectError", "ReadTimeout", "ServiceUnavailableError")


def schedule_daily() -> None:
    """Ставит ежедневное исследование, когда наступило время владельца (учитывает часовой пояс и DST)."""
    with db.db() as conn:
        users = conn.execute("SELECT id FROM users").fetchall()
        for u in users:
            s = db.get_settings(conn, u["id"])
            if not s.get("daily_enabled"):
                continue
            try:
                tz = ZoneInfo(s.get("timezone") or "Asia/Dubai")
                hh, mm = [int(x) for x in str(s.get("daily_time", "09:00")).split(":")[:2]]
            except Exception:
                continue
            now = datetime.now(tz)
            if (now.hour, now.minute) < (hh, mm):
                continue
            day = now.strftime("%Y-%m-%d")
            jobs.enqueue(conn, u["id"], "daily", None, {"date": day}, dedupe_key=f"daily:{u['id']}:{day}",
                         max_attempts=2)


def next_daily_run(conn, user_id: int) -> str:
    s = db.get_settings(conn, user_id)
    if not s.get("daily_enabled"):
        return "выключено"
    tz = ZoneInfo(s.get("timezone") or "Asia/Dubai")
    hh, mm = [int(x) for x in str(s.get("daily_time", "09:00")).split(":")[:2]]
    now = datetime.now(tz)
    day = now.strftime("%Y-%m-%d")
    done = conn.execute("SELECT 1 FROM jobs WHERE dedupe_key=?", (f"daily:{user_id}:{day}",)).fetchone()
    if done or (now.hour, now.minute) >= (hh, mm):
        return f"завтра в {hh:02d}:{mm:02d} ({tz.key})"
    return f"сегодня в {hh:02d}:{mm:02d} ({tz.key})"


def backup_db() -> None:
    """Ежедневная копия базы (хранится 14 последних)."""
    from . import config
    bdir = config.DATA_DIR / "backups"
    bdir.mkdir(exist_ok=True)
    target = bdir / f"app-{datetime.now():%Y%m%d}.db"
    if target.exists():
        return
    import sqlite3
    with db.db() as conn:
        dst = sqlite3.connect(target)
        conn.backup(dst)
        dst.close()
    for old in sorted(bdir.glob("app-*.db"))[:-14]:
        old.unlink(missing_ok=True)


def run_job(job) -> None:
    kind = job["kind"]
    if kind == "research":
        research.run_research(job)
    elif kind == "refine":
        research.run_refine(job)
    elif kind == "daily":
        research.run_daily(job)
    else:
        raise ValueError(f"Неизвестный тип задачи {kind}")


def process_one() -> bool:
    with db.db() as conn:
        job = jobs.claim(conn)
    if not job:
        return False
    try:
        run_job(job)
        with db.db() as conn:
            jobs.finish(conn, job["id"], "done")
    except jobs.Cancelled:
        with db.db() as conn:
            jobs.finish(conn, job["id"], "cancelled")
    except Exception as e:
        name = type(e).__name__
        msg = f"{name}: {e}"
        db.log_error(f"job {job['id']} {job['kind']}", msg + "\n" + traceback.format_exc())
        with db.db() as conn:
            if name in TRANSIENT and job["attempts"] < job["max_attempts"]:
                jobs.retry_later(conn, job["id"], 60 * job["attempts"], msg)  # шаги из кеша не оплачиваются повторно
            else:
                jobs.finish(conn, job["id"], "failed", msg)
                if job["idea_id"]:
                    db.set_idea(conn, job["idea_id"], status="failed", stage=f"Ошибка: {name}")
                from . import notify
                if job["idea_id"]:
                    notify.simple(job["user_id"], f"❌ Идея #{job['idea_id']}: ошибка исследования ({name}). "
                                                  f"Подробности в журнале на сайте.", f"fail:{job['id']}", job["idea_id"])
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    ensure_admin()
    with db.db() as conn:
        n = jobs.recover_stale(conn)
    if n:
        log.info("Возвращено в очередь после перезапуска: %s", n)
    last_sched = 0.0
    while True:
        try:
            if time.time() - last_sched > 30:
                schedule_daily()
                backup_db()
                last_sched = time.time()
            if not process_one():
                time.sleep(2)
        except Exception as e:
            db.log_error("worker-loop", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
