"""Сквозная проверка на ДЕМО-модели: очередь, разбор, версии, уточнения, дедупликация, доступ."""
from app import config, db, economics, jobs, sources, worker
from app.web.auth import ensure_admin


def setup_module(_):
    ensure_admin()


def uid():
    with db.db() as conn:
        return conn.execute("SELECT id FROM users").fetchone()["id"]


def drain():
    while worker.process_one():
        pass


def test_full_research_and_refine():
    u = uid()
    with db.db() as conn:
        db.save_profile(conn, u, {"budget_start": 5000, "hours_per_week": 20, "hourly_rate": 15})
        idea_id = db.create_idea(conn, u, "telegram", "Проверь идею: кастомные футболки для свадеб")
        j1 = jobs.enqueue(conn, u, "research", idea_id, dedupe_key=f"tg-idea:1:{idea_id}")
        j2 = jobs.enqueue(conn, u, "research", idea_id, dedupe_key=f"tg-idea:1:{idea_id}")
    assert j1 == j2  # повтор события не создаёт дубль
    drain()
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, u)
        ver = db.latest_version(conn, idea_id)
        n_jobs = conn.execute("SELECT COUNT(*) c FROM jobs WHERE idea_id=?", (idea_id,)).fetchone()["c"]
    assert n_jobs == 1
    assert idea["status"] == "ready" and idea["score"] is not None and idea["is_demo"] == 1
    assert idea["market"] in ("USA", "UAE")
    sc = db.jload(ver["scoring"])
    assert 0 <= sc["score"] <= 10 and sc["is_probability"] is False
    econ = db.jload(ver["econ_results"])
    assert econ["markets"]["USA"]["status"] == "ok"

    # уточнение экономики: новая версия без повторного поиска (шаги market/production не вызываются)
    with db.db() as conn:
        jobs.enqueue(conn, u, "refine", idea_id, {"text": "А если партия 50 шт?"}, dedupe_key="r1")
    drain()
    with db.db() as conn:
        v2 = db.latest_version(conn, idea_id)
        jid = conn.execute("SELECT id FROM jobs WHERE dedupe_key='r1'").fetchone()["id"]
        steps = [r["step"] for r in conn.execute("SELECT step FROM step_cache WHERE job_id=?", (jid,))]
    assert v2["version"] == 2
    assert db.jload(v2["econ_inputs"])["markets"]["USA"]["batch_size"] == 50
    assert steps == ["route"]

    # конкуренты в регионе
    with db.db() as conn:
        jobs.enqueue(conn, u, "refine", idea_id, {"text": "найди конкурентов в Дубае"}, dedupe_key="r2")
    drain()
    with db.db() as conn:
        v3 = db.latest_version(conn, idea_id)
    rep = db.jload(v3["report"])
    assert rep["competitor_searches"] and rep["competitor_searches"][0]["competitors"][0]["market"] == "UAE"

    # вопрос — ответ сохраняется, версия не меняется
    with db.db() as conn:
        jobs.enqueue(conn, u, "refine", idea_id, {"text": "какой рынок первый?"}, dedupe_key="r3")
    drain()
    with db.db() as conn:
        assert db.latest_version(conn, idea_id)["version"] == 3
        assert conn.execute("SELECT COUNT(*) c FROM qa WHERE idea_id=?", (idea_id,)).fetchone()["c"] == 1


def test_cancel_queued():
    u = uid()
    with db.db() as conn:
        idea_id = db.create_idea(conn, u, "web", "Идея для отмены")
        jid = jobs.enqueue(conn, u, "research", idea_id, dedupe_key=f"c:{idea_id}")
        assert jobs.request_cancel(conn, jid, u)
    drain()
    with db.db() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == "cancelled"
        assert db.latest_version(conn, idea_id) is None


def test_restart_reuses_paid_steps():
    u = uid()
    with db.db() as conn:
        idea_id = db.create_idea(conn, u, "web", "Идея с перезапуском")
        jid = jobs.enqueue(conn, u, "research", idea_id, dedupe_key=f"rs:{idea_id}")
    jobs.store_step(jid, "parse", {"text": '```json\n{"kind":"new_idea","title":"Из кеша"}\n```', "sources": []})
    with db.db() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
        assert jobs.recover_stale(conn) >= 1
    drain()
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, u)
    assert idea["status"] == "ready"


def test_daily_dedupe():
    u = uid()
    with db.db() as conn:
        s = db.get_settings(conn, u)
        s.update(daily_enabled=True, daily_time="00:00", timezone="Asia/Dubai")
        db.save_settings(conn, u, s)
    worker.schedule_daily()
    worker.schedule_daily()
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM jobs WHERE kind='daily'").fetchone()["c"] == 1
    drain()
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM daily_runs").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) c FROM ideas WHERE origin='daily'").fetchone()["c"] == 1


def test_trends_csv():
    csv = ("Category: All categories\n\nWeek,custom shirts: (United Arab Emirates)\n" +
           "\n".join(f"2023-{(i // 4) % 12 + 1:02d}-{(i % 4) * 7 + 1:02d},{10 + (i % 10)}" for i in range(60))).encode()
    res = sources.parse_trends_csv(csv)
    st = list(res["stats"].values())[0]
    assert res["points"] == 60 and st["peak_month"] in range(1, 13)


def test_manual_sources_not_auto():
    st = {p["id"]: p["status"] for p in sources.all_passports()}
    assert st["google_trends"] == sources.MANUAL
    assert st["meta_ad_library"] == sources.MANUAL
    assert st["shopify"] == sources.LATER
    assert st["voice"] == sources.KEY  # ключа нет → видно владельцу


def test_web_access_isolation():
    from fastapi.testclient import TestClient
    from app.web.main import app
    c = TestClient(app)
    assert c.get("/ideas/1", follow_redirects=False).status_code == 303  # без входа — на логин
    r = c.post("/login", data={"email": config.ADMIN_EMAIL, "password": "secret123"}, follow_redirects=False)
    assert r.status_code == 303
    for path in ("/today", "/ideas", "/ideas/1", "/ideas/1?v=1", "/settings", "/sources", "/logs", "/ideas/new"):
        resp = c.get(path)
        assert resp.status_code == 200, (path, resp.text[:500])
    # чужой пользователь не видит идею
    from app.web.auth import hash_password
    with db.db() as conn:
        conn.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)",
                     ("other@x", hash_password("p"), db.now_iso()))
    c2 = TestClient(app)
    c2.post("/login", data={"email": "other@x", "password": "p"})
    assert c2.get("/ideas/1").status_code == 404


def test_web_econ_edit_recalcs():
    from fastapi.testclient import TestClient
    from app.web.main import app
    import re
    c = TestClient(app)
    c.post("/login", data={"email": config.ADMIN_EMAIL, "password": "secret123"})
    page = c.get("/ideas/1").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
    with db.db() as conn:
        before = db.latest_version(conn, 1)["version"]
    r = c.post("/ideas/1/econ", data={"csrf_token": token, "market": "USA", "price": "45", "cac_base": "9"},
               follow_redirects=False)
    assert r.status_code == 303
    with db.db() as conn:
        v = db.latest_version(conn, 1)
    assert v["version"] == before + 1
    inputs = db.jload(v["econ_inputs"])["markets"]["USA"]
    assert inputs["price"] == 45 and inputs["_meta"]["price"]["source"] == "Задано владельцем"
    # плохой CSRF отклоняется
    assert c.post("/ideas/1/econ", data={"csrf_token": "bad", "market": "USA"}).status_code == 400


def test_deep_research_uses_opus_and_keeps_owner_edits(monkeypatch):
    from app import economics, llm, research
    u = uid()
    with db.db() as conn:
        idea_id = db.create_idea(conn, u, "web", "Идея для глубокого разбора")
        jobs.enqueue(conn, u, "research", idea_id, dedupe_key=f"base:{idea_id}")
    drain()
    research.recalc_from_form(u, idea_id, "USA", {"price": 77})
    models = []
    orig = llm.json_call
    def spy(ctx, step, system, content, **kw):
        models.append((step, kw.get("model"), ctx.per_job_limit))
        return orig(ctx, step, system, content, **kw)
    monkeypatch.setattr(llm, "json_call", spy)
    with db.db() as conn:
        jobs.enqueue(conn, u, "research", idea_id, {"deep": True}, dedupe_key=f"deep:{idea_id}")
    drain()
    used = {s: m for s, m, _ in models}
    assert used["market"] == config.MODEL_DEEP and used["production"] == config.MODEL_DEEP
    assert used["parse"] == config.MODEL_FAST
    with db.db() as conn:
        v = db.latest_version(conn, idea_id)
    assert v["change_summary"] == "Глубокий разбор (Opus)"
    assert db.jload(v["report"])["deep"] is True
    assert db.jload(v["econ_inputs"])["markets"]["USA"]["price"] == 77  # правка владельца сохранена
