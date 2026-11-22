"""Исследовательский процесс: разбор ввода → рынок и конкуренты → производство → экономика → оценка."""
import base64
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, db, economics, jobs, llm, notify, prompts, scoring, sources

OWNER_SET = "Задано владельцем"


# ---------- контекст для модели ----------

def _dates_block() -> str:
    ny = datetime.now(ZoneInfo(config.MARKET_TZ["USA"]))
    dxb = datetime.now(ZoneInfo(config.MARKET_TZ["UAE"]))
    return (f"Сегодня: {ny:%Y-%m-%d} в США (America/New_York), {dxb:%Y-%m-%d} в ОАЭ (Asia/Dubai). "
            f"Сезонный календарь строй на 30–180 дней вперёд от этих дат.")


def _profile_block(profile: dict) -> str:
    lines = []
    for key, label, _ in db.PROFILE_FIELDS:
        v = profile.get(key)
        lines.append(f"- {label}: {v if v not in (None, '') else 'НЕ УКАЗАНО (не придумывать)'}")
    return "Профиль владельца:\n" + "\n".join(lines)


def _evidence_block(conn, idea_id: int) -> str:
    rows = conn.execute("SELECT * FROM evidence WHERE idea_id=? ORDER BY id", (idea_id,)).fetchall()
    if not rows:
        return ""
    parts = ["Собранные доказательства (дата получения указана):"]
    for r in rows:
        p = db.jload(r["payload"], {})
        if r["kind"] == "trends_csv":
            parts.append(f"- Google Trends CSV, рынок {r['market']}, период {r['period']}, получено {r['retrieved_at'][:10]}: "
                         f"{db.jdump(p.get('stats'))}")
        elif r["kind"] == "comtrade":
            parts.append(f"- UN Comtrade импорт, рынок {r['market']}: {db.jdump(p.get('series'))} (HS {p.get('hs_code')})")
        else:
            parts.append(f"- Ручное наблюдение [{r['source']}], рынок {r['market']}, {r['url'] or ''}: {p.get('note')} "
                         f"{p.get('value') or ''} {p.get('unit') or ''} период {r['period'] or 'не указан'}")
    return "\n".join(parts)


def _idea_block(idea, parsed: dict | None = None) -> str:
    links = db.jload(idea["input_links"], [])
    s = [f"Идея владельца (номер #{idea['id']}): {idea['input_text'] or ''}"]
    if idea["region_hint"]:
        s.append(f"Регион/площадка для поиска конкурентов, указанные владельцем: {idea['region_hint']}")
    if parsed:
        s.append("Разбор ввода: " + db.jdump({k: parsed.get(k) for k in
                 ("product", "buyer", "situation", "photo_description", "ambiguities")}))
    for l in links:
        if l.get("ok"):
            s.append(f"<untrusted_page url=\"{l['url']}\">\n{l.get('title','')}\n{l.get('text','')[:8000]}\n</untrusted_page>")
        else:
            s.append(f"Ссылка {l.get('url')}: {l.get('error')}")
    return "\n".join(s)


def _image_blocks(idea) -> list:
    blocks = []
    for m in db.jload(idea["input_media"], [])[:4]:
        p = Path(m.get("path", ""))
        if m.get("type") == "photo" and p.exists():
            blocks.append({"type": "image", "source": {"type": "base64", "media_type": m.get("mime", "image/jpeg"),
                                                      "data": base64.b64encode(p.read_bytes()).decode()}})
    return blocks


def make_ctx(conn, user_id: int, job_id: int | None) -> llm.Ctx:
    s = db.get_settings(conn, user_id)
    return llm.Ctx(user_id=user_id, job_id=job_id, per_job_limit=float(s["per_idea_budget_usd"]),
                   monthly_limit=float(s["monthly_budget_usd"]))


# ---------- шаги ----------

def step_parse(ctx, idea) -> dict:
    content = _image_blocks(idea) + [{"type": "text", "text": _idea_block(idea)}]
    return llm.json_call(ctx, "parse", prompts.PARSE, content, model=config.MODEL_FAST, max_tokens=2000)


def step_market(ctx, conn, idea, profile, parsed, instruction: str | None = None, step="market") -> dict:
    text = "\n\n".join(filter(None, [_dates_block(), _profile_block(profile), _idea_block(idea, parsed),
                                     _evidence_block(conn, idea["id"]),
                                     f"Дополнительное указание владельца: {instruction}" if instruction else None]))
    return llm.json_call(ctx, step, prompts.MARKET, text, search=True, max_tokens=24000, model=ctx.research_model)


def step_production(ctx, conn, idea, profile, parsed, market_rep, instruction: str | None = None, step="production") -> dict:
    summary = {k: market_rep.get(k) for k in ("title", "product", "buyer", "situation", "recommended_market",
                                              "market_reasoning")}
    for mk in config.MARKETS:
        mr = (market_rep.get("markets") or {}).get(mk) or {}
        summary[f"{mk}_price_range"] = mr.get("price_range")
        summary[f"{mk}_fulfilment"] = (mr.get("launch_feasibility") or {}).get("fulfilment")
    text = "\n\n".join(filter(None, [_dates_block(), _profile_block(profile), _idea_block(idea, parsed),
                                     "Итоги исследования рынка: " + db.jdump(summary),
                                     f"Дополнительное указание владельца: {instruction}" if instruction else None]))
    return llm.json_call(ctx, step, prompts.PRODUCTION, text, search=True, max_tokens=20000, model=ctx.research_model)


def step_competitors(ctx, idea, report, region: str, market: str | None, step: str) -> dict:
    text = (f"{_dates_block()}\nИдея: {report.get('market', {}).get('title') or idea['title']}. "
            f"Товар: {report.get('market', {}).get('product')}. Покупатель: {report.get('market', {}).get('buyer')}.\n"
            f"Регион/площадка поиска: {region}. Рынок: {market or 'определи по региону'}.")
    return llm.json_call(ctx, step, prompts.COMPETITORS, text, search=True, max_tokens=8000, max_searches=8)


def step_comtrade(conn, idea_id: int, hs_code: str | None) -> dict:
    out = {}
    if not hs_code or config.FAKE_LLM:
        return out
    for mk in config.MARKETS:
        try:
            res = sources.comtrade_imports(hs_code, mk)
            out[mk] = res
            conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) "
                         "VALUES(?,?,?,?,?,?,?,?)", (idea_id, "comtrade", "UN Comtrade", mk, res["source_url"],
                                                     db.jdump(res), "2021–2023", db.now_iso()))
        except Exception as e:
            db.source_fail("un_comtrade", str(e))
            out[mk] = {"error": f"UN Comtrade недоступен: {type(e).__name__}"}
    return out


# ---------- экономика из исследования ----------

def _set(m: dict, field: str, value, meta: dict, overwrite_owner=False):
    if value in (None, ""):
        return
    if not overwrite_owner and (m.get("_meta", {}).get(field, {}).get("source") == OWNER_SET):
        return
    m[field] = value
    m.setdefault("_meta", {})[field] = meta


def _money(obj: dict | None, target_cur: str):
    if not isinstance(obj, dict) or obj.get("value") in (None, ""):
        return None
    try:
        v = float(obj["value"])
    except (TypeError, ValueError):
        return None
    cur = (obj.get("currency") or target_cur).upper()
    if cur not in ("USD", "AED"):
        return None
    return round(economics.convert(v, cur, target_cur), 2)


def _meta(obj, note=""):
    if not isinstance(obj, dict):
        return {"type": "assumption", "source": note}
    return {"type": obj.get("type") or "estimate", "source": obj.get("source_url") or obj.get("basis") or note}


def econ_inputs_from_research(base: dict, market_rep: dict, prod: dict | None) -> dict:
    inputs = deepcopy(base)
    for mk in config.MARKETS:
        m = inputs["markets"][mk]
        cur = m["currency"]
        mr = (market_rep.get("markets") or {}).get(mk) or {}
        h = mr.get("economics_hints") or {}
        _set(m, "price", _money(h.get("price"), cur), _meta(h.get("price")))
        _set(m, "shipping_charged", _money(h.get("shipping_charged"), cur), _meta(h.get("shipping_charged")))
        _set(m, "outbound_shipping", _money(h.get("outbound_shipping"), cur), _meta(h.get("outbound_shipping")))
        rr = h.get("return_rate_pct") or {}
        if isinstance(rr, dict) and rr.get("value") not in (None, ""):
            _set(m, "return_rate", float(rr["value"]), _meta(rr))
        ob = h.get("orders_month_base") or {}
        if isinstance(ob, dict) and ob.get("value") not in (None, ""):
            base_o = float(ob["value"])
            meta = {"type": "assumption", "source": ob.get("basis") or "оценка исследования"}
            _set(m, "orders_base", base_o, meta)
            _set(m, "orders_cons", round(base_o * 0.5), {"type": "assumption", "source": "50% от базового"})
            _set(m, "orders_opt", round(base_o * 1.5), {"type": "assumption", "source": "150% от базового"})
        cac = (mr.get("acquisition") or {}).get("cac_estimate") or {}
        lo, hi = _money({"value": cac.get("low"), "currency": cac.get("currency")}, cur), \
            _money({"value": cac.get("high"), "currency": cac.get("currency")}, cur)
        if lo is not None or hi is not None:
            lo = lo if lo is not None else hi
            hi = hi if hi is not None else lo
            meta = {"type": cac.get("type") or "assumption", "source": cac.get("basis") or "оценка CAC — не измерено"}
            _set(m, "cac_opt", lo, meta)
            _set(m, "cac_cons", hi, meta)
            _set(m, "cac_base", round((lo + hi) / 2, 2), meta)
        if prod:
            pe = (prod.get("economics_inputs") or {}).get(mk) or {}
            uc = pe.get("unit_cost")
            if isinstance(uc, dict) and uc.get("value") not in (None, ""):
                ccy = (uc.get("currency") or "USD").upper()
                if ccy in ("USD", "AED"):
                    _set(m, "unit_cost", float(uc["value"]), _meta(uc))
                    _set(m, "unit_cost_currency", ccy, _meta(uc))
            for f in ("duty_pct", "labor_minutes", "batch_size", "lead_time_days"):
                o = pe.get(f)
                if isinstance(o, dict) and o.get("value") not in (None, ""):
                    try:
                        _set(m, f, float(o["value"]), _meta(o))
                    except (TypeError, ValueError):
                        pass
            for f in ("inbound_per_unit", "packaging"):
                _set(m, f, _money(pe.get(f), cur), _meta(pe.get(f)))
            ot = [i for i in (pe.get("one_time") or []) if isinstance(i, dict) and i.get("amount") not in (None, "")
                  and (i.get("currency") or "USD").upper() in ("USD", "AED")]
            if ot and m.get("_meta", {}).get("one_time", {}).get("source") != OWNER_SET:
                m["one_time"] = [{"name": i.get("name", "Разовый расход"), "amount": float(i["amount"]),
                                  "currency": (i.get("currency") or "USD").upper()} for i in ot]
                m.setdefault("_meta", {})["one_time"] = {"type": "estimate", "source": "Исследование производства"}
    return inputs


# ---------- сборка результата ----------

def finalize(conn, idea_id: int, user_id: int, report: dict, econ_inputs: dict, change_request: str | None,
             change_summary: str | None, partial: bool) -> dict:
    profile = db.get_profile(conn, user_id)
    econ = economics.compute(econ_inputs)
    sc = scoring.evaluate(report.get("market") or {}, econ, profile)
    v = db.save_version(conn, idea_id, report, econ_inputs, econ, sc, change_request, change_summary)
    title = (report.get("market") or {}).get("title")
    fields = dict(score=sc["score"], verdict=sc["verdict_label"], market=sc["market"],
                  status="partial" if partial else "ready", stage=None)
    if title:
        fields["title"] = title[:120]
    db.set_idea(conn, idea_id, **fields)
    return {"version": v, "scoring": sc}


def _new_report(parsed: dict | None) -> dict:
    return {"input": parsed or {}, "market": {}, "production": {}, "comtrade": {}, "competitor_searches": [],
            "seen_sources": [], "partial": [], "generated_at": db.now_iso(), "demo": config.FAKE_LLM}


def run_research(job) -> None:
    idea_id, user_id, job_id = job["idea_id"], job["user_id"], job["id"]
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, user_id)
        if not idea:
            return
        profile = db.get_profile(conn, user_id)
        ctx = make_ctx(conn, user_id, job_id)
        prev = db.latest_version(conn, idea_id)
        deep = bool(db.jload(job["payload"], {}).get("deep"))
        if deep:
            ctx.research_model = config.MODEL_DEEP
            ctx.per_job_limit *= config.DEEP_BUDGET_MULT
        db.set_idea(conn, idea_id, status="researching",
                    stage="Глубокий разбор (Opus): разбираю ввод" if deep else "Разбираю ввод")
    report = _new_report(None)
    report["model"] = ctx.research_model or config.MODEL_RESEARCH
    report["deep"] = deep
    links = db.jload(idea["input_links"], [])
    if links and any("ok" not in l for l in links):
        links = [l if "ok" in l else sources.fetch_url_text(l["url"]) for l in links[:5]]
        with db.db() as conn:
            db.set_idea(conn, idea_id, input_links=db.jdump(links))
            idea = db.get_idea(conn, idea_id, user_id)
    try:
        parsed = step_parse(ctx, idea)
        report["input"] = parsed
        with db.db() as conn:
            upd = {"stage": "Исследую спрос и конкурентов: США (USA) и ОАЭ (UAE)"}
            if parsed.get("title"):
                upd["title"] = parsed["title"][:120]
            if parsed.get("region_hint") and not idea["region_hint"]:
                upd["region_hint"] = parsed["region_hint"]
            db.set_idea(conn, idea_id, **upd)
            idea = db.get_idea(conn, idea_id, user_id)
            report["market"] = step_market(ctx, conn, idea, profile, parsed)
        with db.db() as conn:
            db.set_idea(conn, idea_id, stage="Подбираю производство и подрядчиков")
        try:
            with db.db() as conn:
                report["production"] = step_production(ctx, conn, idea, profile, parsed, report["market"])
        except (llm.BudgetExceeded, jobs.Cancelled):
            raise
        except Exception as e:
            report["partial"].append(f"Производство не исследовано: {type(e).__name__}: {e}")
        with db.db() as conn:
            db.set_idea(conn, idea_id, stage="Считаю экономику")
            report["comtrade"] = step_comtrade(conn, idea_id, report["market"].get("hs_code"))
            report["seen_sources"] = ctx.seen_sources
            # при повторном разборе правки владельца в экономике сохраняются
            base = db.jload(prev["econ_inputs"], None) if prev else None
            base = base or economics.default_inputs(_f(profile.get("hourly_rate")))
            econ_inputs = econ_inputs_from_research(base, report["market"], report["production"])
            summary = ("Глубокий разбор (Opus)" if deep else
                       "Повторный полный разбор" if prev else "Первичный разбор")
            res = finalize(conn, idea_id, user_id, report, econ_inputs, idea["input_text"], summary,
                           partial=bool(report["partial"]))
        notify.idea_ready(user_id, idea_id, res["version"])
    except jobs.Cancelled:
        with db.db() as conn:
            db.set_idea(conn, idea_id, status="cancelled", stage=None)
        notify.simple(user_id, f"⏹ Идея #{idea_id}: исследование остановлено. Сохранено то, что успели.",
                      f"cancel:{idea_id}:{job_id}")
        raise
    except llm.BudgetExceeded as e:
        _save_partial(idea_id, user_id, report, f"Остановлено по бюджету: {e}")
        notify.simple(user_id, f"⚠️ Идея #{idea_id}: {e}\nСохранена частичная версия. Поднимите лимит в настройках "
                               f"и нажмите «Обновить разбор» на сайте.", f"budget:{idea_id}:{job_id}")
    except llm.LLMUnavailable as e:
        with db.db() as conn:
            db.set_idea(conn, idea_id, status="failed", stage=str(e))
        notify.simple(user_id, f"⚠️ Идея #{idea_id} сохранена, но исследование ожидает подключения: {e}",
                      f"unavail:{idea_id}:{job_id}")


def _save_partial(idea_id, user_id, report, reason):
    report["partial"].append(reason)
    with db.db() as conn:
        profile = db.get_profile(conn, user_id)
        base = economics.default_inputs(_f(profile.get("hourly_rate")))
        econ_inputs = econ_inputs_from_research(base, report.get("market") or {}, report.get("production"))
        finalize(conn, idea_id, user_id, report, econ_inputs, None, reason, partial=True)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- уточнения ----------

def run_refine(job) -> None:
    p = db.jload(job["payload"], {})
    idea_id, user_id, job_id = job["idea_id"], job["user_id"], job["id"]
    text = p.get("text", "")
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, user_id)
        ver = db.latest_version(conn, idea_id)
        if not idea:
            return
        ctx = make_ctx(conn, user_id, job_id)
        profile = db.get_profile(conn, user_id)
    if not ver:
        # разбора ещё нет — дополняем ввод и запускаем полное исследование
        with db.db() as conn:
            db.set_idea(conn, idea_id, input_text=(idea["input_text"] or "") + "\nДополнение: " + text)
            jobs.enqueue(conn, user_id, "research", idea_id, dedupe_key=f"research:{idea_id}:refine{job_id}")
        return
    report = db.jload(ver["report"], {})
    econ_inputs = db.jload(ver["econ_inputs"], {})
    route = llm.json_call(ctx, "route", prompts.REFINE,
                          f"Идея: {idea['title']}\nКраткий разбор: рынок {idea['market']}, вывод {idea['verdict']}.\n"
                          f"Текущие параметры экономики (по рынкам): "
                          f"{db.jdump({mk: {k: v for k, v in m.items() if not k.startswith('_')} for mk, m in econ_inputs.get('markets', {}).items()})}\n"
                          f"Сообщение владельца: {text}", model=config.MODEL_FAST, max_tokens=1500)
    scope = route.get("scope", "full")
    instr = route.get("instruction") or text
    mk = route.get("market") if route.get("market") in config.MARKETS else None
    summary = route.get("summary") or text
    try:
        with db.db() as conn:
            db.set_idea(conn, idea_id, status="researching", stage=f"Уточнение: {summary[:80]}")
        if scope == "question":
            ans = llm.call(ctx, "answer", prompts.ANSWER,
                           f"Разбор: {db.jdump({'market': report.get('market'), 'production': report.get('production'), 'scoring': db.jload(ver['scoring'])})[:60000]}\n\nВопрос: {text}",
                           model=config.MODEL_FAST, max_tokens=1200)
            with db.db() as conn:
                conn.execute("INSERT INTO qa(idea_id,question,answer,created_at) VALUES(?,?,?,?)",
                             (idea_id, text, ans, db.now_iso()))
                db.set_idea(conn, idea_id, status=idea["status"] if idea["status"] in ("ready", "partial") else "ready",
                            stage=None)
            notify.answer(user_id, idea_id, ans, f"qa:{job_id}")
            return
        log = []
        if scope == "economics":
            econ_inputs, log = economics.apply_changes(econ_inputs, mk, route.get("changes") or {})
            if not log:
                log = ["Параметры не изменились — проверьте формулировку"]
        elif scope == "competitors":
            region = route.get("region") or idea["region_hint"] or instr
            res = step_competitors(ctx, idea, report, region, mk, "competitors")
            res["searched_at"] = db.now_iso()
            report.setdefault("competitor_searches", []).append(res)
            log = [f"Найдены конкуренты: {region}"]
        elif scope == "production":
            with db.db() as conn:
                report["production"] = step_production(ctx, conn, idea, profile, report.get("input"),
                                                       report.get("market") or {}, instr, step="production_r")
            econ_inputs = econ_inputs_from_research(econ_inputs, {}, report["production"])
            log = ["Пересмотрены варианты производства"]
        else:  # market / full
            with db.db() as conn:
                report["market"] = step_market(ctx, conn, idea, profile, report.get("input"), instr, step="market_r")
                if scope == "full":
                    report["production"] = step_production(ctx, conn, idea, profile, report.get("input"),
                                                           report["market"], instr, step="production_r")
            econ_inputs = econ_inputs_from_research(econ_inputs, report["market"],
                                                    report.get("production") if scope == "full" else None)
            log = ["Обновлено исследование рынка" + (" и производства" if scope == "full" else "")]
        report["seen_sources"] = (report.get("seen_sources") or []) + ctx.seen_sources
        with db.db() as conn:
            res = finalize(conn, idea_id, user_id, report, econ_inputs, text, f"{summary}. " + "; ".join(log),
                           partial=bool(report.get("partial")))
        notify.idea_ready(user_id, idea_id, res["version"], updated=True)
    except jobs.Cancelled:
        with db.db() as conn:
            db.set_idea(conn, idea_id, status="ready", stage=None)
        raise
    except llm.BudgetExceeded as e:
        with db.db() as conn:
            db.set_idea(conn, idea_id, status="partial", stage=None)
        notify.simple(user_id, f"⚠️ Идея #{idea_id}: уточнение остановлено — {e}", f"budget:{idea_id}:{job_id}")


def recalc_from_form(user_id: int, idea_id: int, market: str, changes: dict, items: dict | None = None) -> int | None:
    """Правка экономики на сайте: только код, без запросов к модели и поиску."""
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, user_id)
        ver = db.latest_version(conn, idea_id)
        if not idea or not ver:
            return None
        report = db.jload(ver["report"], {})
        econ_inputs = db.jload(ver["econ_inputs"], {})
        new_inputs, log = economics.apply_changes(econ_inputs, market, changes)
        if items:
            for key in ("fixed_monthly", "one_time"):
                if key in items:
                    new_inputs["markets"][market][key] = items[key]
                    new_inputs["markets"][market].setdefault("_meta", {})[key] = {"type": "assumption", "source": OWNER_SET}
                    log.append(f"{config.MARKET_LABEL[market]}: обновлён список «{'постоянные' if key == 'fixed_monthly' else 'разовые'} расходы»")
        if not log:
            return ver["version"]
        res = finalize(conn, idea_id, user_id, report, new_inputs, "Правка на сайте", "; ".join(log),
                       partial=idea["status"] == "partial")
        return res["version"]


# ---------- ежедневный поиск ----------

def run_daily(job) -> None:
    user_id, job_id = job["user_id"], job["id"]
    p = db.jload(job["payload"], {})
    run_date = p.get("date")
    with db.db() as conn:
        profile = db.get_profile(conn, user_id)
        ctx = make_ctx(conn, user_id, job_id)
        hist = [r["title"] for r in conn.execute(
            "SELECT title FROM ideas WHERE user_id=? ORDER BY id DESC LIMIT 60", (user_id,))]
        existing = conn.execute("SELECT * FROM daily_runs WHERE user_id=? AND run_date=?", (user_id, run_date)).fetchone()
    if existing:
        return
    text = "\n\n".join([_dates_block(), _profile_block(profile),
                        "История идей (не повторять косметически): " + "; ".join(hist or ["пусто"])])
    try:
        res = llm.json_call(ctx, "daily", prompts.DAILY, text, search=True, max_tokens=12000, max_searches=10)
    except llm.BudgetExceeded as e:
        notify.simple(user_id, f"⚠️ Ежедневное исследование {run_date} не выполнено: {e}", f"daily-budget:{run_date}")
        return
    cands = res.get("candidates") or []
    idx = res.get("chosen_index")
    with db.db() as conn:
        if idx is None or not isinstance(idx, int) or idx >= len(cands):
            reason = res.get("no_good_candidate_reason") or "Ни один кандидат не прошёл проверку."
            conn.execute("INSERT OR IGNORE INTO daily_runs(user_id,run_date,summary,candidates,created_at) VALUES(?,?,?,?,?)",
                         (user_id, run_date, reason, db.jdump(cands), db.now_iso()))
            notify.simple(user_id, f"📭 Ежедневное исследование {run_date}: достойной идеи сегодня нет.\n"
                                   f"Причина: {reason}\nКандидаты: {config.SITE_URL}/today",
                          f"daily-none:{user_id}:{run_date}")
            return
        c = cands[idx]
        txt = f"{c.get('title')}: {c.get('product')} для {c.get('buyer')} — {c.get('situation')}. Почему: {c.get('why')}"
        if res.get("what_changed"):
            txt += f"\nПовтор с новыми данными: {res['what_changed']}"
        idea_id = db.create_idea(conn, user_id, "daily", txt, title=c.get("title"))
        conn.execute("INSERT OR IGNORE INTO daily_runs(user_id,run_date,idea_id,summary,candidates,created_at) VALUES(?,?,?,?,?,?)",
                     (user_id, run_date, idea_id, c.get("why"), db.jdump(cands), db.now_iso()))
        jobs.enqueue(conn, user_id, "research", idea_id, dedupe_key=f"research:daily:{user_id}:{run_date}")
