"""Read-only route checks, with an explicit optional one-time budgeted API probe."""
import json
import sys
from collections import Counter

import httpx
from fastapi.testclient import TestClient

from . import config, db, sources, jobs, llm, analytics, memory
from .web.main import app


def verify_research_once(user_id, settings):
    """One bounded release probe, tracked in usage; completed probes never repeat."""
    with db.db() as conn, db.tx(conn):
        existing = conn.execute("SELECT * FROM jobs WHERE dedupe_key=?",("release-verify:2026-11-22-cost-controls",)).fetchone()
        if existing:
            assert existing["status"] == "done", "Release probe needs review before another paid attempt"
            print("RESEARCH_VERIFY " + json.dumps({"status":"already_verified", "cost_usd":db.job_spend(conn,existing["id"])}))
            return
        job_id = jobs.enqueue(conn,user_id,"verification",dedupe_key="release-verify:2026-11-22-cost-controls")
        conn.execute("UPDATE jobs SET status='running',started_at=?,attempts=1 WHERE id=?",(db.now_iso(),job_id))
    ctx = llm.Ctx(user_id,job_id,per_job_limit=min(0.5,float(settings["per_idea_budget_usd"])),
                  monthly_limit=float(settings["monthly_budget_usd"]))
    system = "Проверка соединения исследовательского API. Обязательно выполни ровно один веб-поиск. Ответь одной короткой строкой и ссылкой."
    prompt = "Найди официальную страницу Census Bureau Requesting a Census Data API Key от 11 мая 2026. Требуется ли ключ для запросов данных? Используй веб-поиск."
    try:
        text = llm.call(ctx,"release_verify",system,prompt,search=True,max_searches=1,max_tokens=800)
        with db.db() as conn:
            spent = db.job_spend(conn,job_id)
            searches = conn.execute("SELECT COALESCE(SUM(searches),0) FROM usage WHERE job_id=?",(job_id,)).fetchone()[0]
        assert text and searches == 1 and ctx.seen_sources, "Search did not return verifiable sources"
        cached = llm.call(ctx,"release_verify_cached",system,prompt,search=True,max_searches=1,max_tokens=800)
        with db.db() as conn:
            assert cached == text and db.job_spend(conn,job_id) == spent, "Repeated query cache failed"
            jobs.finish(conn,job_id,"done")
        print("RESEARCH_VERIFY " + json.dumps({"status":"ok","searches":searches,"cost_usd":round(spent,6),
            "cached_repeat_cost_usd":0,"limit_usd":ctx.per_job_limit}))
    except Exception as exc:
        with db.db() as conn:
            jobs.finish(conn,job_id,"failed",type(exc).__name__)
        raise RuntimeError("Research release probe failed: " + type(exc).__name__) from None


def main():
    response = httpx.get("http://127.0.0.1:8000/login", timeout=15)
    response.raise_for_status()
    assert response.status_code == 200, "Web process did not return the login page"
    client = TestClient(app, base_url="https://testserver")
    login = client.post("/login", data={"email": config.ADMIN_EMAIL,
                                       "password": config.ADMIN_PASSWORD}, follow_redirects=False)
    assert login.status_code == 303 and login.headers.get("location") == "/today", "Admin login failed"
    for path in ("/today", "/ideas", "/settings", "/sources", "/logs"):
        page = client.get(path)
        assert page.status_code == 200 and 'action="/login"' not in page.text, f"Route failed: {path}"
    with db.db() as conn:
        user = conn.execute("SELECT id FROM users WHERE email=?", (config.ADMIN_EMAIL,)).fetchone()
        settings = db.get_settings(conn, user["id"])
        ideas = conn.execute("SELECT id,current_version FROM ideas WHERE user_id=?", (user["id"],)).fetchall()
        statuses = dict(Counter(row["status"] for row in conn.execute("SELECT status FROM jobs")))
        profile = db.get_profile(conn, user["id"])
    for idea in ideas:
        page = client.get(f"/ideas/{idea['id']}")
        assert page.status_code == 200, f"Report rendering failed: {idea['id']}"
        assert f'action="/ideas/{idea["id"]}/feedback"' in page.text, "Feedback form missing"
        assert 'id="panel-basis"' in page.text or not idea["current_version"], "Evidence summary tab missing"
        if idea["current_version"]:
            assert 'id="panel-plan"' in page.text, "Test plan missing"
            oldest = client.get(f"/ideas/{idea['id']}?v=1")
            assert oldest.status_code == 200, "Old report no longer renders"
        invalid = client.post(f"/ideas/{idea['id']}/feedback", data={
            "rating":"revise", "comment":"invalid CSRF check", "csrf_token":"invalid"})
        assert invalid.status_code == 400, "CSRF validation failed"
    with db.db() as conn:
        schemas = [db.jload(db.latest_version(conn, idea["id"])["report"], {}).get("schema_version")
                   for idea in ideas if idea["current_version"]]
    print("RUNTIME_CHECK " + json.dumps({"routes":"ok", "reports":len(ideas),
        "historical_versions":"ok", "csrf":"ok", "paid_calls":0,
        "schema3_reports":schemas.count(3), "market_priority":settings["market_priority"],
        "evergreen_first":settings["evergreen_first"], "profile_complete":bool(profile.get("budget_start")) and bool(profile.get("hours_per_week")),
        "jobs":statuses}))
    try:
        context = sources.census_context()
        print("PUBLIC_SOURCE " + json.dumps({"source":"census", "status":"ok", "period":context["period"]}))
    except sources.SourceNeedsConnection:
        print("PUBLIC_SOURCE " + json.dumps({"source":"census", "status":"needs_connection", "requires":"CENSUS_API_KEY"}))
    except Exception as exc:
        db.source_fail("census", type(exc).__name__)
        print("PUBLIC_SOURCE " + json.dumps({"source":"census", "status":"unavailable", "error_type":type(exc).__name__}))
    print("SOURCE_ACCESS " + json.dumps({"keyword_planner_api":sources.google_ads_ready(),
        "trends":"manual_csv", "voice":bool(config.OPENAI_API_KEY)}))
    print("ANALYTICS_CONNECTIONS " + json.dumps({item["id"]:item["status"] for item in analytics.connections(user["id"])}))
    with db.db() as conn:
        context = memory.snapshot(conn,user["id"])
    print("FEEDBACK_MEMORY " + json.dumps({"active_feedback":context["active_feedback_total"],
        "retrieved":len(context["feedback"]),"rules":len(context["rules"]),"method":context["retrieval"]}))
    if "--verify-research-once" in sys.argv:
        verify_research_once(user["id"],settings)


if __name__ == "__main__":
    main()
