"""Short, deterministic explanation of evidence and calculations, not hidden model reasoning."""
from statistics import median
from urllib.parse import urlsplit
from . import analytics, config


def price_benchmark(market, currency):
    """One verified offer per seller; listing prices do not measure demand or sales."""
    offers, sellers = [], set()
    for item in market.get("competitors") or []:
        price = analytics.number(item.get("price"))
        url = item.get("url") or ""
        domain = urlsplit(url).netloc.lower().removeprefix("www.")
        if (not item.get("verified") or item.get("currency")!=currency or
            price is None or price<=0 or not domain or domain in sellers):
            continue
        sellers.add(domain)
        offers.append({"name":item.get("name"),"url":url,"price":price,
            "quote":item.get("price_quote"),"retrieved_at":item.get("retrieved_at")})
    prices = [item["price"] for item in offers]
    return {"count":len(prices),"low":min(prices) if prices else None,
        "high":max(prices) if prices else None,"median":median(prices) if prices else None,"offers":offers,
        "limitation":"Цены предложений, не продажи. Комплектация, доставка и количество в наборе могут отличаться; малую выборку нельзя считать всем рынком."}


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
        currency = economic.get("currency") or {"USA":"USD","UAE":"AED"}[market]
        benchmark = price_benchmark(mr,currency)
        unit = base.get("per_order") or {}
        our_price = analytics.number(unit.get("price"))
        benchmark["our_price"] = our_price
        benchmark["price_vs_median_pct"] = (round((our_price/benchmark["median"]-1)*100,1)
            if our_price is not None and benchmark["median"] else None)
        sources = []
        for signal in ((mr.get("demand") or {}).get("signals") or []):
            if signal.get("verified") and signal.get("source_url") and len(sources)<2:
                sources.append({"claim":signal.get("claim") or signal.get("note"),"url":signal["source_url"],"quote":signal.get("quote")})
        markets.append({"market":market,"score":score.get("score"),"verdict":score.get("verdict"),
            "analytics_count":mr.get("analytics_count",0),"observations":score.get("observations",0),
            "demand_method":((mr.get("scores") or {}).get("demand") or {}).get("why"),
            "cash_need":base.get("startup_cash_need"),"margin_per_order":(base.get("per_order") or {}).get("CM2"),
            "currency":currency,"sources":sources,"price_benchmark":benchmark,
            "breakeven_cac":unit.get("breakeven_cac"),"breakeven_roas":unit.get("breakeven_roas"),
            "breakeven_orders":base.get("breakeven_orders"),"cac_assumption":unit.get("CAC"),
            "missing":[g["label"] for g in score.get("gates",[]) if g["status"]=="unknown"],
            "stops":[g["label"] for g in score.get("gates",[]) if g["status"] in ("fail_hard","fail_soft")],
            "preference_checks":[g for g in score.get("gates",[]) if g["key"].startswith("preference:")]})
    context = report.get("memory_context") or {}
    return {"version":2,"facts":facts[:12],"markets":markets,
        "feedback_sent":context.get("feedback") or [],"memory_captured_at":context.get("captured_at"),
        "feedback_total":context.get("active_feedback_total",0),"rules":(report.get("rule_context") or {}).get("rules") or [],
        "memory_verified":bool(context.get("sent_to_model")),"connections":report.get("analytics_connections") or [],
        "summary":"Здесь отдельно показаны доступные измерения API, проверенные цены предложений и расчёты экономики. У источников сохранены даты и ссылки. Неподтверждённые оценки остаются гипотезами.",
        "limitations":"Поисковый интерес, объявления и рейтинг Amazon не являются прогнозом продаж или вероятностью успеха."}
