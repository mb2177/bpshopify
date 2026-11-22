"""Обёртка над Claude API: веб-поиск, pause_turn, учёт расходов, лимиты бюджета, кеш шагов."""
import json
import re
from dataclasses import dataclass, field

from . import config, db, jobs


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


def _client():
    if not config.ANTHROPIC_API_KEY:
        raise LLMUnavailable("Не задан ANTHROPIC_API_KEY — исследование ожидает подключения.")
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=3, timeout=900)


def _prices(model: str):
    if model == config.MODEL_FAST:
        return config.PRICE_FAST_IN, config.PRICE_FAST_OUT
    if model == config.MODEL_DEEP:
        return config.PRICE_DEEP_IN, config.PRICE_DEEP_OUT
    return config.PRICE_RESEARCH_IN, config.PRICE_RESEARCH_OUT


def check_budget(ctx: Ctx) -> None:
    with db.db() as conn:
        month = db.month_spend(conn, ctx.user_id)
        job = db.job_spend(conn, ctx.job_id) if ctx.job_id else 0.0
    if month >= ctx.monthly_limit:
        raise BudgetExceeded(f"Месячный лимит {ctx.monthly_limit:.2f} USD исчерпан (потрачено {month:.2f} USD).")
    if ctx.job_id and job >= ctx.per_job_limit:
        raise BudgetExceeded(f"Лимит на одно исследование {ctx.per_job_limit:.2f} USD исчерпан ({job:.2f} USD).")


def _record(ctx: Ctx, model: str, resp) -> None:
    u = getattr(resp, "usage", None)
    in_tok = (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
        + (getattr(u, "cache_creation_input_tokens", 0) or 0)
    out_tok = getattr(u, "output_tokens", 0) or 0
    stu = getattr(u, "server_tool_use", None)
    searches = (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
    pin, pout = _prices(model)
    cost = in_tok / 1e6 * pin + out_tok / 1e6 * pout + searches * config.PRICE_PER_SEARCH
    with db.db() as conn:
        db.add_usage(conn, ctx.user_id, ctx.job_id, "anthropic", model, in_tok, out_tok, searches, cost)


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

    check_budget(ctx)
    model = model or config.MODEL_RESEARCH
    client = _client()
    tools = []
    if search:
        tool = {"type": config.WEB_SEARCH_TOOL, "name": "web_search",
                "max_uses": max_searches or config.WEB_SEARCH_MAX_USES}
        if user_location:
            tool["user_location"] = user_location
        tools.append(tool)
    messages = [{"role": "user", "content": user_content}]
    texts = []
    before = len(ctx.seen_sources)
    for _ in range(6):  # продолжение при pause_turn
        kwargs = dict(model=model, max_tokens=max_tokens, system=system, messages=messages)
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)
        _record(ctx, model, resp)
        _collect_sources(ctx, resp.content)
        if resp.stop_reason == "pause_turn":
            messages = messages + [{"role": "assistant", "content": resp.content}]
            jobs.check_cancel(ctx.job_id)
            continue
        texts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
        break
    text = "\n".join(texts).strip()
    jobs.check_cancel(ctx.job_id)  # поздний ответ после отмены не сохраняем
    jobs.store_step(ctx.job_id, step, {"text": text, "sources": ctx.seen_sources[before:]})
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


def json_call(ctx: Ctx, step: str, system: str, user_content, **kw):
    text = call(ctx, step, system, user_content, **kw)
    try:
        return extract_json(text)
    except ValueError:
        # одна попытка починить формат дешёвой моделью (без поиска)
        fix = call(ctx, step + ":fix", "Верни только валидный JSON без комментариев, сохранив все данные.",
                   text[:60000], model=config.MODEL_FAST, max_tokens=16000)
        return extract_json(fix)
