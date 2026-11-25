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
