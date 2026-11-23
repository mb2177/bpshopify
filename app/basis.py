"""Short, deterministic explanation of evidence and calculations, not hidden model reasoning."""
from . import analytics, config


def build(report, econ, scoring):
    facts = []
    for bundle in report.get("analytics") or []:
        if bundle.get("status")!="ok":
            continue
        for metric in (bundle.get("metrics") or [])[:1]:
            facts.append({**metric,"market":bundle["market"],"provider":analytics.PROVIDERS[bundle["provider"]]["name"],
                "source_url":bundle.get("source_url"),"period":bundle.get("period"),"retrieved_at":bundle.get("retrieved_at"),
                "fresh":analytics.fresh(bundle),"limitation":bundle.get("limitation"),"kind":bundle["kind"],"cached":bundle.get("cached")})
    markets = []
    for market in config.MARKETS:
        score = (scoring.get("per_market") or {}).get(market) or {}
        economic = (econ.get("markets") or {}).get(market) or {}
        base = (economic.get("scenarios") or {}).get("base") or {}
        mr = ((report.get("market") or {}).get("markets") or {}).get(market) or {}
        sources = []
        for signal in ((mr.get("demand") or {}).get("signals") or []):
            if signal.get("verified") and signal.get("source_url") and len(sources)<2:
                sources.append({"claim":signal.get("claim") or signal.get("note"),"url":signal["source_url"],"quote":signal.get("quote")})
        markets.append({"market":market,"score":score.get("score"),"verdict":score.get("verdict"),
            "analytics_count":mr.get("analytics_count",0),"observations":score.get("observations",0),
            "demand_method":((mr.get("scores") or {}).get("demand") or {}).get("why"),
            "cash_need":base.get("startup_cash_need"),"margin_per_order":(base.get("per_order") or {}).get("CM2"),
            "currency":economic.get("currency"),"sources":sources,
            "missing":[g["label"] for g in score.get("gates",[]) if g["status"]=="unknown"],
            "stops":[g["label"] for g in score.get("gates",[]) if g["status"] in ("fail_hard","fail_soft")],
            "preference_checks":[g for g in score.get("gates",[]) if g["key"].startswith("preference:")]})
    context = report.get("memory_context") or {}
    return {"version":1,"facts":facts[:12],"markets":markets,
        "feedback_sent":context.get("feedback") or [],"memory_captured_at":context.get("captured_at"),
        "feedback_total":context.get("active_feedback_total",0),"rules":(report.get("rule_context") or {}).get("rules") or [],
        "memory_verified":bool(context.get("sent_to_model")),"connections":report.get("analytics_connections") or [],
        "summary":"Измерения получены через API и сохранены с датой. Экономику и ограничения считает код. Неподтверждённые оценки модели остаются гипотезами.",
        "limitations":"Поисковый интерес, объявления и рейтинг Amazon не являются прогнозом продаж или вероятностью успеха."}
