"""One explicitly requested, budgeted daily research, executed by the normal worker."""
import argparse
import json
import time
from datetime import date

from . import config, db, diagnostics, jobs


OWNER_FEEDBACK = ("Требования владельца от 24 ноября 2026: приоритет США; адекватные, креативные товары "
    "с возможностью масштабирования и круглогодичного спроса. Не ограничиваться ОАЭ и праздниками. "
    "Убрать 30-дневный план. Для идеи дня самостоятельно выбирать комплектацию и размеры, явно указывать "
    "допущения, не спрашивать владельца про товар, который бот выбрал сам. «Ты о чём?» не является "
    "поручением запускать новое платное исследование. Выводы подтверждать источниками и расчётами.")


def prepare(user_id, run_date):
    """Never create a second daily job or overwrite existing reports/settings."""
    date.fromisoformat(run_date)
    with db.db() as conn:
        idea = conn.execute("SELECT id FROM ideas WHERE user_id=? AND id=4",(user_id,)).fetchone()
        if idea and not conn.execute("SELECT 1 FROM idea_feedback WHERE user_id=? AND comment=?",(user_id,OWNER_FEEDBACK)).fetchone():
            db.save_feedback(conn,user_id,idea["id"],"dislike",OWNER_FEEDBACK,True)
        with db.tx(conn):
            return jobs.enqueue(conn,user_id,"daily",payload={"date":run_date},
                dedupe_key=f"daily:{user_id}:{run_date}",max_attempts=2)


def result(user_id, run_date, root_id):
    with db.db() as conn:
        daily = conn.execute("SELECT * FROM daily_runs WHERE user_id=? AND run_date=?",(user_id,run_date)).fetchone()
        root = conn.execute("SELECT status,error FROM jobs WHERE id=?",(root_id,)).fetchone()
        active = conn.execute("SELECT 1 FROM jobs WHERE (id=? OR budget_group=?) AND status IN ('queued','running')",(root_id,root_id)).fetchone()
        if active:
            return None
        if not daily or not daily["idea_id"]:
            return {"status":"no_candidate","job_status":root["status"],
                "reason":diagnostics.redact(root["error"] or (daily["summary"] if daily else "No daily result"),user_id)}
        idea = db.get_idea(conn,daily["idea_id"],user_id)
        version = db.latest_version(conn,idea["id"])
        report = db.jload(version["report"],{}) if version else {}
        score = db.jload(version["scoring"],{}) if version else {}
        explanation = report.get("basis") or {}
        steps = [dict(row) for row in conn.execute("SELECT u.step,COUNT(*) calls,SUM(u.searches) searches,SUM(u.cost_usd) cost_usd "
            "FROM usage u JOIN jobs j ON j.id=u.job_id WHERE j.id=? OR j.budget_group=? GROUP BY u.step",(root_id,root_id))]
        complete = bool(report.get("market") and report.get("production"))
        return {"status":"complete" if complete else "incomplete","idea_id":idea["id"],"idea_status":idea["status"],
            "url":f"{config.SITE_URL}/ideas/{idea['id']}","title":idea["title"],
            "product":report.get("market",{}).get("product"),"buyer":report.get("market",{}).get("buyer"),
            "score":score.get("score"),"market":score.get("market"),"verdict":score.get("verdict_label"),
            "assumptions":report.get("assumptions",[]),"source_validation":report.get("source_validation"),
            "basis":explanation,"production_present":bool(report.get("production")),"partial":report.get("partial"),
            "cost_usd":round(db.group_spend(conn,root_id),6),"steps":steps}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare-date",required=True)
    args = parser.parse_args()
    with db.db() as conn:
        user = conn.execute("SELECT id FROM users WHERE email=?",(config.ADMIN_EMAIL,)).fetchone()
    root = prepare(user["id"],args.prepare_date)
    print("RESEARCH_PREPARED_JOB "+json.dumps({"id":root,"date":args.prepare_date}),flush=True)
    deadline = time.monotonic()+660
    while time.monotonic()<deadline:
        outcome = result(user["id"],args.prepare_date,root)
        if outcome is not None:
            print("RESEARCH_PREPARED_RESULT "+json.dumps(outcome,ensure_ascii=False),flush=True)
            if outcome["status"]!="complete":
                raise RuntimeError("Full research needs review: "+outcome["status"])
            return
        time.sleep(3)
    raise RuntimeError("Research is still queued/running; no second paid job was created")


if __name__=="__main__":
    main()
