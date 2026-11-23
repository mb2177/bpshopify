"""Исследовательский процесс: разбор ввода → рынок и конкуренты → производство → экономика → оценка."""
import base64
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from contextlib import nullcontext

from . import config, db, economics, jobs, llm, notify, prompts, scoring, sources, selection, plans

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
    lines.append(f"Приоритет рынка — {profile.get('market_priority','USA')}. Круглогодичные товары в приоритете.")
    return "Профиль владельца:\n" + "\n".join(lines)


def _memory_block(conn, user_id: int, idea_id: int | None = None) -> str:
    rows = list(db.feedback(conn,user_id,remembered=True))
    if idea_id:
        by_id = {row["id"]:row for row in rows}
        by_id.update({row["id"]:row for row in db.feedback(conn,user_id,idea_id)})
        rows = sorted(by_id.values(),key=lambda row:row["id"],reverse=True)
    memories = [{"idea":row["title"],"rating":row["rating"],"reason":row["comment"][:450],
                 "scope":"future" if row["remember"] else "this_idea"} for row in rows[:15]]
    settings = db.get_settings(conn,user_id)
    previous = []
    for idea in conn.execute("SELECT id,title,verdict FROM ideas WHERE user_id=? ORDER BY updated_at DESC LIMIT 8",(user_id,)):
        version = db.latest_version(conn,idea["id"])
        report = db.jload(version["report"],{}) if version else {}
        previous.append({"title":idea["title"],"verdict":idea["verdict"],
                         "main_risk":(report.get("market") or {}).get("main_risk")})
    instructions = []
    if idea_id:
        instructions = [row["change_request"] for row in conn.execute(
            "SELECT change_request FROM idea_versions WHERE idea_id=? AND version>1 AND change_request IS NOT NULL ORDER BY version DESC LIMIT 5",(idea_id,))]
    return "Предпочтения и обратная связь владельца. " + db.jdump({"market_priority":settings["market_priority"],
        "evergreen_first":settings["evergreen_first"],"feedback":memories,"previous_analyses":previous,
        "previous_instructions":instructions})[:14000]


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
        elif r["kind"] in ("keyword_metrics","keyword_csv","census"):
            parts.append(f"- {r['source']}, рынок {r['market']}, период {r['period']}, получено {r['retrieved_at']}: {db.jdump(p)[:10000]}")
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
    job = conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone() if job_id else None
    payload = db.jload(job["payload"],{}) if job else {}
    return llm.Ctx(user_id=user_id, job_id=job_id, per_job_limit=float(s["per_idea_budget_usd"]),
                   monthly_limit=float(s["monthly_budget_usd"]),budget_group=job["budget_group"] if job else None,
                   force_fresh=bool(payload.get("force_fresh")))


# ---------- шаги ----------

def step_parse(ctx, idea) -> dict:
    content = _image_blocks(idea) + [{"type": "text", "text": _idea_block(idea)}]
    return llm.json_call(ctx, "parse", prompts.PARSE, content, model=config.MODEL_FAST, max_tokens=1200)


def step_market(ctx, conn, idea, profile, parsed, instruction: str | None = None, step="market") -> dict:
    text = "\n\n".join(filter(None, [_dates_block(), _profile_block(profile), _memory_block(conn,idea["user_id"],idea["id"]), _idea_block(idea, parsed),
                                     _evidence_block(conn, idea["id"]),
                                     f"Дополнительное указание владельца: {instruction}" if instruction else None]))
    return llm.json_call(ctx, step, prompts.MARKET, text, search=True,max_searches=3,
                         max_tokens=config.MARKET_MAX_TOKENS, model=ctx.research_model)


def step_production(ctx, conn, idea, profile, parsed, market_rep, instruction: str | None = None, step="production") -> dict:
    summary = {k: market_rep.get(k) for k in ("title", "product", "buyer", "situation", "recommended_market",
                                              "market_reasoning")}
    for mk in config.MARKETS:
        mr = (market_rep.get("markets") or {}).get(mk) or {}
        summary[f"{mk}_price_range"] = mr.get("price_range")
        summary[f"{mk}_fulfilment"] = (mr.get("launch_feasibility") or {}).get("fulfilment")
    text = "\n\n".join(filter(None, [_dates_block(), _profile_block(profile), _memory_block(conn,idea["user_id"],idea["id"]),
                                     _evidence_block(conn,idea["id"]),_idea_block(idea, parsed),
                                     "Итоги исследования рынка: " + db.jdump(summary),
                                     f"Дополнительное указание владельца: {instruction}" if instruction else None]))
    return llm.json_call(ctx, step, prompts.PRODUCTION, text, search=True,max_searches=2,
                         max_tokens=config.PRODUCTION_MAX_TOKENS, model=ctx.research_model)


def step_competitors(ctx, idea, report, region: str, market: str | None, step: str) -> dict:
    text = (f"{_dates_block()}\nИдея: {report.get('market', {}).get('title') or idea['title']}. "
            f"Товар: {report.get('market', {}).get('product')}. Покупатель: {report.get('market', {}).get('buyer')}.\n"
            f"Регион/площадка поиска: {region}. Рынок: {market or 'определи по региону'}.")
    return llm.json_call(ctx, step, prompts.COMPETITORS, text, search=True, max_tokens=3500, max_searches=2)


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
                                                     db.jdump(res), res.get("period"), res.get("retrieved_at") or db.now_iso()))
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


def econ_inputs_from_research(base: dict, market_rep: dict, prod: dict | None, include_variants=True) -> dict:
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
            if (ot or "one_time" in pe) and m.get("_meta", {}).get("one_time", {}).get("source") != OWNER_SET:
                m["one_time"] = [{"name": i.get("name", "Разовый расход"), "amount": float(i["amount"]),
                                  "currency": (i.get("currency") or "USD").upper()} for i in ot]
                m.setdefault("_meta", {})["one_time"] = {"type": "estimate", "source": "Исследование производства"}
    if prod and include_variants and prod.get("options"):
        variants = {}
        choices = prod.get("recommended_by_market") or {}
        for mk in config.MARKETS:
            variants[mk] = {}
            for option in prod["options"]:
                if option.get("market") not in (mk,"both"):
                    continue
                key = option.get("key")
                if not key:
                    continue
                clean = deepcopy(inputs)
                clean.pop("variants",None)
                target = clean["markets"][mk]
                if target.get("_meta",{}).get("one_time",{}).get("source") != OWNER_SET:
                    target["one_time"] = []
                data = deepcopy(option.get("economics_inputs") or {})
                data.setdefault("unit_cost",option.get("unit_cost"))
                if "one_time" not in data:
                    startup = option.get("startup_cost") or {}
                    data["one_time"] = [{"name":option.get("name") or key,"amount":startup["value"],
                        "currency":startup.get("currency") or config.MARKET_CURRENCY[mk]}] if startup.get("value") is not None else []
                # A missing quote in this variant must not inherit another variant's unit cost.
                current_choice = inputs["markets"][mk].get("production_option")
                if target.get("_meta",{}).get("unit_cost",{}).get("source") != OWNER_SET or key != current_choice:
                    target["unit_cost"] = None
                    target.get("_meta",{}).pop("unit_cost",None)
                variant = econ_inputs_from_research(clean,market_rep,{"economics_inputs":{mk:data}},False)
                startup_items = data.get("one_time") or []
                unresolved = any(item.get("amount") in (None,"") for item in startup_items)
                variant["markets"][mk]["startup_unknown"] = unresolved or (not startup_items and (option.get("startup_cost") or {}).get("value") is None)
                variants[mk][key] = variant["markets"][mk]
            selected = inputs["markets"][mk].get("production_option") or choices.get(mk) or prod.get("recommended_option")
            if selected in variants[mk]:
                inputs["markets"][mk] = deepcopy(variants[mk][selected])
                inputs["markets"][mk]["production_option"] = selected
            elif not variants[mk] and inputs["markets"][mk].get("_meta",{}).get("one_time",{}).get("source") != OWNER_SET:
                inputs["markets"][mk]["one_time"] = []
                inputs["markets"][mk]["production_review_required"] = True
        inputs["variants"] = variants
    return inputs


# ---------- сборка результата ----------

def finalize(conn, idea_id: int, user_id: int, report: dict, econ_inputs: dict, change_request: str | None,
             change_summary: str | None, partial: bool, preserve_owner=False) -> dict:
    with nullcontext() if conn.in_transaction else db.tx(conn):
        if preserve_owner:
            latest = db.latest_version(conn,idea_id)
            saved = db.jload(latest["econ_inputs"],{}) if latest else {}
            for mk,market in saved.get("markets",{}).items():
                for key,meta in market.get("_meta",{}).items():
                    if meta.get("source")==OWNER_SET:
                        econ_inputs["markets"][mk][key] = deepcopy(market.get(key))
                        econ_inputs["markets"][mk].setdefault("_meta",{})[key] = meta
        profile = db.get_profile(conn, user_id)
        profile["market_priority"] = db.get_settings(conn,user_id)["market_priority"]
        target_income = _f(profile.get("target_income"))
        for mk,m in econ_inputs.get("markets",{}).items():
            if target_income is not None and m.get("_meta",{}).get("target_profit_month",{}).get("source") != OWNER_SET:
                m["target_profit_month"] = economics.convert(target_income,"USD",m["currency"])
        econ = economics.compute(econ_inputs)
        sc = scoring.evaluate(report.get("market") or {}, econ, profile)
        report["schema_version"] = 2
        report["test_plan"] = plans.build(report,econ,profile,sc)
        v = db.save_version(conn, idea_id, report, econ_inputs, econ, sc, change_request, change_summary)
        title = (report.get("market") or {}).get("title")
        fields = dict(score=sc["score"], verdict=sc["verdict_label"], market=sc["market"],
                      status="partial" if partial else "ready", stage=None)
        if title:
            fields["title"] = title[:120]
        db.set_idea(conn, idea_id, **fields)
        return {"version": v, "scoring": sc}


def collect_context(conn, idea_id: int, parsed: dict, report: dict) -> None:
    if config.FAKE_LLM:
        return
    connections = report.setdefault("connections",{})
    connections["google_trends"] = "manual_csv"
    for mk in config.MARKETS:
        try:
            value = sources.keyword_metrics(parsed.get("keywords") or [parsed.get("product") or ""],mk)
            connections[f"keyword_planner_{mk}"] = value.get("status")
            if value.get("status")=="ok":
                conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) VALUES(?,?,?,?,?,?,?,?)",
                    (idea_id,"keyword_metrics","Google Ads Keyword Planner",mk,value["source_url"],db.jdump(value),
                     "Последние доступные месяцы",value["retrieved_at"]))
        except Exception as exc:
            db.source_fail("keyword_planner",type(exc).__name__)
            connections[f"keyword_planner_{mk}"] = "unavailable"
    try:
        value = sources.census_context()
        conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) VALUES(?,?,?,?,?,?,?,?)",
            (idea_id,"census","U.S. Census ACS","USA",value["source_url"],db.jdump(value),value["period"],value["retrieved_at"]))
        connections["census"] = "ok"
    except Exception as exc:
        db.source_fail("census",type(exc).__name__)
        connections["census"] = "unavailable"
    if parsed.get("hs_code"):
        report["comtrade"] = step_comtrade(conn,idea_id,parsed["hs_code"])


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
        profile["market_priority"] = db.get_settings(conn,user_id)["market_priority"]
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
        if parsed.get("critical_question"):
            question = str(parsed["critical_question"])[:700]
            with db.db() as conn:
                db.set_idea(conn,idea_id,status="needs_input",stage=question)
            notify.simple(user_id,f"Нужен один ответ по идее #{idea_id}. {question}\nОтветьте на это сообщение.",
                          f"question:{idea_id}:{job_id}",idea_id)
            return
        with db.db() as conn:
            collect_context(conn,idea_id,parsed,report)
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
            if not report["comtrade"]:
                report["comtrade"] = step_comtrade(conn, idea_id, report["market"].get("hs_code"))
            for market,data in report["comtrade"].items():
                if data.get("error"):
                    report["partial"].append(f"{market} — {data['error']}")
            report["seen_sources"] = ctx.seen_sources
            evidence = conn.execute("SELECT * FROM evidence WHERE idea_id=?",(idea_id,)).fetchall()
            sources.validate_evidence(report,evidence)
            # при повторном разборе правки владельца в экономике сохраняются
            latest = db.latest_version(conn,idea_id)
            base = db.jload(latest["econ_inputs"], None) if latest else None
            base = base or economics.default_inputs(_f(profile.get("hourly_rate")))
            econ_inputs = econ_inputs_from_research(base, report["market"], report["production"])
            summary = ("Глубокий разбор (Opus)" if deep else
                       "Повторный полный разбор" if prev else "Первичный разбор")
            res = finalize(conn, idea_id, user_id, report, econ_inputs, idea["input_text"], summary,
                           partial=bool(report["partial"]),preserve_owner=True)
        notify.idea_ready(user_id, idea_id, res["version"])
        payload = db.jload(job["payload"],{})
        if payload.get("daily_date") and res["scoring"]["verdict"]=="reject":
            enqueue_next_candidate(user_id,payload,job_id)
    except jobs.Cancelled:
        with db.db() as conn:
            db.set_idea(conn, idea_id, status="cancelled", stage=None)
        notify.simple(user_id, f"⏹ Идея #{idea_id}: исследование остановлено. Сохранено то, что успели.",
                      f"cancel:{idea_id}:{job_id}")
        raise
    except llm.BudgetExceeded as e:
        report["seen_sources"] = ctx.seen_sources
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
        previous = db.latest_version(conn,idea_id)
        base = db.jload(previous["econ_inputs"],None) if previous else None
        base = base or economics.default_inputs(_f(profile.get("hourly_rate")))
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
            target_market = res.get("market") if res.get("market") in config.MARKETS else mk
            if target_market:
                target = report.setdefault("market",{}).setdefault("markets",{}).setdefault(target_market,{})
                target["competitors"] = (target.get("competitors") or []) + (res.get("competitors") or [])
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
        if scope in ("competitors","production","market","full"):
            with db.db() as conn:
                sources.validate_evidence(report,conn.execute("SELECT * FROM evidence WHERE idea_id=?",(idea_id,)).fetchall())
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


def recalc_from_form(user_id: int, idea_id: int, market: str, changes: dict, items: dict | None = None,
                     expected_version: int | None = None) -> int | None:
    """Правка экономики на сайте: только код, без запросов к модели и поиску."""
    with db.db() as conn, db.tx(conn):
        idea = db.get_idea(conn, idea_id, user_id)
        ver = db.latest_version(conn, idea_id)
        if not idea or not ver:
            return None
        if expected_version is not None and ver["version"] != expected_version:
            raise ValueError("Разбор уже обновился. Обновите страницу перед правкой")
        report = db.jload(ver["report"], {})
        econ_inputs = db.jload(ver["econ_inputs"], {})
        new_inputs, log = economics.apply_changes(econ_inputs, market, changes)
        if items:
            for key in ("fixed_monthly", "one_time"):
                if key in items:
                    new_inputs["markets"][market][key] = items[key]
                    new_inputs["markets"][market].setdefault("_meta", {})[key] = {"type": "assumption", "source": OWNER_SET}
                    log.append(f"{config.MARKET_LABEL[market]}: обновлён список «{'постоянные' if key == 'fixed_monthly' else 'разовые'} расходы»")
                    if key=="one_time" and items[key]:
                        new_inputs["markets"][market]["startup_unknown"] = False
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
    with db.db() as conn:
        memory = _memory_block(conn,user_id)
        settings = db.get_settings(conn,user_id)
    profile["market_priority"] = settings["market_priority"]
    text = "\n\n".join([_dates_block(), _profile_block(profile),memory,
                        "История идей (не повторять косметически): " + "; ".join(hist or ["пусто"])])
    try:
        res = llm.json_call(ctx, "daily", prompts.DAILY, text, search=True, max_tokens=5000, max_searches=4)
    except llm.BudgetExceeded as e:
        notify.simple(user_id, f"⚠️ Ежедневное исследование {run_date} не выполнено: {e}", f"daily-budget:{run_date}")
        return
    cands = selection.rank(res.get("candidates") or [],settings,hist,ctx.seen_sources)
    if config.FAKE_LLM:
        for candidate in cands:
            candidate["eligible"] = True
    eligible = [candidate for candidate in cands if candidate["eligible"]]
    if settings.get("evergreen_first",True):
        evergreen = [candidate for candidate in eligible if candidate.get("is_seasonal") is False]
        seasonal = [candidate for candidate in eligible if candidate.get("is_seasonal") is True]
        eligible = evergreen + seasonal[:1]
    if not eligible and config.FAKE_LLM:
        eligible = cands
    with db.db() as conn:
        if not eligible:
            reason = res.get("no_good_candidate_reason") or "Ни один кандидат не прошёл проверку."
            conn.execute("INSERT OR IGNORE INTO daily_runs(user_id,run_date,summary,candidates,created_at) VALUES(?,?,?,?,?)",
                         (user_id, run_date, reason, db.jdump(cands), db.now_iso()))
            notify.simple(user_id, f"📭 Ежедневное исследование {run_date}: достойной идеи сегодня нет.\n"
                                   f"Причина: {reason}\nКандидаты: {config.SITE_URL}/today",
                          f"daily-none:{user_id}:{run_date}")
            return
        c = eligible[0]
        txt = f"{c.get('title')}: {c.get('product')} для {c.get('buyer')} — {c.get('situation')}. Почему: {c.get('why')}"
        if res.get("what_changed"):
            txt += f"\nПовтор с новыми данными: {res['what_changed']}"
        evidence_links = [{"url":e["source_url"]} for e in c.get("evidence",[]) if sources.canonical_url(e.get("source_url"))]
        txt += "\nОснования предварительного отбора — " + db.jdump(c)
        idea_id = db.create_idea(conn, user_id, "daily", txt, title=c.get("title"),links=evidence_links)
        conn.execute("INSERT OR IGNORE INTO daily_runs(user_id,run_date,idea_id,summary,candidates,created_at) VALUES(?,?,?,?,?,?)",
                     (user_id, run_date, idea_id, c.get("why"), db.jdump(cands), db.now_iso()))
        jobs.enqueue(conn,user_id,"research",idea_id,{"budget_group":job_id,"daily_date":run_date,
            "remaining_candidates":eligible[1:2],"candidate_attempt":1},dedupe_key=f"research:daily:{user_id}:{run_date}")


def enqueue_next_candidate(user_id: int, payload: dict, job_id: int):
    candidates = payload.get("remaining_candidates") or []
    if not candidates or payload.get("candidate_attempt",1)>=2:
        return
    candidate = candidates[0]
    with db.db() as conn:
        settings = db.get_settings(conn,user_id)
        group = payload.get("budget_group") or job_id
        if db.group_spend(conn,group) >= settings["per_idea_budget_usd"]*0.6:
            return
        text = db.jdump(candidate)
        idea_id = db.create_idea(conn,user_id,"daily",text,title=candidate.get("title"),
            links=[{"url":e["source_url"]} for e in candidate.get("evidence",[]) if e.get("source_url")])
        next_payload = {**payload,"remaining_candidates":[],"candidate_attempt":2,"budget_group":group}
        jobs.enqueue(conn,user_id,"research",idea_id,next_payload,dedupe_key=f"daily-fallback:{user_id}:{payload['daily_date']}")
        conn.execute("UPDATE daily_runs SET idea_id=?,summary=? WHERE user_id=? AND run_date=?",
                     (idea_id,"Первый кандидат отклонён. Проверяется следующий в пределах общего бюджета.",user_id,payload["daily_date"]))


def run_recalculate(job, prepared_report=None) -> None:
    with db.db() as conn:
        idea = db.get_idea(conn,job["idea_id"],job["user_id"])
        version = db.latest_version(conn,job["idea_id"])
        if not idea or not version:
            return
        report = prepared_report if prepared_report is not None else db.jload(version["report"],{})
        inputs = db.jload(version["econ_inputs"],{})
        if not inputs:
            return
        report["rechecked_at"] = db.now_iso()
        for row in conn.execute("SELECT * FROM evidence WHERE idea_id=? AND kind IN ('trends_csv','keyword_csv','keyword_metrics','manual')",
                                (idea["id"],)):
            mk = row["market"]
            if mk not in config.MARKETS:
                continue
            payload = db.jload(row["payload"],{})
            market = report.setdefault("market",{}).setdefault("markets",{}).setdefault(mk,{})
            demand = market.setdefault("demand",{})
            signals = demand.setdefault("signals",[])
            marker = f"evidence:{row['id']}"
            if any(signal.get("evidence_id")==marker for signal in signals):
                continue
            if row["kind"]=="trends_csv":
                claim = "Google Trends — " + db.jdump(payload.get("stats",{}))
            elif row["kind"] in ("keyword_csv","keyword_metrics"):
                claim = "Keyword Planner — " + db.jdump(payload.get("rows") or payload.get("results") or [])[:4000]
            else:
                claim = str(payload.get("note") or "")
            signals.append({"evidence_id":marker,"claim":claim,"source_url":row["url"],"source_name":row["source"],
                            "market":mk,"period":row["period"],"retrieved_at":row["retrieved_at"],
                            "type":"observation","verified":bool(row["url"]),
                            "scope":"product" if row["kind"] in ("trends_csv","keyword_csv","keyword_metrics") else payload.get("scope","context")})
            demand["summary"] = "Сводка обновлена по добавленным данным. " + claim[:1200]
        if (report.get("production") or {}).get("options"):
            inputs = econ_inputs_from_research(inputs,report.get("market") or {},report["production"])
        result = finalize(conn,idea["id"],idea["user_id"],report,inputs,"Пересчёт сохранённых данных",
                          "Обновлены формулы, проверки и план теста",bool(report.get("partial")))
    if db.jload(job["payload"],{}).get("notify"):
        notify.idea_ready(idea["user_id"],idea["id"],result["version"],updated=True)


def run_refresh(job) -> None:
    with db.db() as conn:
        version = db.latest_version(conn,job["idea_id"])
        if not version:
            return
        report = db.jload(version["report"],{})
        settings = db.get_settings(conn,job["user_id"])
        profile = db.get_profile(conn,job["user_id"])
        before = report.get("generated_at") or version["created_at"]
        age = (datetime.now().astimezone()-datetime.fromisoformat(before)).days
        evidence = conn.execute("SELECT id FROM evidence WHERE idea_id=? AND retrieved_at>? LIMIT 1",
                                (job["idea_id"],version["created_at"])).fetchone()
        rows = conn.execute("SELECT * FROM evidence WHERE idea_id=?",(job["idea_id"],)).fetchall()
    old_quality = {mk:scoring.evidence_quality(value) for mk,value in (report.get("market",{}).get("markets") or {}).items()}
    sources.validate_evidence(report,rows)
    new_quality = {mk:scoring.evidence_quality(value) for mk,value in (report.get("market",{}).get("markets") or {}).items()}
    # Verify source snapshots daily. Model research is refreshed separately, within the configured budget.
    if evidence:
        run_recalculate(job,report)
    elif age >= int(settings["refresh_days"]) and profile.get("budget_start") is not None and profile.get("hours_per_week") is not None:
        with db.db() as conn:
            busy = conn.execute("SELECT 1 FROM jobs WHERE idea_id=? AND kind IN ('research','refine') AND status IN ('queued','running')",
                                (job["idea_id"],)).fetchone()
            if not busy:
                jobs.enqueue(conn,job["user_id"],"research",job["idea_id"],{"force_fresh":True},
                             dedupe_key=f"refresh-research:{job['id']}")
    elif report.get("schema_version") != 2 or old_quality != new_quality:
        run_recalculate(job,report)
    db.source_ok(f"idea:{job['idea_id']}:refresh")

