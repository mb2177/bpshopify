"""Regression cases from the owner's actual failed daily-idea conversation."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from app import basis, bot, config, db, jobs, llm, research, sources, worker
from app.web.auth import ensure_admin
from app.release_checks import prepare, OWNER_FEEDBACK


@pytest.fixture
def owner(tmp_path,monkeypatch):
    monkeypatch.setattr(config,"DB_PATH",tmp_path/"reliability.db")
    ensure_admin()
    with db.db() as conn:
        return conn.execute("SELECT id FROM users LIMIT 1").fetchone()[0]


def message(text,kind="private",is_bot=False,reply=None,actor=123):
    sent=[]
    async def answer(text,**kwargs):
        sent.append(text)
        return NS(message_id=1000)
    return NS(from_user=NS(id=actor,is_bot=is_bot),chat=NS(id=-111,type=kind),message_id=91,
        text=text,caption=None,reply_to_message=reply,answer=answer,sent=sent)


def test_confused_reply_after_failed_daily_is_free_for_every_status(owner):
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"daily","Растворимые этикетки",title="Этикетки")
    bot.map_msg(-111,90,idea_id)
    for status in ("failed","queued","researching","ready","needs_input","partial"):
        with db.db() as conn:
            db.set_idea(conn,idea_id,status=status)
        msg=message("Ты вообще о чём?",reply=NS(message_id=90))
        asyncio.run(bot.route_text(msg,owner,msg.text,[]))
        assert len(msg.sent)==1 and "Её выбрал сам бот" in msg.sent[0]
        assert "не запускает" in msg.sent[0]
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==0


def test_group_chatter_ack_and_bot_output_never_create_jobs(owner):
    cases=[message("Спасибо, вот про эту идею завтра поговорим подробно",kind="group"),
        message("Проверь идею: наборы этикеток",kind="group",is_bot=True),message("Спасибо!")]
    for msg in cases:
        asyncio.run(bot.route_text(msg,owner,msg.text,[]))
        assert not msg.sent
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==0
    msg=message("Проверь идею: комплект этикеток",kind="supergroup")
    asyncio.run(bot.route_text(msg,owner,msg.text,[]))
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==1


def test_group_pending_mode_is_bound_to_human_actor(owner):
    first=message("Товар",kind="group",actor=1)
    other=message("Обычная беседа",kind="group",actor=2)
    bot.pending_put(bot.mode_key(first,owner),owner,{"mode":"new"})
    assert bot.intended_for_bot(first,owner)
    assert not bot.intended_for_bot(other,owner)


def test_resume_retains_paid_steps_and_budget_and_is_idempotent(owner):
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"daily","Labels")
        job=jobs.enqueue(conn,owner,"research",idea_id)
        jobs.finish(conn,job,"failed","ValueError")
        db.set_idea(conn,idea_id,status="failed")
        db.add_usage(conn,owner,job,"anthropic","test",100,100,0,.02,"parse")
    jobs.store_step(job,"market",{"text":"already paid","sources":[]})
    jobs.store_step(job,"parse:pending",{"messages":[{"role":"assistant","content":"truncated"}]})
    with db.db() as conn:
        assert jobs.resume(conn,owner,idea_id)==job
        assert jobs.resume(conn,owner,idea_id) is None
        assert db.job_spend(conn,job)==.02
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==1
    assert jobs.cached_step(job,"market")["text"]=="already paid"
    assert jobs.cached_step(job,"parse:pending") is None


def test_truncated_checkpoint_does_not_send_invalid_assistant_prefill(owner,monkeypatch):
    monkeypatch.setattr(config,"FAKE_LLM",False)
    with db.db() as conn:
        job=jobs.enqueue(conn,owner,"research")
    jobs.store_step(job,"market:pending",{"stop_reason":"max_tokens","messages":[{"role":"assistant","content":"cut"}]})
    monkeypatch.setattr(llm,"_client",lambda:pytest.fail("Must use compact retry, not replay truncated prefill"))
    with pytest.raises(llm.ResponseTooLong):
        llm.call(llm.Ctx(owner,job),"market","sys","new")


def test_input_parse_has_room_for_russian_structured_response(owner,monkeypatch):
    seen=[]
    monkeypatch.setattr(llm,"json_call",lambda *args,**kwargs:seen.append(kwargs) or {})
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"daily","Labels")
        research.step_parse(llm.Ctx(owner),db.get_idea(conn,idea_id,owner))
    assert seen[0]["max_tokens"]>=2400


def test_verified_price_benchmark_excludes_duplicates_and_wrong_currency():
    offers=[{"name":"Shop","url":"https://shop.test/a","price":20,"currency":"USD","verified":True},
        {"url":"https://www.shop.test/b","price":200,"currency":"USD","verified":True},
        {"url":"https://other.test/c","price":40,"currency":"USD","verified":True},
        {"url":"https://unverified.test","price":1,"currency":"USD","verified":False},
        {"url":"https://uae.test","price":500,"currency":"AED","verified":True}]
    value=basis.price_benchmark({"competitors":offers},"USD")
    assert (value["count"],value["low"],value["high"],value["median"])==(2,20,40,30)
    assert "не продажи" in value["limitation"]


def test_price_quote_country_currency_and_numeric_boundaries(owner,monkeypatch):
    monkeypatch.setattr(config,"FAKE_LLM",False)
    page="Buy now $20.00 USD for the complete kit"
    monkeypatch.setattr(sources,"fetch_url_text",lambda url:{"ok":True,"text":page,"retrieved_at":db.now_iso()})
    base={"url":"https://shop.test/a","market":"USA","currency":"USD","price":20,"price_quote":"$20.00 USD"}
    items=[deepcopy(base),{**base,"price":2},{**base,"market":"UAE"},{**base,"currency":"AED"}]
    report={"seen_sources":[{"url":base["url"]}],"market":{"markets":{"USA":{"competitors":items}}}}
    sources.validate_evidence(report,[])
    assert [item["verified"] for item in items]==[True,False,False,False]


def test_preparation_uses_scheduler_dedupe_and_saved_owner_feedback(owner):
    with db.db() as conn:
        for index in range(4):
            db.create_idea(conn,owner,"daily",f"Idea {index}")
    one=prepare(owner,"2026-11-25")
    assert one==prepare(owner,"2026-11-25")
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==1
        rows=conn.execute("SELECT * FROM idea_feedback").fetchall()
        assert len(rows)==1 and rows[0]["comment"]==OWNER_FEEDBACK
        context=research._memory_block(conn,owner,ctx=llm.Ctx(owner,one))
        assert "круглогодичного спроса" in context


def test_old_saved_basis_and_historical_report_still_render(owner):
    from fastapi.testclient import TestClient
    from app.web.main import app
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"web","Label kit")
        jobs.enqueue(conn,owner,"research",idea_id)
    assert worker.process_one()
    with db.db() as conn:
        version=db.latest_version(conn,idea_id)
        report=db.jload(version["report"])
        report["basis"]={"version":1,"summary":"Old saved explanation", "markets":[{"market":"USA","currency":"USD"}]}
        conn.execute("UPDATE idea_versions SET report=? WHERE id=?",(db.jdump(report),version["id"]))
    client=TestClient(app)
    client.post("/login",data={"email":config.ADMIN_EMAIL,"password":config.ADMIN_PASSWORD})
    for path in (f"/ideas/{idea_id}",f"/ideas/{idea_id}?v=1"):
        page=client.get(path)
        assert page.status_code==200 and 'id="panel-basis"' in page.text
        assert 'id="panel-plan"' not in page.text


def test_search_uses_direct_structured_results_with_budget_cap(owner,monkeypatch):
    monkeypatch.setattr(config,"FAKE_LLM",False)
    calls=[]
    def create(**kwargs):
        calls.append(kwargs)
        return NS(stop_reason="end_turn",content=[
            NS(type="web_search_tool_result",content=[NS(type="web_search_result",url="https://source.test/product",title="Product")]),
            NS(type="text",text='{"ok":true}')],
            usage=NS(input_tokens=100,output_tokens=20,server_tool_use=NS(web_search_requests=1),
                cache_read_input_tokens=0,cache_creation_input_tokens=0))
    monkeypatch.setattr(llm,"_client",lambda:NS(messages=NS(create=create)))
    with db.db() as conn:
        identifier=jobs.enqueue(conn,owner,"daily")
    ctx=llm.Ctx(owner,identifier,per_job_limit=5,monthly_limit=100)
    assert llm.json_call(ctx,"direct","system","product",search=True,max_searches=3)=={"ok":True}
    assert calls[0]["tools"][0]["allowed_callers"]==["direct"]
    assert calls[0]["tools"][0]["max_uses"]==3
    assert ctx.seen_sources[0]["url"]=="https://source.test/product"


def test_reviewed_search_retry_reuses_original_job_and_cost_only_once(owner):
    identifier=prepare(owner,"2026-11-25")
    jobs.store_step(identifier,"daily",{"text":"search unavailable","sources":[{"url":"https://source.test"}]})
    with db.db() as conn:
        jobs.finish(conn,identifier)
        conn.execute("INSERT INTO daily_runs(user_id,run_date,summary,candidates,created_at) VALUES(?,?,?,?,?)",
            (owner,"2026-11-25","Поиск недоступен (лимит)","[]",db.now_iso()))
        db.add_usage(conn,owner,identifier,"anthropic","test",0,0,4,.18,"daily")
    assert prepare(owner,"2026-11-25")==identifier
    with db.db() as conn:
        row=conn.execute("SELECT * FROM jobs WHERE id=?",(identifier,)).fetchone()
        assert row["status"]=="queued" and db.jload(row["payload"])["selection_retry"]==1
        assert db.group_spend(conn,identifier)==.18
        jobs.finish(conn,identifier)
    prepare(owner,"2026-11-25")
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==1
        assert conn.execute("SELECT status FROM jobs").fetchone()[0]=="done"
    assert jobs.cached_step(identifier,"daily")["sources"]


def test_real_provided_pages_are_retained_as_evidence(owner,monkeypatch):
    monkeypatch.setattr(sources,"fetch_url_text",lambda url:{"url":url,"ok":True,"title":"Supplier",
        "text":"A complete kit costs $20.00 USD", "retrieved_at":db.now_iso()})
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"web","A complete kit",links=[{"url":"https://supplier.test/kit"}])
        jobs.enqueue(conn,owner,"research",idea_id)
    assert worker.process_one()
    with db.db() as conn:
        report=db.jload(db.latest_version(conn,idea_id)["report"])
    assert any(row.get("origin")=="provided_page" and row["url"]=="https://supplier.test/kit" for row in report["seen_sources"])


def test_daily_origin_is_explicit_in_model_input_and_not_attributed_to_owner(owner):
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"daily","Washable grip kit")
        text=research._idea_block(db.get_idea(conn,idea_id,owner))
    assert "выбрал сам бот" in text and "Владелец этот товар не предлагал" in text
    assert "вопросы владельцу не задавай" in text


def test_haiku_tier_boundary_applies_to_input_and_output(monkeypatch):
    monkeypatch.setattr(config,"MODEL_FAST","claude-haiku-5-5")
    monkeypatch.setattr(config,"PRICE_FAST_IN",.1)
    monkeypatch.setattr(config,"PRICE_FAST_OUT",.5)
    monkeypatch.setattr(config,"PRICE_FAST_LONG_IN",.5)
    monkeypatch.setattr(config,"PRICE_FAST_LONG_OUT",2.5)
    assert llm._prices(config.MODEL_FAST,100000)==(.1,.5)
    assert llm._prices(config.MODEL_FAST,100001)==(.5,2.5)


def test_sonnet_55_cache_read_price_uses_five_percent(owner,monkeypatch):
    monkeypatch.setattr(config,"MODEL_RESEARCH","claude-sonnet-5-5")
    monkeypatch.setattr(config,"PRICE_RESEARCH_IN",2)
    monkeypatch.setattr(config,"PRICE_RESEARCH_OUT",10)
    usage=NS(input_tokens=0,output_tokens=10,cache_read_input_tokens=1000,
        cache_creation_input_tokens=0,server_tool_use=None)
    with db.db() as conn:
        identifier=jobs.enqueue(conn,owner,"research")
    llm._record(llm.Ctx(owner,identifier),config.MODEL_RESEARCH,NS(usage=usage))
    with db.db() as conn:
        assert db.job_spend(conn,identifier)==pytest.approx(.0002)


def test_asking_about_idea_number_does_not_create_a_new_product(owner):
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"daily","Kit")
    for text in (f"Идея #{idea_id}?",f"Что с идеей {idea_id}?",f"#{idea_id}"):
        msg=message(text)
        asyncio.run(bot.route_text(msg,owner,text,[]))
        assert len(msg.sent)==1 and f"Идея #{idea_id}" in msg.sent[0]
    with db.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]==0


def test_production_uses_economical_model_and_preserves_explicit_deep_mode(owner,monkeypatch):
    calls=[]
    monkeypatch.setattr(llm,"json_call",lambda *args,**kwargs:calls.append(kwargs) or {})
    with db.db() as conn:
        idea_id=db.create_idea(conn,owner,"daily","Kit")
        idea=db.get_idea(conn,idea_id,owner)
        research.step_production(llm.Ctx(owner),conn,idea,{}, {},{})
        research.step_production(llm.Ctx(owner,research_model=config.MODEL_DEEP),conn,idea,{}, {},{})
    assert calls[0]["model"]==config.MODEL_FAST
    assert calls[1]["model"]==config.MODEL_DEEP
