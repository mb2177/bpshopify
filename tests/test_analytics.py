"""Analytical response contracts, accounting, evidence scope, and durable feedback behavior."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import analytics, basis, config, credentials, db, economics, jobs, llm, memory, research, scoring
from app.web.auth import ensure_admin, hash_password


@pytest.fixture
def owner(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"DB_PATH",tmp_path/"analytics.db")
    for name in ("DATAFORSEO_LOGIN","DATAFORSEO_PASSWORD","SEARCHAPI_API_KEY","KEEPA_API_KEY","CENSUS_API_KEY"):
        monkeypatch.setattr(config,name,"")
    ensure_admin()
    with db.db() as conn:
        return conn.execute("SELECT id FROM users WHERE email=?",(config.ADMIN_EMAIL,)).fetchone()[0]


def context(user_id):
    with db.db() as conn:
        job_id = jobs.enqueue(conn,user_id,"analytics")
    return llm.Ctx(user_id,job_id,per_job_limit=5,monthly_limit=100)


def connect(monkeypatch,provider):
    for name,_ in credentials.FIELDS[provider]:
        monkeypatch.setattr(config,name,"fixture-private-credential")


def dfs(result,cost=.011):
    return httpx.Response(200,json={"status_code":20000,"cost":cost,"tasks":[{"status_code":20000,"result":result}]})


def measured(keyword="desk mat",market="USA",volume=1000):
    return {"provider":"dataforseo","kind":"search_volume_api","engine":"google","market":market,
        "query":keyword,"keywords":[keyword],"status":"ok","retrieved_at":db.now_iso(),
        "period":str(datetime.now(timezone.utc).year),"source_url":"https://dataforseo.com/apis/keywords-data-api",
        "metrics":[{"name":keyword,"value":volume,"unit":"запросов/месяц"}],
        "rows":[{"keyword":keyword,"search_volume":volume}]}


def save_measurement(user_id,bundle):
    with db.db() as conn:
        idea_id = db.create_idea(conn,user_id,"web","Product")
        analytics.save_bundle(conn,idea_id,bundle)
        return conn.execute("SELECT * FROM evidence WHERE idea_id=?",(idea_id,)).fetchall()


def test_volume_contract_keeps_zero_and_country(owner,monkeypatch):
    connect(monkeypatch,"dataforseo")
    seen = []
    def send(user,provider,endpoint,payload):
        seen.append((provider,endpoint,payload))
        return dfs([{"keyword":"desk mat","location_code":2840,"search_volume":0,"cpc":0,
                     "monthly_searches":[{"year":2026,"month":10,"search_volume":0}]}])
    monkeypatch.setattr(analytics,"_send",send)
    result = analytics.volumes(context(owner),["desk mat"],"USA","google")
    assert result["status"]=="ok" and result["rows"][0]["search_volume"]==0
    assert result["rows"][0]["cpc"]==0 and result["market"]=="USA"
    assert seen[0][1]=="keywords_data/google_ads/search_volume/live" and seen[0][2]["location_code"]==2840


def test_trends_uses_same_calendar_period_and_excludes_missing(owner,monkeypatch):
    connect(monkeypatch,"dataforseo")
    today = datetime.now(timezone.utc).date()
    points = [{"date_from":(today-timedelta(days=days)).isoformat(),"values":[value],"missing_data":False}
              for days,value in [(400,10),(370,10),(80,20),(0,20)]]
    points.append({"date_from":today.isoformat(),"values":[100],"missing_data":True})
    monkeypatch.setattr(analytics,"_send",lambda *args:dfs([{"location_code":2840,"items":[
        {"type":"google_trends_graph","keywords":["desk mat"],"data":points}]}]))
    result = analytics.trends(context(owner),["desk mat"],"USA")
    assert result["stats"]["desk mat"]["index_90d"]==20
    assert result["stats"]["desk mat"]["yoy_pct"]==100 and len(result["series"]["desk mat"])==4


def test_wrong_country_is_rejected(owner,monkeypatch):
    connect(monkeypatch,"dataforseo")
    monkeypatch.setattr(analytics,"_send",lambda *args:dfs([{"keyword":"desk mat","location_code":2784,"search_volume":1000}]))
    with pytest.raises(analytics.AnalyticsError,match="Страна"):
        analytics.volumes(context(owner),["desk mat"],"USA","google")


def test_api_cache_does_not_repeat_cost_or_request(owner,monkeypatch):
    connect(monkeypatch,"dataforseo")
    calls = []
    def send(*args):
        calls.append(1)
        return dfs([{"keyword":"desk mat","location_code":2840,"search_volume":1000}])
    monkeypatch.setattr(analytics,"_send",send)
    ctx = context(owner)
    analytics.volumes(ctx,["desk mat"],"USA","google")
    result = analytics.volumes(ctx,["desk mat"],"USA","google")
    with db.db() as conn:
        assert db.job_spend(conn,ctx.job_id)==.011
        assert conn.execute("SELECT status FROM analytics_calls ORDER BY id DESC LIMIT 1").fetchone()[0]=="cache"
    assert result["cached"] and len(calls)==1


def test_budget_blocks_before_network(owner,monkeypatch):
    connect(monkeypatch,"dataforseo")
    monkeypatch.setattr(analytics,"_send",lambda *args:pytest.fail("Must not request"))
    ctx = context(owner)
    ctx.per_job_limit=.01
    with pytest.raises(llm.BudgetExceeded):
        analytics.volumes(ctx,["desk mat"],"USA","google")
    with db.db() as conn:
        assert db.job_spend(conn,ctx.job_id)==0


def test_monthly_request_limit_blocks_subscription_usage(owner,monkeypatch):
    connect(monkeypatch,"searchapi")
    with db.db() as conn:
        settings = db.get_settings(conn,owner)
        settings["analytics_calls_per_month"]=0
        db.save_settings(conn,owner,settings)
    monkeypatch.setattr(analytics,"_send",lambda *args:pytest.fail("Must not request"))
    with pytest.raises(llm.BudgetExceeded):
        analytics.ads(context(owner),"desk mat","USA")


def test_failed_request_is_not_automatically_repaid(owner,monkeypatch):
    connect(monkeypatch,"dataforseo")
    calls=[]
    def send(*args):
        calls.append(1)
        return httpx.Response(429,json={"error":"rate limit"})
    monkeypatch.setattr(analytics,"_send",send)
    ctx = context(owner)
    for _ in range(2):
        with pytest.raises(analytics.AnalyticsError):
            analytics.volumes(ctx,["desk mat"],"USA","google")
    assert len(calls)==1


def test_model_cannot_invent_analytical_measurements(owner):
    report={"market":{"markets":{"USA":{"analytics_status":"ok","analytics_count":99}}}}
    analytics.attach(report,[])
    assert report["market"]["markets"]["USA"]["analytics_status"]=="missing" and not report["analytics"]


def test_old_product_data_does_not_validate_new_product(owner):
    rows = save_measurement(owner,measured("desk mat"))
    report={"analytics_queries":["wedding shirts"]}
    analytics.attach(report,rows)
    assert not report["analytics"] and report["market"]["markets"]["USA"]["analytics_status"]=="missing"


def test_actual_volume_drives_score_without_summing_keywords(owner):
    first=measured("desk mat",volume=1000)
    second=measured("custom desk mat",volume=1000)
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"web","Desk mat")
        analytics.save_bundle(conn,idea_id,first)
        analytics.save_bundle(conn,idea_id,second)
        rows=conn.execute("SELECT * FROM evidence WHERE idea_id=?",(idea_id,)).fetchall()
    report={"analytics_queries":["desk mat","custom desk mat"]}
    analytics.attach(report,rows)
    demand=report["market"]["markets"]["USA"]["scores"]["demand"]
    assert demand["score"]==6.0 and "1000" in demand["why"] and "2000" not in demand["why"]


def test_stale_measurement_cannot_pass_gate(owner):
    bundle=measured()
    bundle["retrieved_at"]=(datetime.now(timezone.utc)-timedelta(days=40)).isoformat()
    rows=save_measurement(owner,bundle)
    report={"analytics_queries":["desk mat"]}
    analytics.attach(report,rows)
    assert report["market"]["markets"]["USA"]["analytics_status"]=="missing"


def test_keepa_does_not_use_uae_or_false_amazon_host():
    assert analytics.asins_from_urls(["https://amazon.ae/dp/B012345678","https://amazon.com.evil.test/dp/B012345678"])==[]
    assert analytics.asins_from_urls(["https://www.amazon.com/dp/B012345678?tag=x"])==["B012345678"]


def test_keepa_prices_and_unknown_sales(owner,monkeypatch):
    connect(monkeypatch,"keepa")
    minutes=int((datetime.now(timezone.utc)-datetime(2011,1,1,tzinfo=timezone.utc)).total_seconds()/60)
    raw={"tokensConsumed":1,"products":[{"asin":"B012345678","domainId":1,"title":"Desk mat","lastUpdate":minutes,
        "monthlySold":None,"stats":{"current":[-1,2599,-1,500],"avg90":[-1,2499]}}]}
    monkeypatch.setattr(analytics,"_send",lambda *args:httpx.Response(200,json=raw))
    result=analytics.keepa(context(owner),["B012345678"])
    assert result["market"]=="USA" and result["products"][0]["price_usd"]==25.99
    assert result["products"][0]["monthly_sold_lower_bound"] is None


def test_meta_empty_results_do_not_become_sales(owner,monkeypatch):
    connect(monkeypatch,"searchapi")
    raw={"search_parameters":{"country":"us"},"search_information":{"total_results":0},"ads":[]}
    monkeypatch.setattr(analytics,"_send",lambda *args:httpx.Response(200,json=raw))
    result=analytics.ads(context(owner),"desk mat","USA")
    assert result["status"]=="no_data" and result["metrics"]==[]


def test_credentials_are_encrypted_and_owner_scoped(owner):
    credentials.save(owner,"keepa",{"KEEPA_API_KEY":"private-live-placeholder"})
    with db.db() as conn:
        raw=conn.execute("SELECT ciphertext FROM integration_secrets").fetchone()[0]
        cur=conn.execute("INSERT INTO users(email,password_hash,created_at) VALUES(?,?,?)",("second@test.local",hash_password("test123"),db.now_iso()))
        second=cur.lastrowid
    assert "private-live-placeholder" not in raw
    assert credentials.get(owner,"KEEPA_API_KEY")=="private-live-placeholder"
    assert credentials.get(second,"KEEPA_API_KEY")==""


def test_relevant_old_feedback_is_retrieved_after_many_new_comments(owner):
    with db.db() as conn:
        first=db.create_idea(conn,owner,"web","Fragile lamps")
        old=db.save_feedback(conn,owner,first,"dislike","Avoid fragile glass lamps, shipping damage",True)
        for index in range(40):
            other=db.create_idea(conn,owner,"web",f"Other {index}")
            db.save_feedback(conn,owner,other,"revise",f"Consider different fabric color {index}",True)
        target=db.create_idea(conn,owner,"web","Glass lamps with fragile shades")
        result=memory.snapshot(conn,owner,target)
    assert old in [item["id"] for item in result["feedback"]] and result["active_feedback_total"]==41


def test_feedback_disabling_removes_global_rule(owner):
    with db.db() as conn:
        idea=db.create_idea(conn,owner,"web","Lamp")
        identifier=db.save_feedback(conn,owner,idea,"dislike","Avoid fragile goods",True)
        memory.save_rules(conn,owner,identifier,["not_fragile"])
        assert memory.active_rules(conn,owner)
        conn.execute("UPDATE idea_feedback SET remember=0 WHERE id=?",(identifier,))
        assert not memory.active_rules(conn,owner)
        assert not memory.snapshot(conn,owner)["feedback"]


def test_feedback_rule_changes_launch_gate_in_code(owner):
    rule={"key":"not_fragile","feedback_id":12}
    profile={"feedback_rules":[rule]}
    market={"product_attributes":{"fragile":True}}
    gates=scoring.market_gates("USA",market,{"status":"insufficient","missing_labels":[]},profile)
    assert next(g for g in gates if g["key"]=="preference:not_fragile")["status"]=="fail_soft"
    market["product_attributes"]["fragile"]=False
    assert memory.check_rules(market,[rule])[0]["status"]=="ok"
    market["product_attributes"]["fragile"]=None
    assert memory.check_rules(market,[rule])[0]["status"]=="unknown"


def test_memory_trace_is_immutable_for_resumed_request(owner):
    ctx=context(owner)
    with db.db() as conn:
        idea=db.create_idea(conn,owner,"web","Desk mat")
        first=db.save_feedback(conn,owner,idea,"revise","Prefer USA repeat purchases",True)
        block=research._memory_block(conn,owner,idea,ctx)
        second=db.save_feedback(conn,owner,idea,"revise","New instruction added later",True)
        resumed=research._memory_block(conn,owner,idea,llm.Ctx(owner,ctx.job_id))
    assert block==resumed and str(first) in block and "New instruction added later" not in resumed
    assert first!=second


def test_comment_reaches_market_prompt_and_report_trace(owner,monkeypatch):
    ctx=context(owner)
    captured=[]
    monkeypatch.setattr(llm,"json_call",lambda *args,**kwargs:captured.append(args[3]) or {})
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"web","Desk mat")
        db.save_feedback(conn,owner,idea_id,"revise","Prefer replaceable washable covers",True)
        idea=db.get_idea(conn,idea_id,owner)
        research.step_market(ctx,conn,idea,{}, {"keywords":["desk mat"]})
    assert "Prefer replaceable washable covers" in captured[0]
    explanation=basis.build({"memory_context":{**ctx.memory_snapshot,"sent_to_model":True}},{},{})
    assert explanation["memory_verified"] and explanation["feedback_sent"][0]["reason"]=="Prefer replaceable washable covers"


def test_sources_and_feedback_forms_hide_secrets_and_enforce_csrf(owner):
    from app.web.main import app
    client=TestClient(app)
    client.post("/login",data={"email":config.ADMIN_EMAIL,"password":"secret123"})
    html=client.get("/sources").text
    csrf=re.search(r'name="csrf_token" value="([^"]+)"',html).group(1)
    response=client.post("/sources/keepa/credentials",data={"csrf_token":csrf,"KEEPA_API_KEY":"do-not-render-this"})
    assert response.status_code==200 and "do-not-render-this" not in response.text
    assert credentials.get(owner,"KEEPA_API_KEY")=="do-not-render-this"
    assert client.post("/sources/keepa/credentials",data={"csrf_token":"bad","KEEPA_API_KEY":"overwrite"}).status_code==400
    assert credentials.get(owner,"KEEPA_API_KEY")=="do-not-render-this"
    with db.db() as conn:
        idea=db.create_idea(conn,owner,"web","UI rules")
    response=client.post(f"/ideas/{idea}/feedback",data={"csrf_token":csrf,"rating":"revise","comment":"Avoid breakage",
        "remember":"on","rules":["not_fragile"]})
    assert response.status_code==200
    with db.db() as conn:
        assert memory.active_rules(conn,owner)[0]["key"]=="not_fragile"


def test_analytical_gate_requires_api_values_not_model_links(owner):
    gates=scoring.market_gates("USA",{"analytics_status":"missing"},{"status":"insufficient","missing_labels":[]},{"require_analytics":True})
    assert next(g for g in gates if g["key"]=="analytics")["status"]=="unknown"


def test_provider_json_removes_echoed_secret_values():
    clean=analytics._safe({"api_key":"secret","nested":{"password":"secret","value":100},"request_url":"https://x/?key=secret"})
    assert clean=={"nested":{"value":100}}


def test_no_paid_daily_generation_before_analytics_connection(owner,monkeypatch):
    monkeypatch.setattr(config,"FAKE_LLM",False)
    monkeypatch.setattr(llm,"json_call",lambda *args,**kwargs:pytest.fail("Must not pay for unsupported daily selection"))
    with db.db() as conn:
        job_id=jobs.enqueue(conn,owner,"daily",payload={"date":"2026-11-23"})
        job=conn.execute("SELECT * FROM jobs WHERE id=?",(job_id,)).fetchone()
    research.run_daily(job)
    with db.db() as conn:
        summary=conn.execute("SELECT summary FROM daily_runs WHERE user_id=?",(owner,)).fetchone()[0]
        assert "DataForSEO" in summary and db.job_spend(conn,job_id)==0


def test_saved_mandatory_feedback_blocks_otherwise_positive_verdict(owner):
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"web","Glass table lamp")
        identifier=db.save_feedback(conn,owner,idea_id,"dislike","Avoid fragile shipping",True)
        memory.save_rules(conn,owner,identifier,["not_fragile"])
        rules=memory.active_rules(conn,owner)
    market={"product_attributes":{"fragile":True},
        "scores":{key:{"score":9} for key in ("demand","intent","competition","feasibility")},
        "launch_feasibility":{key:{"status":"ok"} for key in ("payments","legal","fulfilment","product_requirements")},
        "demand":{"signals":[{"type":"observation","scope":"product","verified":True,"retrieved_at":db.now_iso(),
            "source_url":f"https://example{index}.test/product"} for index in range(4)]}}
    inputs=economics.default_inputs(15)
    inputs["markets"]["USA"].update(price=100,unit_cost=10,cac_base=5,orders_base=50)
    computed=economics.compute(inputs)["markets"]["USA"]
    profile={"feedback_rules":rules,"budget_start":100000,"hours_per_week":1000}
    rejected=scoring.score_market("USA",market,computed,profile)
    market["product_attributes"]["fragile"]=False
    accepted=scoring.score_market("USA",market,computed,profile)
    assert rejected["verdict"]=="improve" and rejected["score"]<=4.5
    assert accepted["verdict"]=="test"
