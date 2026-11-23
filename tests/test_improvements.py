"""Regressions for the audit findings and the owner's feedback workflow."""
from copy import deepcopy
from datetime import date,timedelta
from types import SimpleNamespace as NS

from app import config,db,economics,jobs,llm,research,scoring,selection,sources
from app.web.auth import ensure_admin


def owner():
    ensure_admin()
    with db.db() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?",(config.ADMIN_EMAIL,)).fetchone()["id"]


def complete_inputs():
    inputs = economics.default_inputs(15)
    for market in inputs["markets"].values():
        market.update(price=100,unit_cost=10,cac_base=5,orders_base=50)
    return inputs


def confirmed_market():
    return {"scores":{key:{"score":8} for key in ("demand","intent","competition","feasibility")},
            "launch_feasibility":{key:{"status":"ok"} for key in ("payments","legal","fulfilment","product_requirements")},
            "demand":{"signals":[{"type":"observation","verified":True,"scope":"product",
                                   "source_url":f"https://source{i}.example/product","retrieved_at":db.now_iso()}
                                  for i in range(4)]}}


def test_duplicate_sources_and_category_signals_do_not_prove_product_demand():
    market = confirmed_market()
    signal = market["demand"]["signals"][0]
    market["demand"]["signals"] = [signal]*6
    quality = scoring.evidence_quality(market)
    assert quality["observations"]==1 and quality["data_quality"]!="высокое"
    signal["scope"] = "context"
    assert scoring.evidence_quality(market)["demand_observations"]==0


def test_unknown_conditions_never_recommend_testing():
    market = confirmed_market()
    market["launch_feasibility"] = {}
    result = scoring.score_market("USA",market,economics.compute(complete_inputs())["markets"]["USA"],{})
    assert result["verdict"]=="insufficient" and result["score"]<=5


def test_missed_season_is_checked_without_economic_inputs():
    market = confirmed_market()
    market["seasonality"] = {"is_seasonal":True,"next_peak_date":(date.today()-timedelta(days=3)).isoformat()}
    result = scoring.score_market("USA",market,{"status":"insufficient","missing_labels":[]},{})
    assert result["verdict"]=="reject"
    assert any(g["key"]=="season" and g["status"]=="fail_hard" for g in result["gates"])


def test_upfront_inventory_and_defects_are_funded_before_sales():
    inputs = complete_inputs()
    market = inputs["markets"]["USA"]
    market.update(orders_base=100,batch_size=100,unit_cost=10,defect_pct=10,lead_time_days=0)
    result = economics.compute(inputs)["markets"]["USA"]["scenarios"]["base"]
    assert result["cashflow"]["months"][0]["purchase"]>=2000
    assert result["startup_cash_need"]>=result["cashflow"]["months"][0]["purchase"]
    assert result["cashflow"]["months"][0]["inventory_left"]>=0


def test_invalid_costs_cannot_create_profitable_result():
    inputs = complete_inputs()
    inputs["markets"]["USA"]["cac_base"] = -10
    assert economics.compute(inputs)["markets"]["USA"]["status"]=="insufficient"


def test_production_variants_do_not_share_equipment_costs():
    inputs = complete_inputs()
    production = {"recommended_by_market":{"USA":"contractor"},"options":[
        {"key":"own","market":"USA","economics_inputs":{"unit_cost":{"value":4,"currency":"USD"},
            "one_time":[{"name":"Laser","amount":9000,"currency":"USD"}]}},
        {"key":"contractor","market":"USA","economics_inputs":{"unit_cost":{"value":10,"currency":"USD"},
            "one_time":[{"name":"Samples","amount":50,"currency":"USD"}]}}]}
    result = research.econ_inputs_from_research(inputs,{},production)
    assert result["markets"]["USA"]["one_time"][0]["amount"]==50
    computed = economics.compute(result)
    assert computed["variants"]["USA"]["own"]["scenarios"]["base"]["one_time"]==9000
    assert computed["variants"]["USA"]["contractor"]["scenarios"]["base"]["one_time"]==50


def test_feedback_is_remembered_and_duplicate_submission_is_safe():
    user_id = owner()
    with db.db() as conn:
        idea_id = db.create_idea(conn,user_id,"web","Feedback test")
        first = db.save_feedback(conn,user_id,idea_id,"dislike","Prefer scalable evergreen products in USA",True)
        second = db.save_feedback(conn,user_id,idea_id,"dislike","Prefer scalable evergreen products in USA",True)
        assert first==second
        memory = research._memory_block(conn,user_id)
        assert "Prefer scalable evergreen products in USA" in memory
        conn.execute("UPDATE idea_feedback SET remember=0 WHERE id=?",(first,))
        assert "Prefer scalable evergreen products in USA" not in research._memory_block(conn,user_id)


def test_feedback_web_flow_and_csrf():
    from fastapi.testclient import TestClient
    from app.web.main import app
    import re
    user_id = owner()
    with db.db() as conn:
        idea_id = db.create_idea(conn,user_id,"web","UI feedback")
    client = TestClient(app)
    client.post("/login",data={"email":config.ADMIN_EMAIL,"password":"secret123"})
    page = client.get(f"/ideas/{idea_id}").text
    token = re.search(r'name="csrf_token" value="([^"]+)"',page).group(1)
    data = {"csrf_token":token,"rating":"revise","comment":"Prefer repeat purchase and USA","remember":"on","version":0}
    response = client.post(f"/ideas/{idea_id}/feedback",data=data)
    assert response.status_code==200 and "Prefer repeat purchase and USA" in response.text
    assert client.post(f"/ideas/{idea_id}/feedback",data={**data,"csrf_token":"bad"}).status_code==400
    with db.db() as conn:
        assert len(db.feedback(conn,user_id,idea_id))==1
        assert not conn.execute("SELECT 1 FROM jobs WHERE idea_id=?",(idea_id,)).fetchone()


def test_shortlist_prefers_evergreen_usa_with_evidence():
    candidates = [{"title":"Recurring B2B kit","product":"kit","buyer":"business","market_guess":"USA",
                   "is_seasonal":False,"scalability":"Batch manufacturing","feasibility":"ok",
                   "evidence":[{"source_url":"https://market.example/kit"}]},
                  {"title":"Ramadan calendar","market_guess":"UAE","is_seasonal":True,
                   "scalability":"Batch manufacturing","feasibility":"ok",
                   "evidence":[{"source_url":"https://market.example/calendar"}]}]
    ranked = selection.rank(candidates,{"market_priority":"USA","evergreen_first":True},[],
                            [{"url":"https://market.example/kit"},{"url":"https://market.example/calendar"}])
    assert ranked[0]["title"]=="Recurring B2B kit" and ranked[0]["eligible"]


def test_stale_evidence_cannot_qualify_as_current_demand():
    market = confirmed_market()
    for signal in market["demand"]["signals"]:
        signal["retrieved_at"] = (date.today()-timedelta(days=100)).isoformat()
    assert scoring.evidence_quality(market)["observations"]==0


def test_monthly_trends_csv_uses_calendar_year_comparison():
    rows = [f"2023-{month:02d},10" for month in range(1,13)]+["2024-01,20","2024-02,20"]
    result = sources.parse_trends_csv(("Month,product: (United States)\n"+"\n".join(rows)).encode())
    stat = next(iter(result["stats"].values()))
    assert stat["interval"]=="месяцы" and stat["yoy"]==1
    assert result["raw_csv"].startswith("Month")


def test_critical_question_prevents_paid_market_research(monkeypatch):
    user_id = owner()
    with db.db() as conn:
        idea_id = db.create_idea(conn,user_id,"web","Unknown object")
        job_id = jobs.enqueue(conn,user_id,"research",idea_id)
        job = conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
    monkeypatch.setattr(research,"step_parse",lambda *args:{"critical_question":"Which product?"})
    monkeypatch.setattr(research,"step_market",lambda *args:(_ for _ in ()).throw(AssertionError("Market should not run")))
    research.run_research(job)
    with db.db() as conn:
        assert db.get_idea(conn,idea_id,user_id)["status"]=="needs_input"
        assert db.job_spend(conn,job_id)==0


def test_budget_reservation_stops_call_before_spending(monkeypatch):
    user_id = owner()
    with db.db() as conn:
        job_id = jobs.enqueue(conn,user_id,"research")
    monkeypatch.setattr(config,"FAKE_LLM",False)
    client = NS(messages=NS(create=lambda **kwargs:(_ for _ in ()).throw(AssertionError("Must not call API"))))
    monkeypatch.setattr(llm,"_client",lambda:client)
    try:
        llm.call(llm.Ctx(user_id,job_id,per_job_limit=0.01,monthly_limit=100),"bounded","system","unique budget probe",search=True)
    except llm.BudgetExceeded:
        pass
    else:
        raise AssertionError("Expected budget check")
    with db.db() as conn:
        assert db.job_spend(conn,job_id)==0


def test_paused_response_is_resumed_without_repaying_completed_part(monkeypatch):
    user_id = owner()
    with db.db() as conn:
        job_id = jobs.enqueue(conn,user_id,"research")
    monkeypatch.setattr(config,"FAKE_LLM",False)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if len(calls)==2:
            raise ConnectionError("temporary transport error")
        usage = NS(input_tokens=100,output_tokens=20,server_tool_use=NS(web_search_requests=1),
                   cache_read_input_tokens=0,cache_creation_input_tokens=0)
        blocks = [NS(type="web_search_tool_result",content=[NS(url="https://evidence.example/product",title="Product")])]
        if len(calls)==1:
            return NS(stop_reason="pause_turn",content=blocks,usage=usage)
        assert len(kwargs["messages"])==2
        return NS(stop_reason="end_turn",content=[NS(type="text",text='{"ok":true}')],usage=usage)
    monkeypatch.setattr(llm,"_client",lambda:NS(messages=NS(create=create)))
    ctx = llm.Ctx(user_id,job_id,per_job_limit=5,monthly_limit=100)
    try:
        llm.call(ctx,"resume","system","unique resume probe",search=True)
    except ConnectionError:
        pass
    result = llm.call(llm.Ctx(user_id,job_id,per_job_limit=5,monthly_limit=100),"resume","system","unique resume probe",search=True)
    assert result=='{"ok":true}' and len(calls)==3
    cached = jobs.cached_step(job_id,"resume")
    assert cached["sources"][0]["url"]=="https://evidence.example/product"


def test_test_plan_keeps_preparation_and_budget_explicit():
    from app import plans
    plan = plans.build({},economics.compute(complete_inputs()),{"max_test_loss":200,"market_priority":"USA"},
                       {"market":"none"})
    assert plan["phase"]=="preparation" and plan["earliest_sales_date"] is None
    assert plan["budget_limit_usd"]==200 and len(plan["steps"])==6
    assert all(step["owner"] and step["stop_criterion"] and step["deadline"] for step in plan["steps"])


def test_memory_reaches_new_research_prompt(monkeypatch):
    user_id = owner()
    with db.db() as conn:
        first = db.create_idea(conn,user_id,"web","A rejected idea")
        db.save_feedback(conn,user_id,first,"dislike","Avoid heavy fragile shipments",True)
        second = db.create_idea(conn,user_id,"web","A different idea")
        idea = db.get_idea(conn,second,user_id)
        captured = []
        monkeypatch.setattr(llm,"json_call",lambda *args,**kwargs:captured.append(args[3]) or {})
        research.step_market(llm.Ctx(user_id),conn,idea,{}, {})
    assert "Avoid heavy fragile shipments" in captured[0]
    assert "A rejected idea" in captured[0]


def test_keyword_csv_ignores_export_metadata():
    data = b'Keyword Stats 2026\nGenerated report\nKeyword,Currency,Avg. monthly searches\ncustom kits,USD,"1,000"\n'
    parsed = sources.parse_keyword_csv(data)
    assert parsed["rows"][0]["Keyword"]=="custom kits"
    assert parsed["rows"][0]["Avg. monthly searches"]=="1,000"


def test_economic_edit_rejects_stale_version():
    user_id = owner()
    with db.db() as conn:
        idea_id = db.create_idea(conn,user_id,"web","Versioned economics")
        research.finalize(conn,idea_id,user_id,{"market":{"markets":{"USA":confirmed_market()}}},
                          complete_inputs(),None,"initial",False)
    research.recalc_from_form(user_id,idea_id,"USA",{"price":110},expected_version=1)
    try:
        research.recalc_from_form(user_id,idea_id,"USA",{"price":120},expected_version=1)
    except ValueError:
        pass
    else:
        raise AssertionError("Stale financial form must be rejected")
    with db.db() as conn:
        assert db.jload(db.latest_version(conn,idea_id)["econ_inputs"])["markets"]["USA"]["price"]==110


def test_paid_cache_reuse_is_available_after_budget_exhaustion(monkeypatch):
    user_id = owner()
    with db.db() as conn:
        first = jobs.enqueue(conn,user_id,"research")
        second = jobs.enqueue(conn,user_id,"research")
    monkeypatch.setattr(config,"FAKE_LLM",False)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return NS(stop_reason="end_turn",content=[NS(type="text",text='{"cached":true}')],
                  usage=NS(input_tokens=50,output_tokens=20,server_tool_use=None,cache_read_input_tokens=0,cache_creation_input_tokens=0))
    monkeypatch.setattr(llm,"_client",lambda:NS(messages=NS(create=create)))
    first_result = llm.call(llm.Ctx(user_id,first,per_job_limit=5,monthly_limit=100),"reuse","system","unique same request",max_tokens=1000)
    second_result = llm.call(llm.Ctx(user_id,second,per_job_limit=0,monthly_limit=0),"reuse","system","unique same request",max_tokens=1000)
    assert first_result==second_result and len(calls)==1
    with db.db() as conn:
        assert db.job_spend(conn,second)==0


def test_concurrent_budget_reservations_share_monthly_limit():
    user_id = owner()
    with db.db() as conn:
        first = jobs.enqueue(conn,user_id,"research")
        second = jobs.enqueue(conn,user_id,"research")
        spent = db.month_spend(conn,user_id)
    ctx = llm.Ctx(user_id,first,per_job_limit=5,monthly_limit=spent+0.5)
    llm.reserve(ctx,0.4,"regression-reserve-one")
    try:
        try:
            llm.reserve(llm.Ctx(user_id,second,per_job_limit=5,monthly_limit=spent+0.5),0.4,"regression-reserve-two")
        except llm.BudgetExceeded:
            pass
        else:
            raise AssertionError("Concurrent reservations must respect the shared limit")
    finally:
        llm.release("regression-reserve-one")
        llm.release("regression-reserve-two")


def test_source_quote_and_search_provenance_are_required(monkeypatch):
    owner()
    monkeypatch.setattr(config,"FAKE_LLM",False)
    monkeypatch.setattr(sources,"fetch_url_text",lambda url:{"ok":True,"text":"Buy custom product kits with repeat monthly orders for local workshops.","retrieved_at":db.now_iso()})
    report = {"seen_sources":[{"url":"https://real.example/kits"}],"market":{"markets":{"USA":{
        "demand":{"signals":[{"type":"observation","scope":"product","market":"USA",
           "source_url":"https://real.example/kits","quote":"repeat monthly orders for local workshops"},
          {"type":"observation","scope":"product","market":"USA","source_url":"https://invented.example/claim",
           "quote":"repeat monthly orders for local workshops"}]}}}}}
    sources.validate_evidence(report,[])
    signals = report["market"]["markets"]["USA"]["demand"]["signals"]
    assert signals[0]["verified"] and not signals[1]["verified"]
    assert signals[1]["type"]=="estimate"


def test_schema_upgrade_preserves_existing_jobs(monkeypatch):
    import sqlite3,tempfile,re
    from pathlib import Path
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)/"old.db"
        connection = sqlite3.connect(path)
        for table in ("jobs","usage"):
            definition = re.search(r"CREATE TABLE IF NOT EXISTS "+table+r" \([\s\S]*?\n\);",db.SCHEMA).group()
            connection.executescript(definition)
        connection.execute("INSERT INTO jobs(user_id,kind,created_at) VALUES(1,'research','2026-01-01')")
        connection.commit();connection.close()
        monkeypatch.setattr(config,"DB_PATH",path)
        db.init_db();db.init_db()
        with db.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==1
            assert "budget_group" in {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            assert "step" in {row["name"] for row in conn.execute("PRAGMA table_info(usage)")}
