"""ДЕМО-ответы для локальной проверки без API (FAKE_LLM=1). Все данные вымышлены и так помечены."""
import json
import re

DEMO = "ДЕМО — вымышленные данные"


def _market(mk, cur, price, comp_price, scores):
    return {
        "summary": f"{DEMO}. Сводка рынка {mk}.",
        "demand": {"summary": DEMO, "signals": [
            {"claim": "Рост запросов (демо)", "value": 20, "unit": "%", "period": "12 мес", "market": mk,
             "source_url": "https://example.com/demo-trends", "source_name": "DEMO", "type": "observation"},
            {"claim": "Сезонный пик (демо)", "value": None, "unit": "", "period": "", "market": mk,
             "source_url": "https://example.com/demo-season", "source_name": "DEMO", "type": "observation"},
        ]},
        "intent": DEMO, "language": "English" if mk == "USA" else "English / Arabic",
        "seasonality": {"summary": DEMO, "is_seasonal": False, "next_peak_date": None, "peaks": []},
        "price_range": {"low": price * 0.7, "high": price * 1.4, "currency": cur, "based_on": DEMO},
        "competitors": [{"name": f"Demo Shop {i}", "url": f"https://example.com/{mk.lower()}-shop-{i}",
                         "platform": "сайт", "region": mk, "market": mk, "price": comp_price + i,
                         "currency": cur, "assortment": DEMO, "differentiator": DEMO} for i in range(3)],
        "our_offer": DEMO,
        "launch_feasibility": {k: {"status": "ok", "note": DEMO} for k in
                               ("payments", "legal", "fulfilment", "product_requirements")},
        "acquisition": {"channels": ["Instagram", "TikTok"],
                        "cac_estimate": {"low": price * 0.15, "high": price * 0.4, "currency": cur,
                                         "type": "assumption", "basis": DEMO}},
        "scale_limits": DEMO,
        "economics_hints": {
            "price": {"value": price, "currency": cur, "type": "observation", "source_url": "https://example.com/demo"},
            "shipping_charged": {"value": 0, "currency": cur, "type": "assumption"},
            "outbound_shipping": {"value": 5 if cur == "USD" else 18, "currency": cur, "type": "estimate"},
            "return_rate_pct": {"value": 5, "type": "assumption"},
            "orders_month_base": {"value": 40, "type": "assumption", "basis": DEMO},
        },
        "scores": {k: {"score": v, "why": DEMO} for k, v in scores.items()},
    }


def respond(step: str, content) -> str:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    base = step.split(":")[0]
    if base == "parse":
        region = "Дубай" if "дуба" in text.lower() else None
        out = {"kind": "new_idea", "title": "ДЕМО: кастомные футболки", "product": "кастомные футболки",
               "buyer": "организаторы мероприятий", "situation": "свадьбы и вечеринки", "region_hint": region,
               "markets_requested": ["USA", "UAE"], "photo_description": None, "ambiguities": [], "critical_question": None}
    elif base in ("market", "market_r"):
        out = {"title": "ДЕМО: кастомные футболки для свадеб", "product": "футболки с персональным принтом",
               "buyer": "пары и друзья невесты/жениха", "situation": "девичник, свадьба",
               "segments": [{"market": "USA", "name": "Девичники", "who": DEMO, "why": DEMO, "price_point": "$25–35",
                             "where_to_find": "Instagram, Pinterest"}],
               "markets": {"USA": _market("USA", "USD", 29, 24, {"demand": 7, "intent": 7, "competition": 4, "feasibility": 8}),
                           "UAE": _market("UAE", "AED", 120, 95, {"demand": 5, "intent": 6, "competition": 6, "feasibility": 7})},
               "alt_explanations": [{"explanation": "DIY-интерес", "assessment": DEMO}],
               "recommended_market": "USA", "market_reasoning": DEMO, "second_market_conditions": DEMO,
               "hs_code": "6109", "risks": [DEMO], "main_risk": "Высокая конкуренция на Etsy (демо)",
               "data_gaps": ["Реальный CAC не измерен"],
               "next_step": {"action": "Заказать 3 образца", "cost": "$60", "success_criterion": "качество печати ≥ 30 стирок"},
               "sources": [{"url": "https://example.com/demo", "title": DEMO, "market": "both", "used_for": "демо"}]}
    elif base in ("production", "production_r"):
        out = {"options": [
            {"key": "contractor", "name": "A. Печать у подрядчика", "operations": "DTG/DTF у подрядчика", "equipment": "нет",
             "startup_cost": {"value": 0, "currency": "USD", "type": "estimate"},
             "unit_cost": {"value": 9, "currency": "USD", "type": "estimate", "source_url": "https://example.com/demo"},
             "moq": "от 10 шт", "lead_time_days": 10, "labor_minutes_per_unit": 3, "quality": DEMO, "capacity": DEMO,
             "risks": DEMO, "storage": "минимум", "scalability": "высокая"},
            {"key": "hybrid", "name": "B. Термопресс + готовые трансферы", "operations": "перенос трансфера",
             "equipment": "термопресс", "startup_cost": {"value": 400, "currency": "USD", "type": "estimate"},
             "unit_cost": {"value": 6.5, "currency": "USD", "type": "estimate"}, "moq": "от 1 шт",
             "lead_time_days": 7, "labor_minutes_per_unit": 6, "quality": DEMO, "capacity": "≈ 40 шт/день",
             "risks": DEMO, "storage": "заготовки", "scalability": "средняя"},
            {"key": "own", "name": "C. Своя DTF-печать", "operations": "печать + перенос", "equipment": "DTF-принтер, печь, пресс",
             "startup_cost": {"value": 6000, "currency": "USD", "type": "estimate"},
             "unit_cost": {"value": 4.5, "currency": "USD", "type": "estimate"}, "moq": "от 1 шт",
             "lead_time_days": 30, "labor_minutes_per_unit": 10, "quality": DEMO, "capacity": DEMO,
             "risks": DEMO, "storage": "оборудование", "scalability": "высокая"}],
            "equipment_breakeven": {"units_per_month": 250, "explanation": DEMO, "fits_owner_hours": "неизвестно"},
            "recommended_option": "contractor", "recommended_reason": DEMO,
            "contractors": [{"name": f"Demo Print {i}", "url": f"https://example.com/print-{i}", "services": "DTG",
                             "location": "демо", "market": "USA", "small_batch_terms": DEMO, "samples": DEMO,
                             "lead_time": "7–10 дней", "shipping": DEMO, "price": "нужен запрос",
                             "landed_cost_note": DEMO, "contact": None} for i in range(3)],
            "economics_inputs": {m: {"unit_cost": {"value": 9, "currency": "USD", "type": "estimate"},
                                     "labor_minutes": {"value": 4, "type": "estimate"},
                                     "batch_size": {"value": 30, "type": "assumption"},
                                     "lead_time_days": {"value": 21, "type": "estimate"}} for m in ("USA", "UAE")},
            "quote_request_draft": "Hello, (DEMO) we are looking for small-batch DTG printing...",
            "sources": []}
    elif base == "competitors":
        out = {"region": "демо", "market": "UAE", "competitors": [
            {"name": "Demo Dubai Tees", "url": "https://example.com/dubai", "platform": "Instagram", "region": "Дубай",
             "market": "UAE", "price": 110, "currency": "AED", "assortment": DEMO, "differentiator": DEMO}],
               "summary": DEMO, "gaps": []}
    elif base == "route":
        msg = text.split("Сообщение владельца:")[-1].lower()
        nums = re.findall(r"\d+", msg)
        if "парти" in msg and nums:
            out = {"scope": "economics", "market": "both", "changes": {"batch_size": float(nums[0])},
                   "region": None, "instruction": msg, "summary": f"Партия {nums[0]} шт."}
        elif "цен" in msg and nums:
            out = {"scope": "economics", "market": "both", "changes": {"price": float(nums[0])},
                   "region": None, "instruction": msg, "summary": f"Цена {nums[0]}"}
        elif "конкурент" in msg:
            out = {"scope": "competitors", "market": "UAE", "changes": {}, "region": "Дубай",
                   "instruction": msg, "summary": "Конкуренты в Дубае"}
        elif "?" in msg:
            out = {"scope": "question", "market": "both", "changes": {}, "region": None, "instruction": msg, "summary": "вопрос"}
        else:
            out = {"scope": "market", "market": "both", "changes": {}, "region": None, "instruction": msg, "summary": msg[:60]}
    elif base == "answer":
        return "ДЕМО-ответ: по данным разбора рекомендуемый рынок — США (USA)."
    elif base == "daily":
        out = {"candidates": [{"title": "ДЕМО: именные фартуки для барбекю", "product": "фартук", "buyer": "мужчины 30–50",
                               "situation": "подарок на День отца", "market_guess": "USA", "why": DEMO,
                               "evidence": [], "weakness": DEMO}], "chosen_index": 0,
               "no_good_candidate_reason": None, "repeat_of_previous": None, "what_changed": None}
    else:
        out = {}
    return "```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"
