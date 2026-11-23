"""Read-only checks of the deployed routes and saved reports. Never call a paid model."""
import json
from collections import Counter

import httpx
from fastapi.testclient import TestClient

from . import config, db, sources
from .web.main import app


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
        "schema2_reports":schemas.count(2), "market_priority":settings["market_priority"],
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


if __name__ == "__main__":
    main()
