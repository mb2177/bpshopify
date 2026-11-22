"""Проверка обёртки Claude API на подставном клиенте: pause_turn, учёт расходов, источники, кеш, бюджет."""
from types import SimpleNamespace as NS

import pytest

from app import config, db, llm
from app.web.auth import ensure_admin


class FakeClient:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        assert kw["tools"][0]["type"] == config.WEB_SEARCH_TOOL
        usage = NS(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                   server_tool_use=NS(web_search_requests=2))
        res = NS(type="web_search_tool_result", content=[NS(url="https://shop.example/a", title="A", page_age="2026")])
        if self.calls == 1:
            return NS(stop_reason="pause_turn", content=[res], usage=usage)
        return NS(stop_reason="end_turn", content=[NS(type="text", text='```json\n{"ok": true}\n```')], usage=usage)


def test_call_with_search(monkeypatch):
    ensure_admin()
    monkeypatch.setattr(config, "FAKE_LLM", False)
    fc = FakeClient()
    monkeypatch.setattr(llm, "_client", lambda: fc)
    with db.db() as conn:
        u = conn.execute("SELECT id FROM users").fetchone()["id"]
        jid = conn.execute("INSERT INTO jobs(user_id,kind,created_at) VALUES(?,?,?)", (u, "research", db.now_iso())).lastrowid
    ctx = llm.Ctx(user_id=u, job_id=jid, per_job_limit=5, monthly_limit=100)
    out = llm.json_call(ctx, "market", "sys", "text", search=True)
    assert out == {"ok": True} and fc.calls == 2
    assert ctx.seen_sources[0]["url"] == "https://shop.example/a"
    with db.db() as conn:
        cost = db.job_spend(conn, jid)
    expected = 2 * (1000 / 1e6 * config.PRICE_RESEARCH_IN + 500 / 1e6 * config.PRICE_RESEARCH_OUT + 2 * config.PRICE_PER_SEARCH)
    assert abs(cost - expected) < 1e-9
    # повтор шага берётся из кеша — без платного вызова
    llm.json_call(ctx, "market", "sys", "text", search=True)
    assert fc.calls == 2
    # лимит на исследование
    ctx2 = llm.Ctx(user_id=u, job_id=jid, per_job_limit=0.0001, monthly_limit=100)
    with pytest.raises(llm.BudgetExceeded):
        llm.call(ctx2, "other", "sys", "text")
