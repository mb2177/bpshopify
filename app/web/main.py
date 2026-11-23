"""Веб-кабинет: история идей, полные разборы, экономика, настройки, источники."""
import re
import time
import uuid
import math
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import config, db, economics, jobs, notify, research, scoring, sources
from ..worker import next_daily_run
from . import auth

BASE = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    auth.ensure_admin()
    yield


app = FastAPI(title="Shopify Research", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, max_age=60 * 60 * 24 * 30,
                   https_only=config.SITE_URL.startswith("https"), same_site="lax")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ---------- форматирование для шаблонов ----------

def money(v, cur="", digits=2):
    if v is None or v == "":
        return "неизвестно"
    try:
        return f"{float(v):,.{digits}f} {cur}".replace(",", " ").strip()
    except (TypeError, ValueError):
        return str(v)


def pct(v, digits=1):
    if v is None:
        return "—"
    return f"{v * 100:.{digits}f}%"


TYPE_LABEL = {"observation": "наблюдение", "estimate": "оценка", "assumption": "допущение"}
STATUS_LABEL = {"queued": "в очереди", "researching": "исследуется", "ready": "готово", "partial": "частично готово",
                "failed": "ошибка", "cancelled": "остановлено", "new": "новая", "needs_input":"нужен ваш ответ"}
GATE_ICON = {"ok": "✅", "fail_hard": "⛔", "fail_soft": "⚠️", "unknown": "❔"}
FEAS = {"ok": "✅ ok", "risk": "⚠️ риск", "blocked": "⛔ блок", "unknown": "❔ нет данных"}

templates.env.globals.update(money=money, pct=pct, MARKET_LABEL=config.MARKET_LABEL, TYPE_LABEL=TYPE_LABEL,
                             STATUS_LABEL=STATUS_LABEL, GATE_ICON=GATE_ICON, FEAS=FEAS, MARKETS=config.MARKETS,
                             FIELD_LABELS=economics.FIELD_LABELS, SCENARIO_LABEL=economics.SCENARIO_LABEL,
                             PCT_FIELDS=economics.PCT_FIELDS, SITE_URL=config.SITE_URL, FAKE=config.FAKE_LLM)
templates.env.filters["fromjson"] = lambda s: db.jload(s, {})


def current_user(request: Request):
    uid = request.session.get("uid")
    if not uid:
        return None
    with db.db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def need_user(request: Request):
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=303, headers={"Location": f"/login?next={request.url.path}"})
    return u


def csrf(request: Request, token: str | None):
    if not auth.check_csrf(request.session, token):
        raise HTTPException(400, "CSRF: обновите страницу и повторите")


def render(request: Request, name: str, **ctx):
    ctx.update(request=request, csrf=auth.csrf_token(request.session), user=current_user(request))
    return templates.TemplateResponse(request, name, ctx)


def go(url: str):
    return RedirectResponse(url, status_code=303)


# ---------- вход ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/today", error: str = ""):
    return render(request, "login.html", next=next, error=error,
                  no_admin=not config.ADMIN_PASSWORD)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), next: str = Form("/today")):
    time.sleep(0.4)
    with db.db() as conn:
        u = conn.execute("SELECT * FROM users WHERE email=?", (email.strip(),)).fetchone()
    if not u or not auth.verify_password(password, u["password_hash"]):
        return go("/login?error=1")
    request.session.clear()
    request.session["uid"] = u["id"]
    return go(next if next.startswith("/") else "/today")


@app.get("/auth/tg/{tok}")
def tg_login(request: Request, tok: str):
    uid = auth.use_login_token(tok)
    if not uid:
        return go("/login?error=2")
    request.session.clear()
    request.session["uid"] = uid
    return go("/today")


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/login")


# ---------- страницы ----------

@app.get("/")
def root():
    return go("/today")


@app.get("/today", response_class=HTMLResponse)
def today(request: Request):
    u = need_user(request)
    with db.db() as conn:
        daily = conn.execute("SELECT * FROM daily_runs WHERE user_id=? ORDER BY run_date DESC LIMIT 1", (u["id"],)).fetchone()
        daily_idea = db.get_idea(conn, daily["idea_id"], u["id"]) if daily and daily["idea_id"] else None
        daily_scoring = None
        if daily_idea:
            v = db.latest_version(conn, daily_idea["id"])
            daily_scoring = db.jload(v["scoring"]) if v else None
        recent = conn.execute("SELECT * FROM ideas WHERE user_id=? AND archived=0 ORDER BY updated_at DESC LIMIT 8",
                              (u["id"],)).fetchall()
        active = jobs.active_jobs(conn, u["id"])
        profile = db.get_profile(conn, u["id"])
        nxt = next_daily_run(conn, u["id"])
        last_ok = conn.execute("SELECT MAX(finished_at) t FROM jobs WHERE user_id=? AND kind='daily' AND status='done'",
                               (u["id"],)).fetchone()["t"]
        spend = db.month_spend(conn, u["id"])
        s = db.get_settings(conn, u["id"])
    return render(request, "today.html", daily=daily, daily_idea=daily_idea, daily_scoring=daily_scoring,
                  candidates=db.jload(daily["candidates"], []) if daily else [], recent=recent, active=active,
                  profile_empty=not any(profile.values()) if profile else True, next_run=nxt, last_ok=last_ok,
                  spend=spend, settings=s, tg_linked=bool(u["tg_chat_id"]))


@app.get("/ideas", response_class=HTMLResponse)
def ideas(request: Request, f: str = "all", q: str = ""):
    u = need_user(request)
    sql = "SELECT * FROM ideas WHERE user_id=?"
    args = [u["id"]]
    if f == "projects":
        sql += " AND is_project=1 AND archived=0"
    elif f == "archived":
        sql += " AND archived=1"
    elif f == "daily":
        sql += " AND origin='daily' AND archived=0"
    else:
        sql += " AND archived=0"
    if q:
        sql += " AND (title LIKE ? OR input_text LIKE ?)"
        args += [f"%{q}%", f"%{q}%"]
    with db.db() as conn:
        rows = conn.execute(sql + " ORDER BY updated_at DESC LIMIT 300", args).fetchall()
    return render(request, "ideas.html", rows=rows, f=f, q=q)


@app.get("/ideas/new", response_class=HTMLResponse)
def new_idea_page(request: Request):
    need_user(request)
    return render(request, "new_idea.html")


@app.post("/ideas/new")
async def new_idea(request: Request, text: str = Form(""), region: str = Form(""), csrf_token: str = Form(...),
                   photos: list[UploadFile] = File(default=[])):
    u = need_user(request)
    csrf(request, csrf_token)
    media = []
    d = config.UPLOAD_DIR / str(u["id"])
    d.mkdir(parents=True, exist_ok=True)
    for ph in photos or []:
        if not ph.filename:
            continue
        data = await ph.read()
        if len(data) > 10_000_000 or not (ph.content_type or "").startswith("image/"):
            continue
        name = f"web-{uuid.uuid4().hex}{Path(ph.filename).suffix.lower()[:5]}"
        (d / name).write_bytes(data)
        media.append({"type": "photo", "path": str(d / name), "mime": ph.content_type})
    if not text.strip() and not media:
        return go("/ideas/new")
    links = [{"url": x.rstrip(").,")} for x in re.findall(r"https?://\S+", text)]
    with db.db() as conn:
        idea_id = db.create_idea(conn, u["id"], "web", text.strip(), links=links, media=media,
                                 region_hint=region.strip() or None)
        jobs.enqueue(conn, u["id"], "research", idea_id, dedupe_key=f"research:{idea_id}")
    return go(f"/ideas/{idea_id}")


def _load_idea(u, idea_id: int, v: int | None = None):
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, u["id"])
        if not idea:
            raise HTTPException(404, "Идея не найдена")
        versions = conn.execute("SELECT version, change_summary, change_request, created_at FROM idea_versions "
                                "WHERE idea_id=? ORDER BY version DESC", (idea_id,)).fetchall()
        if v:
            ver = conn.execute("SELECT * FROM idea_versions WHERE idea_id=? AND version=?", (idea_id, v)).fetchone()
        else:
            ver = db.latest_version(conn, idea_id)
        evidence = conn.execute("SELECT * FROM evidence WHERE idea_id=? ORDER BY id DESC", (idea_id,)).fetchall()
        qa = conn.execute("SELECT * FROM qa WHERE idea_id=? ORDER BY id DESC", (idea_id,)).fetchall()
        job = conn.execute("SELECT * FROM jobs WHERE idea_id=? AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                           (idea_id,)).fetchone()
    return idea, ver, versions, evidence, qa, job


@app.get("/ideas/{idea_id}", response_class=HTMLResponse)
def idea_page(request: Request, idea_id: int, v: int | None = None):
    u = need_user(request)
    idea, ver, versions, evidence, qa, job = _load_idea(u, idea_id, v)
    report = db.jload(ver["report"], {}) if ver else {}
    with db.db() as conn:
        feedback = db.feedback(conn,u["id"],idea_id)
        progress = {r["step_key"]:dict(r) for r in conn.execute("SELECT * FROM project_progress WHERE idea_id=?",(idea_id,))}
        spend = conn.execute("SELECT COALESCE(SUM(u.cost_usd),0) n FROM usage u JOIN jobs j ON j.id=u.job_id WHERE j.idea_id=? AND u.user_id=?",
                             (idea_id,u["id"])).fetchone()["n"]
        settings = db.get_settings(conn,u["id"])
        refreshed = conn.execute("SELECT last_ok_at FROM source_checks WHERE source=?",(f"idea:{idea_id}:refresh",)).fetchone()
    return render(request, "idea.html", idea=idea, ver=ver, versions=versions, evidence=evidence, qa=qa, job=job,
                  feedback=feedback,progress=progress,research_spend=spend,settings=settings,
                  last_refresh=refreshed["last_ok_at"] if refreshed else None,
                  report=report, mrep=report.get("market") or {}, prod=report.get("production") or {},
                  econ_inputs=db.jload(ver["econ_inputs"], {}) if ver else {},
                  econ=db.jload(ver["econ_results"], {}) if ver else {},
                  sc=db.jload(ver["scoring"], {}) if ver else {},
                  media=db.jload(idea["input_media"], []), links=db.jload(idea["input_links"], []),
                  is_latest=(not v) or (ver and ver["version"] == idea["current_version"]),
                  rules=scoring.RULES_TEXT)


@app.get("/ideas/{idea_id}/status")
def idea_status(request: Request, idea_id: int):
    u = need_user(request)
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, u["id"])
    if not idea:
        raise HTTPException(404)
    return JSONResponse({"status": idea["status"], "stage": idea["stage"], "version": idea["current_version"]})


@app.post("/ideas/{idea_id}/econ")
async def idea_econ(request: Request, idea_id: int):
    u = need_user(request)
    form = await request.form()
    csrf(request, form.get("csrf_token"))
    market = form.get("market")
    if market not in config.MARKETS:
        raise HTTPException(400)
    changes = {}
    for k in economics.FIELD_LABELS:
        if k in form:
            val = str(form.get(k)).strip().replace(",", ".")
            if k == "price_includes_vat":
                changes[k] = val in ("1", "on", "true")
            elif k == "unit_cost_currency":
                changes[k] = val.upper() if val.upper() in ("USD", "AED") else "USD"
            else:
                try:
                    changes[k] = float(val) if val != "" else None
                except ValueError:
                    pass
    if "price_includes_vat" not in form and "price" in form:
        changes["price_includes_vat"] = False
    items = {}
    for key in ("fixed_monthly", "one_time"):
        names, amounts, curs = form.getlist(f"{key}_name"), form.getlist(f"{key}_amount"), form.getlist(f"{key}_currency")
        if names:
            lst = []
            for n, a, c in zip(names, amounts, curs):
                try:
                    if n.strip() and a.strip():
                        lst.append({"name": n.strip(), "amount": float(a.replace(",", ".")),
                                    "currency": c if c in ("USD", "AED") else "USD"})
                except ValueError:
                    continue
            items[key] = lst
    try:
        expected_version = int(form["expected_version"]) if form.get("expected_version") else None
        research.recalc_from_form(u["id"], idea_id, market, changes, items,expected_version)
    except ValueError as exc:
        return go(f"/ideas/{idea_id}?err={str(exc)[:100]}#{market.lower()}")
    return go(f"/ideas/{idea_id}#{market.lower()}")


@app.post("/ideas/{idea_id}/refine")
def idea_refine(request: Request, idea_id: int, text: str = Form(...), csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    with db.db() as conn:
        if not db.get_idea(conn, idea_id, u["id"]):
            raise HTTPException(404)
        jobs.enqueue(conn, u["id"], "refine", idea_id, {"text": text.strip()},
                     dedupe_key=f"web-ref:{idea_id}:{uuid.uuid4().hex[:8]}")
        db.set_idea(conn, idea_id, stage="Уточнение в очереди")
    return go(f"/ideas/{idea_id}")


@app.post("/ideas/{idea_id}/rerun")
def idea_rerun(request: Request, idea_id: int, csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    with db.db() as conn:
        if not db.get_idea(conn, idea_id, u["id"]):
            raise HTTPException(404)
        busy = conn.execute("SELECT 1 FROM jobs WHERE idea_id=? AND status IN ('queued','running')", (idea_id,)).fetchone()
        if not busy:
            jobs.enqueue(conn, u["id"], "research", idea_id,{"force_fresh":True},dedupe_key=f"research:{idea_id}:{int(time.time())}")
            db.set_idea(conn, idea_id, status="queued", stage="Полный повторный разбор в очереди")
    return go(f"/ideas/{idea_id}")


@app.post("/ideas/{idea_id}/deep")
def idea_deep(request: Request, idea_id: int, csrf_token: str = Form(...)):
    """Глубокий разбор одной идеи на Opus (новая версия; правки экономики сохраняются)."""
    u = need_user(request)
    csrf(request, csrf_token)
    with db.db() as conn:
        if not db.get_idea(conn, idea_id, u["id"]):
            raise HTTPException(404)
        busy = conn.execute("SELECT 1 FROM jobs WHERE idea_id=? AND status IN ('queued','running')", (idea_id,)).fetchone()
        if not busy:
            jobs.enqueue(conn, u["id"], "research", idea_id, {"deep": True},
                         dedupe_key=f"deep:{idea_id}:{int(time.time())}")
            db.set_idea(conn, idea_id, status="queued", stage="Глубокий разбор (Opus) в очереди")
    return go(f"/ideas/{idea_id}")


@app.post("/ideas/{idea_id}/flag")
def idea_flag(request: Request, idea_id: int, what: str = Form(...), csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, u["id"])
        if not idea:
            raise HTTPException(404)
        if what == "project":
            db.set_idea(conn, idea_id, is_project=0 if idea["is_project"] else 1)
        elif what == "archive":
            db.set_idea(conn, idea_id, archived=0 if idea["archived"] else 1)
    return go(f"/ideas/{idea_id}")


@app.post("/ideas/{idea_id}/cancel")
def idea_cancel(request: Request, idea_id: int, csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    with db.db() as conn:
        for j in conn.execute("SELECT id FROM jobs WHERE idea_id=? AND status IN ('queued','running')", (idea_id,)).fetchall():
            jobs.request_cancel(conn, j["id"], u["id"])
    return go(f"/ideas/{idea_id}")


@app.post("/ideas/{idea_id}/trends")
async def idea_trends(request: Request, idea_id: int, market: str = Form(...), csrf_token: str = Form(...),
                      file: UploadFile = File(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    if market not in config.MARKETS:
        raise HTTPException(400)
    with db.db() as conn:
        if not db.get_idea(conn, idea_id, u["id"]):
            raise HTTPException(404)
    try:
        content = await file.read()
        if len(content)>500000:
            raise ValueError("Файл должен быть меньше 500 КБ")
        parsed = sources.parse_trends_csv(content)
        names = " ".join(parsed.get("series",[])).lower()
        if (market=="USA" and "united arab emirates" in names) or (market=="UAE" and "united states" in names):
            raise ValueError("Рынок не совпадает со страной в выгрузке")
    except ValueError as e:
        return go(f"/ideas/{idea_id}?err={str(e)[:120]}#evidence")
    with db.db() as conn:
        conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) VALUES(?,?,?,?,?,?,?,?)",
                     (idea_id, "trends_csv", "Google Trends (CSV, ручной импорт)", market, "https://trends.google.com/",
                      db.jdump(parsed), parsed["period"], db.now_iso()))
    db.source_ok("google_trends")
    with db.db() as conn:
        jobs.enqueue(conn,u["id"],"recalculate",idea_id,dedupe_key=f"evidence-recalc:{idea_id}:{uuid.uuid4().hex}")
    return go(f"/ideas/{idea_id}#data")


@app.post("/ideas/{idea_id}/evidence")
def idea_evidence(request: Request, idea_id: int, source: str = Form(...), market: str = Form(...),
                  url: str = Form(""), note: str = Form(...), value: str = Form(""), unit: str = Form(""),
                  period: str = Form(""), scope: str = Form("context"), csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    if market not in config.MARKETS:
        raise HTTPException(400)
    with db.db() as conn:
        if not db.get_idea(conn, idea_id, u["id"]):
            raise HTTPException(404)
        conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) VALUES(?,?,?,?,?,?,?,?)",
                     (idea_id, "manual", source, market, url.strip() or None,
                      db.jdump({"note": note, "value": value, "unit": unit,"scope":scope if scope in ("product","category","context") else "context"}), period or None, db.now_iso()))
        jobs.enqueue(conn,u["id"],"recalculate",idea_id,dedupe_key=f"evidence-recalc:{idea_id}:{uuid.uuid4().hex}")
    return go(f"/ideas/{idea_id}#data")


@app.post("/ideas/{idea_id}/keywords")
async def idea_keywords(request: Request,idea_id: int,market: str = Form(...),period: str = Form(...),
                        csrf_token: str = Form(...),file: UploadFile = File(...)):
    u = need_user(request)
    csrf(request,csrf_token)
    if market not in config.MARKETS:
        raise HTTPException(400)
    with db.db() as conn:
        if not db.get_idea(conn,idea_id,u["id"]):
            raise HTTPException(404)
    data = await file.read()
    if len(data)>500000:
        raise HTTPException(400,"Файл должен быть меньше 500 КБ")
    try:
        parsed = sources.parse_keyword_csv(data)
    except ValueError as exc:
        raise HTTPException(400,str(exc))
    with db.db() as conn:
        conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) VALUES(?,?,?,?,?,?,?,?)",
            (idea_id,"keyword_csv","Google Ads Keyword Planner",market,"https://ads.google.com/home/tools/keyword-planner/",db.jdump(parsed),period,db.now_iso()))
        jobs.enqueue(conn,u["id"],"recalculate",idea_id,dedupe_key=f"keyword-recalc:{idea_id}:{uuid.uuid4().hex}")
    db.source_ok("keyword_planner")
    return go(f"/ideas/{idea_id}#data")


@app.post("/ideas/{idea_id}/feedback")
def idea_feedback(request: Request,idea_id: int,rating: str = Form(...),comment: str = Form(...),
                  remember: str = Form(""),revise: str = Form(""),version: int | None = Form(None),csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request,csrf_token)
    with db.db() as conn:
        if not db.get_idea(conn,idea_id,u["id"]):
            raise HTTPException(404)
        try:
            feedback_id = db.save_feedback(conn,u["id"],idea_id,rating,comment,remember=="on",version)
        except ValueError as exc:
            raise HTTPException(400,str(exc))
        if revise=="on":
            jobs.enqueue(conn,u["id"],"refine",idea_id,{"text":comment},dedupe_key=f"feedback-refine:{feedback_id}")
    return go(f"/ideas/{idea_id}?saved=feedback#feedback")


@app.post("/feedback/{feedback_id}/memory")
def feedback_memory(request: Request,feedback_id: int,remember: int = Form(...),csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request,csrf_token)
    with db.db() as conn:
        row = conn.execute("SELECT idea_id FROM idea_feedback WHERE id=? AND user_id=?",(feedback_id,u["id"])).fetchone()
        if not row:
            raise HTTPException(404)
        conn.execute("UPDATE idea_feedback SET remember=? WHERE id=?",(int(bool(remember)),feedback_id))
    return go(f"/ideas/{row['idea_id']}#feedback")


@app.post("/ideas/{idea_id}/recalculate")
def idea_recalculate(request: Request,idea_id: int,csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request,csrf_token)
    with db.db() as conn:
        if not db.get_idea(conn,idea_id,u["id"]):
            raise HTTPException(404)
        jobs.enqueue(conn,u["id"],"recalculate",idea_id,dedupe_key=f"manual-recalc:{idea_id}:{uuid.uuid4().hex}")
        db.set_idea(conn,idea_id,status="queued",stage="Пересчитываю сохранённые данные без платного поиска")
    return go(f"/ideas/{idea_id}")


@app.post("/ideas/{idea_id}/production")
def idea_production(request: Request,idea_id: int,market: str = Form(...),option: str = Form(...),csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request,csrf_token)
    with db.db() as conn:
        if not db.get_idea(conn,idea_id,u["id"]):
            raise HTTPException(404)
        version = db.latest_version(conn,idea_id)
        if not version or market not in config.MARKETS:
            raise HTTPException(400)
        inputs = db.jload(version["econ_inputs"],{})
        variant = inputs.get("variants",{}).get(market,{}).get(option)
        if not variant:
            raise HTTPException(400,"Вариант ещё не рассчитан")
        inputs["markets"][market] = {**variant,"production_option":option}
        inputs["markets"][market].setdefault("_meta",{})["production_option"] = {"type":"assumption","source":research.OWNER_SET}
        research.finalize(conn,idea_id,u["id"],db.jload(version["report"],{}),inputs,"Выбор производства",
                          f"Выбран {option} для {market}",bool(db.jload(version["report"],{}).get("partial")))
    return go(f"/ideas/{idea_id}#production")


@app.post("/ideas/{idea_id}/plan/{step_key}")
def idea_plan(request: Request,idea_id: int,step_key: str,status: str = Form(...),note: str = Form(""),
              actual_cost: str = Form(""),csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request,csrf_token)
    if status not in ("planned","active","done","stopped"):
        raise HTTPException(400)
    with db.db() as conn:
        if not db.get_idea(conn,idea_id,u["id"]):
            raise HTTPException(404)
        version = db.latest_version(conn,idea_id)
        report = db.jload(version["report"],{}) if version else {}
        keys = {step["key"] for step in report.get("test_plan",{}).get("steps",[])}
        if step_key not in keys:
            raise HTTPException(400)
        try:
            cost = float(actual_cost.replace(",",".")) if actual_cost.strip() else None
        except ValueError:
            raise HTTPException(400,"Проверьте сумму")
        if cost is not None and (not math.isfinite(cost) or cost<0):
            raise HTTPException(400,"Проверьте сумму")
        conn.execute("INSERT INTO project_progress VALUES(?,?,?,?,?,?) ON CONFLICT(idea_id,step_key) DO UPDATE SET "
                     "status=excluded.status,note=excluded.note,actual_cost=excluded.actual_cost,updated_at=excluded.updated_at",
                     (idea_id,step_key,status,note[:3000],cost,db.now_iso()))
    return go(f"/ideas/{idea_id}#plan")


@app.get("/media/{user_id}/{name}")
def media(request: Request, user_id: int, name: str):
    u = need_user(request)
    if u["id"] != user_id or "/" in name or ".." in name:
        raise HTTPException(404)
    p = config.UPLOAD_DIR / str(user_id) / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


# ---------- настройки ----------

TIMEZONES = ["Asia/Dubai", "America/New_York", "Europe/Berlin", "Europe/Moscow", "UTC"]


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: str = ""):
    u = need_user(request)
    with db.db() as conn:
        profile = db.get_profile(conn, u["id"])
        s = db.get_settings(conn, u["id"])
        spend = db.month_spend(conn, u["id"])
        if not u["tg_chat_id"] and not u["tg_bind_code"]:
            conn.execute("UPDATE users SET tg_bind_code=? WHERE id=?", (auth.new_bind_code(), u["id"]))
        user = conn.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
        nxt = next_daily_run(conn, u["id"])
        memories = db.feedback(conn,u["id"],remembered=True)
    return render(request, "settings.html", profile=profile, s=s, fields=db.PROFILE_FIELDS, spend=spend,memories=memories,
                  u=user, saved=saved, timezones=TIMEZONES, next_run=nxt, tg_token=bool(config.TELEGRAM_BOT_TOKEN))


@app.post("/settings/profile")
async def settings_profile(request: Request):
    u = need_user(request)
    form = await request.form()
    csrf(request, form.get("csrf_token"))
    data = {}
    for key, _, kind in db.PROFILE_FIELDS:
        v = str(form.get(key, "")).strip()
        if kind == "number" and v:
            try:
                v = float(v.replace(",", "."))
            except ValueError:
                v = ""
        data[key] = v
    with db.db() as conn:
        db.save_profile(conn, u["id"], data)
    return go("/settings?saved=profile")


@app.post("/settings/general")
async def settings_general(request: Request):
    u = need_user(request)
    form = await request.form()
    csrf(request, form.get("csrf_token"))
    with db.db() as conn:
        s = db.get_settings(conn, u["id"])
        s["daily_enabled"] = form.get("daily_enabled") == "on"
        if form.get("market_priority") in ("USA","UAE","balanced"):
            s["market_priority"] = form.get("market_priority")
        s["evergreen_first"] = form.get("evergreen_first")=="on"
        s["refresh_enabled"] = form.get("refresh_enabled")=="on"
        t = str(form.get("daily_time", "09:00"))
        if re.match(r"^\d{2}:\d{2}$", t):
            s["daily_time"] = t
        tz = str(form.get("timezone", "Asia/Dubai"))
        if tz in TIMEZONES:
            s["timezone"] = tz
        for k in ("monthly_budget_usd", "per_idea_budget_usd"):
            try:
                s[k] = max(0.0, float(str(form.get(k)).replace(",", ".")))
            except (TypeError, ValueError):
                pass
        db.save_settings(conn, u["id"], s)
    return go("/settings?saved=general")


@app.post("/settings/telegram")
def settings_telegram(request: Request, action: str = Form(...), csrf_token: str = Form(...)):
    u = need_user(request)
    csrf(request, csrf_token)
    with db.db() as conn:
        if action == "unlink":
            conn.execute("UPDATE users SET tg_chat_id=NULL, tg_bind_code=? WHERE id=?", (auth.new_bind_code(), u["id"]))
        elif action == "newcode":
            conn.execute("UPDATE users SET tg_bind_code=? WHERE id=?", (auth.new_bind_code(), u["id"]))
    if action == "test":
        notify.simple(u["id"], "✅ Тестовое сообщение: бот подключён и может присылать уведомления.",
                      f"test:{u['id']}:{int(time.time())}")
    return go("/settings?saved=telegram")


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    need_user(request)
    return render(request, "sources.html", passports=sources.all_passports())


@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    u = need_user(request)
    with db.db() as conn:
        errors = conn.execute("SELECT * FROM error_log ORDER BY id DESC LIMIT 100").fetchall()
        usage = conn.execute("SELECT u.*, j.idea_id FROM usage u LEFT JOIN jobs j ON j.id=u.job_id WHERE u.user_id=? "
                             "ORDER BY u.id DESC LIMIT 200", (u["id"],)).fetchall()
        jobs_rows = conn.execute("SELECT * FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 50", (u["id"],)).fetchall()
        spend = db.month_spend(conn, u["id"])
    return render(request, "logs.html", errors=errors, usage=usage, jobs=jobs_rows, spend=spend)


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return HTMLResponse(f"<h3>{exc.status_code}</h3><p>{exc.detail}</p><a href='/today'>На главную</a>",
                        status_code=exc.status_code)

