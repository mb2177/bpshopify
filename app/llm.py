"""Обёртка над Claude API: веб-поиск, pause_turn, учёт расходов, лимиты бюджета, кеш шагов."""
import json
import re
import hashlib
import uuid
from dataclasses import dataclass, field

from . import config, db, jobs

shutdown_requested = False


class BudgetExceeded(Exception):
    pass


class LLMUnavailable(Exception):
    pass


@dataclass
class Ctx:
    user_id: int
    job_id: int | None = None
    per_job_limit: float = 1.5
    monthly_limit: float = 30.0
    seen_sources: list = field(default_factory=list)
    research_model: str | None = None   # None → MODEL_RESEARCH; для глубокого разбора — MODEL_DEEP
    budget_group: int | None = None
    force_fresh: bool = False
    memory_snapshot: dict | None = None
    analytics_force_fresh: bool = False


def _client():
    if not config.ANTHROPIC_API_KEY:
        raise LLMUnavailable("Не задан ANTHROPIC_API_KEY — исследование ожидает подключения.")
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=0, timeout=600)


def _prices(model: str):
    if model == config.MODEL_FAST:
        return config.PRICE_FAST_IN, config.PRICE_FAST_OUT
    if model == config.MODEL_DEEP:
        return config.PRICE_DEEP_IN, config.PRICE_DEEP_OUT
    return config.PRICE_RESEARCH_IN, config.PRICE_RESEARCH_OUT


def check_budget(ctx: Ctx) -> None:
    with db.db() as conn:
        month = db.month_spend(conn, ctx.user_id)
        job = db.group_spend(conn, ctx.budget_group or ctx.job_id) if ctx.job_id else 0.0
    if month >= ctx.monthly_limit:
        raise BudgetExceeded(f"Месячный лимит {ctx.monthly_limit:.2f} USD исчерпан (потрачено {month:.2f} USD).")
    if ctx.job_id and job >= ctx.per_job_limit:
        raise BudgetExceeded(f"Лимит на одно исследование {ctx.per_job_limit:.2f} USD исчерпан ({job:.2f} USD).")


def reserve(ctx: Ctx, amount: float, key: str) -> None:
    with db.db() as conn, db.tx(conn):
        pending = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM budget_reservations WHERE user_id=?",
                               (ctx.user_id,)).fetchone()["n"]
        if db.month_spend(conn, ctx.user_id) + pending + amount > ctx.monthly_limit:
            raise BudgetExceeded("На следующий запрос не хватает месячного бюджета. Сохранён готовый результат.")
        group = ctx.budget_group or ctx.job_id
        if group:
            reserved = conn.execute("SELECT COALESCE(SUM(amount),0) n FROM budget_reservations WHERE budget_group=?",
                                    (group,)).fetchone()["n"]
            if db.group_spend(conn, group) + reserved + amount > ctx.per_job_limit:
                raise BudgetExceeded("Следующий запрос может превысить лимит исследования. Сохранён готовый результат.")
        try:
            conn.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?,?)",
                         (key,ctx.user_id,ctx.job_id,group,amount,db.now_iso()))
        except Exception as exc:
            raise BudgetExceeded("Этот запрос уже выполняется") from exc


def release(key: str) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM budget_reservations WHERE reservation_key=?", (key,))


def _record(ctx: Ctx, model: str, resp, step=None, checkpoint=None, reservation_key=None) -> None:
    u = getattr(resp, "usage", None)
    in_tok = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    out_tok = getattr(u, "output_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    pin, pout = _prices(model)
    read = getattr(u, "cache_read_input_tokens", 0) or 0
    write = getattr(u, "cache_creation_input_tokens", 0) or 0
    normal = getattr(u, "input_tokens", 0) or 0
    cost = (normal + read * 0.1 + write * 1.25) / 1e6 * pin + out_tok / 1e6 * pout + searches * config.PRICE_PER_SEARCH
    with db.db() as conn, db.tx(conn):
        db.add_usage(conn, ctx.user_id, ctx.job_id, "anthropic", model, in_tok, out_tok, searches, cost, step)
        if checkpoint is not None and ctx.job_id:
            conn.execute("INSERT OR REPLACE INTO step_cache(job_id,step,result,created_at) VALUES(?,?,?,?)",
                         (ctx.job_id, step + ":pending", db.jdump(checkpoint), db.now_iso()))
        if reservation_key:
            conn.execute("DELETE FROM budget_reservations WHERE reservation_key=?", (reservation_key,))
    if searches:
        db.source_ok("claude_web_search")


def _collect_sources(ctx: Ctx, content) -> None:
    for b in content:
        t = getattr(b, "type", None)
        if t == "web_search_tool_result":
            items = getattr(b, "content", None)
            if isinstance(items, list):
                for it in items:
                    url = getattr(it, "url", None)
                    if url:
                        ctx.seen_sources.append({"url": url, "title": getattr(it, "title", ""),
                                                 "page_age": getattr(it, "page_age", None)})
        for citation in getattr(b,"citations",None) or []:
            url = getattr(citation,"url",None)
            if url:
                ctx.seen_sources.append({"url":url,"title":getattr(citation,"title","")})
        nested = getattr(b,"content",None)
        if isinstance(nested,list):
            _collect_sources(ctx,nested)


def call(ctx: Ctx, step: str, system: str, user_content, *, model: str | None = None,
         search: bool = False, max_tokens: int = 16000, max_searches: int | None = None,
         user_location: dict | None = None) -> str:
    """Возвращает итоговый текст ответа. Результат шага кешируется по (job_id, step)."""
    cached = jobs.cached_step(ctx.job_id, step)
    if cached is not None:
        ctx.seen_sources.extend(cached.get("sources", []))
        return cached["text"]
    jobs.check_cancel(ctx.job_id)

    if config.FAKE_LLM:
        from . import fake_llm
        text = fake_llm.respond(step, user_content)
        jobs.store_step(ctx.job_id, step, {"text": text, "sources": []})
        return text

    model = model or config.MODEL_RESEARCH
    digest = hashlib.sha256(db.jdump([system,user_content,model,search,max_tokens,max_searches]).encode()).hexdigest()
    cache_key = f"llm:{ctx.user_id}:{digest}"
    shared = None if ctx.force_fresh else db.cache_get(cache_key)
    if shared:
        ctx.seen_sources.extend(shared.get("sources", []))
        jobs.store_step(ctx.job_id, step, shared)
        with db.db() as conn:
            db.add_usage(conn,ctx.user_id,ctx.job_id,"cache",model,0,0,0,0,step)
        return shared["text"]
    tools = []
    if search:
        tool = {"type": config.WEB_SEARCH_TOOL, "name": "web_search",
                "max_uses": max_searches or config.WEB_SEARCH_MAX_USES}
        if user_location:
            tool["user_location"] = user_location
        tools.append(tool)
    pending = jobs.cached_step(ctx.job_id, step + ":pending") or {}
    if pending.get("stop_reason")=="max_tokens":
        raise ResponseTooLong("Предыдущий ответ оборвался по длине; нужен компактный повтор.")
    searches_used = int(pending.get("searches_used",0))
    if pending.get("done_text"):
        ctx.seen_sources.extend(pending.get("sources", []))
        jobs.store_step(ctx.job_id, step, {"text":pending["done_text"],"sources":pending.get("sources",[])})
        return pending["done_text"]
    client = _client()
    messages = pending.get("messages") or [{"role": "user", "content": user_content}]
    before = len(ctx.seen_sources)
    ctx.seen_sources.extend(pending.get("sources", []))
    texts = []
    for _ in range(6):  # продолжение при pause_turn
        if shutdown_requested:
            raise jobs.RestartRequested()
        check_budget(ctx)
        jobs.check_cancel(ctx.job_id)
        pin,pout = _prices(model)
        if search:
            input_reserve = config.SEARCH_INPUT_RESERVE_TOKENS
        else:
            text_size = len(system)
            image_tokens = 0
            for message in messages:
                blocks = message["content"]
                if isinstance(blocks,str):
                    text_size += len(blocks)
                else:
                    for block in blocks:
                        if block.get("type")=="image":
                            image_tokens += 4000
                        else:
                            text_size += len(db.jdump(block))
            input_reserve = max(1000,text_size+image_tokens)
        estimate = input_reserve / 1e6 * pin + max_tokens / 1e6 * pout
        estimate += (max_searches or config.WEB_SEARCH_MAX_USES) * config.PRICE_PER_SEARCH if search else 0
        reservation_key = f"{ctx.job_id}:{step}:{uuid.uuid4().hex}"
        reserve(ctx, estimate, reservation_key)
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
            remaining_searches = (max_searches or config.WEB_SEARCH_MAX_USES)-searches_used
            if remaining_searches <= 0:
                kwargs["tool_choice"] = {"type":"none"}
            else:
                kwargs["tools"] = [{**tools[0],"max_uses":remaining_searches}]
        try:
            resp = client.messages.create(**kwargs)
        except Exception as exc:
            release(reservation_key)
            if search:
                db.source_fail("claude_web_search", type(exc).__name__)
            raise
        _collect_sources(ctx, resp.content)
        usage = getattr(resp,"usage",None)
        server_use = getattr(usage,"server_tool_use",None)
        searches_used += int(getattr(server_use,"web_search_requests",0) or 0)
        content = json.loads(json.dumps(resp.content, default=lambda obj: obj.model_dump(mode="json") if hasattr(obj,"model_dump") else vars(obj)))
        texts = [b.text for b in resp.content if getattr(b,"type",None)=="text"]
        checkpoint = {"messages":messages + [{"role":"assistant","content":content}],
                      "stop_reason":resp.stop_reason,
                      "searches_used":searches_used,
                      "sources":ctx.seen_sources[before:],
                      "done_text":"\n".join(texts).strip() if resp.stop_reason=="end_turn" else None}
        _record(ctx, model, resp, step, checkpoint, reservation_key)
        if resp.stop_reason == "pause_turn":
            messages = checkpoint["messages"]
            jobs.check_cancel(ctx.job_id)
            continue
        if resp.stop_reason == "max_tokens":
            raise ResponseTooLong("Ответ модели не поместился в лимит длины.")
        break
    text = "\n".join(texts).strip()
    jobs.check_cancel(ctx.job_id)  # поздний ответ после отмены не сохраняем
    jobs.store_step(ctx.job_id, step, {"text": text, "sources": ctx.seen_sources[before:]})
    if text:
        db.cache_put(cache_key,{"text":text,"sources":ctx.seen_sources[before:]},86400)
    return text


def extract_json(text: str):
    """Достаёт JSON из ответа модели (последний блок ```json``` или первый объект)."""
    if not text:
        raise ValueError("Пустой ответ модели")
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for cand in reversed(blocks):
        try:
            return json.loads(cand)
        except ValueError:
            continue
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("В ответе модели нет JSON")


class ResponseTooLong(ValueError):
    """Модель упёрлась в max_tokens — ответ оборван."""


COMPACT_NOTE = ("\n\nВАЖНО: предыдущий ответ не поместился по длине. Ответь компактнее: каждое текстовое поле — "
                "1–2 коротких предложения, не больше 3 конкурентов и 3 сигналов спроса на рынок, не больше 5 источников. "
                "Схема JSON та же.")


def json_call(ctx: Ctx, step: str, system: str, user_content, **kw):
    try:
        text = call(ctx, step, system, user_content, **kw)
    except ResponseTooLong:
        # один повтор с просьбой ответить короче и с бо́льшим запасом длины (поиск — не больше одного)
        if isinstance(user_content, str):
            compact = user_content + COMPACT_NOTE
        else:
            compact = list(user_content) + [{"type": "text", "text": COMPACT_NOTE}]
        kw2 = dict(kw)
        kw2["max_tokens"] = int(kw.get("max_tokens", 4000) * 1.5)
        if kw.get("search"):
            kw2["max_searches"] = 1
        text = call(ctx, step + ":compact", system, compact, **kw2)
    if not text:
        raise LLMUnavailable("Модель не вернула результат. Продолжение сохранено для повторного запуска.")
    try:
        return extract_json(text)
    except ValueError:
        # одна попытка починить формат дешёвой моделью (без поиска)
        fix = call(ctx, step + ":fix", "Верни только валидный JSON без комментариев, сохранив данные. Незаконченные и отсутствующие значения замени на null. Факты и ссылки не добавляй.",
                   text[:24000], model=config.MODEL_FAST, max_tokens=6500)
        return extract_json(fix)

