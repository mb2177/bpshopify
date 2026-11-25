"""Фоновый исполнитель: очередь задач + планировщик ежедневного исследования."""
import logging
import time
import traceback
import signal
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
            if not s.get("daily_enabled") and not s.get("refresh_enabled",True):
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
            if s.get("daily_enabled"):
                jobs.enqueue(conn, u["id"], "daily", None, {"date": day}, dedupe_key=f"daily:{u['id']}:{day}",
                             max_attempts=2)
            if s.get("refresh_enabled",True):
                for idea in conn.execute("SELECT id FROM ideas WHERE user_id=? AND archived=0 AND current_version>0 "
                                         "ORDER BY is_project DESC,updated_at DESC",(u["id"],)).fetchall():
                    jobs.enqueue(conn,u["id"],"refresh",idea["id"],dedupe_key=f"refresh:{idea['id']}:{day}",max_attempts=2)


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


def human_error(idea_id: int, e: Exception) -> str:
    """Сообщение об ошибке простыми словами: что случилось и что делать."""
    name = type(e).__name__
    text = str(e)
    if name == "ResponseTooLong":
        why = "ответ модели оказался слишком длинным даже после сокращения"
    elif name in ("JSONDecodeError",) or "JSON" in text:
        why = "модель вернула ответ в неправильном формате"
    elif name in TRANSIENT:
        why = "сервис Claude временно не отвечает"
    elif any("а" <= ch <= "я" for ch in text.lower()):
        why = text.rstrip(".")
    else:
        why = "не удалось обработать ответ сервиса"
    return (f"❌ Идея #{idea_id}: разбор не завершён — {why}.\n"
            f"Сохранённые данные доступны на сайте. Можно продолжить незавершённые шаги из карточки идеи.")


def run_job(job) -> None:
    kind = job["kind"]
    if kind == "research":
        research.run_research(job)
    elif kind == "refine":
        research.run_refine(job)
    elif kind == "daily":
        research.run_daily(job)
    elif kind == "recalculate":
        research.run_recalculate(job)
    elif kind == "refresh":
        research.run_refresh(job)
    elif kind == "analytics":
        research.run_analytics(job)
    elif kind == "connection_check":
        research.run_connection_check(job)
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
    except jobs.RestartRequested:
        with db.db() as conn:
            jobs.retry_later(conn,job["id"],0,"Продолжение после обновления приложения")
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
                    db.set_idea(conn, job["idea_id"], status="failed", stage="Разбор остановлен. Сохранённые шаги можно продолжить.")
                from . import notify
                if job["idea_id"]:
                    notify.simple(job["user_id"], human_error(job["idea_id"], e), f"fail:{job['id']}", job["idea_id"])
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db.init_db()
    ensure_admin()
    with db.db() as conn:
        n = jobs.recover_stale(conn)
        # Upgrade saved reports using stored data. No external model calls are needed for this job.
        for idea in conn.execute("SELECT id,user_id,current_version FROM ideas WHERE current_version>0 ORDER BY id DESC").fetchall():
            version = db.latest_version(conn,idea["id"])
            report = db.jload(version["report"],{})
            if report.get("schema_version") != 3 or (report.get("source_validation") or {}).get("rules_version")!=2:
                jobs.enqueue(conn,idea["user_id"],"recalculate",idea["id"],
                             dedupe_key=f"source-rules2:{idea['id']}:{idea['current_version']}")
    if n:
        log.info("Возвращено в очередь после перезапуска: %s", n)
    last_sched = 0.0
    def request_shutdown(_signal,_frame):
        llm.shutdown_requested = True
    signal.signal(signal.SIGTERM,request_shutdown)
    signal.signal(signal.SIGINT,request_shutdown)
    while not llm.shutdown_requested:
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

