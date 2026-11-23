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


def test_telegram_card_explains_verdict():
    from app import economics, notify, scoring
    econ = economics.compute(economics.default_inputs())
    rep = {"markets": {mk: {"scores": {k: {"score": 6} for k in ("demand", "intent", "competition", "feasibility")}}
                       for mk in ("USA", "UAE")}, "main_risk": "Сезон короткий", "next_step": {"action": "Заказать образцы"}}
    sc = scoring.evaluate(rep, econ, {})
    txt = notify.explain(sc, {"market": rep})
    assert "По рынкам" in txt and "Почему:" in txt and "Цена товара" in txt
    assert "Риск: Сезон короткий" in txt and "Шаг: Заказать образцы" in txt


def test_daily_idea_never_asks_owner(monkeypatch):
    """Идею дня выбрал бот — вопрос владельцу не отправляется, бот делает допущение."""
    from app import fake_llm, notify
    u = uid()
    orig = fake_llm.respond
    def with_question(step, content):
        out = orig(step, content)
        if step == "parse":
            out = out.replace('"critical_question": null', '"critical_question": "Только этикетки или ещё крышки?"')
        return out
    monkeypatch.setattr(fake_llm, "respond", with_question)
    asked = []
    monkeypatch.setattr(notify, "ask", lambda *a, **k: asked.append(a))
    with db.db() as conn:
        daily_id = db.create_idea(conn, u, "daily", "Растворимые этикетки для банок")
        own_id = db.create_idea(conn, u, "telegram", "Этикетки для банок")
        jobs.enqueue(conn, u, "research", daily_id, dedupe_key=f"dq:{daily_id}")
        jobs.enqueue(conn, u, "research", own_id, dedupe_key=f"dq:{own_id}")
    drain()
    with db.db() as conn:
        daily, own = db.get_idea(conn, daily_id, u), db.get_idea(conn, own_id, u)
        rep = db.jload(db.latest_version(conn, daily_id)["report"])
    assert daily["status"] == "ready" and rep["assumptions"] == ["Только этикетки или ещё крышки?"]
    assert own["status"] == "needs_input" and len(asked) == 1 and asked[0][1] == own_id
    # ответ владельца продолжает исследование и больше не спрашивает
    with db.db() as conn:
        jobs.enqueue(conn, u, "refine", own_id, {"text": "только этикетки"}, dedupe_key=f"ans:{own_id}")
    drain()
    with db.db() as conn:
        own = db.get_idea(conn, own_id, u)
    assert own["status"] == "ready" and "Ответ владельца: только этикетки" in own["input_text"] and len(asked) == 1


def test_confused_reply_detection():
    from app.bot import is_confused
    assert is_confused("Ты вообще о чем?") and is_confused("не понял") and is_confused("?")
    assert not is_confused("только этикетки, размеры regular и wide-mouth")


def test_too_long_answer_retries_compact(monkeypatch):
    from app import llm
    calls = []
    def fake_call(ctx, step, system, content, **kw):
        calls.append((step, kw.get("max_tokens"), kw.get("max_searches")))
        if not step.endswith(":compact"):
            raise llm.ResponseTooLong("long")
        return '```json\n{"ok": true}\n```'
    monkeypatch.setattr(llm, "call", fake_call)
    out = llm.json_call(llm.Ctx(user_id=1), "market", "sys", "text", search=True, max_tokens=12000, max_searches=3)
    assert out == {"ok": True} and calls[1] == ("market:compact", 18000, 1)


def test_human_error_message():
    from app import llm, worker
    msg = worker.human_error(4, llm.ResponseTooLong("x"))
    assert "Идея #4" in msg and "слишком длинным" in msg and "ValueError" not in msg
