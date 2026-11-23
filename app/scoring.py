"""Оценка идеи по 10-балльной шкале, обязательные условия запуска и вердикт.

Балл — это взвешенная сумма компонентов, а НЕ вероятность успеха. Невыполнимое обязательное условие
ограничивает балл сверху: красивая сумма не может скрыть стоп-фактор.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import math

from . import config, economics
from .sources import canonical_url
from urllib.parse import urlsplit
import re

WEIGHTS = {"demand": 0.25, "intent": 0.15, "competition": 0.15, "economics": 0.30, "feasibility": 0.15}
COMPONENT_LABEL = {
    "demand": "Спрос (поисковый интерес, сезонность, сигналы)",
    "intent": "Коммерческое намерение (готовность купить)",
    "competition": "Возможность отличиться от конкурентов",
    "economics": "Экономика (маржа после рекламы, базовый сценарий)",
    "feasibility": "Выполнимость (производство, платежи, доставка, требования)",
}
VERDICTS = {
    "test": "тестировать", "improve": "доработать", "watch": "наблюдать",
    "reject": "отклонить", "insufficient": "недостаточно данных",
}
RULES_TEXT = [
    "Компоненты 0–10: спрос 25%, намерение 15%, конкуренция 15%, экономика 30%, выполнимость 15%.",
    "Экономика считается кодом: маржинальность после рекламы базового сценария; 35% и выше = 10 баллов, 0% и ниже = 0.",
    "Учитываются проверенные ссылки без дублей. Спрос на товар оценивается отдельно от цен конкурентов и общей статистики.",
    "Жёсткий стоп (платежи или законность заблокированы, CM1 ≤ 0, пропущен сезон) — балл не выше 3, вердикт «отклонить».",
    "Мягкий стоп (не хватает бюджета или часов, CM2 ≤ 0 при положительном CM1) — балл не выше 4.5, вердикт «доработать».",
    "Мало проверяемых данных (меньше 3 наблюдений с источником) — балл не выше 6, вердикт «недостаточно данных».",
    "Без обязательных входов экономики (цена, себестоимость, CAC, объём) — вердикт «недостаточно данных».",
    "Неизвестные платежи, условия запуска, бюджет или часы блокируют рекомендацию теста. Статус риска требует доработки.",
    "При сопоставимых оценках с разницей до 0.5 балла учитывается выбранный владельцем приоритет рынка.",
    "Иначе: ≥7 — тестировать, ≥5 — доработать, ≥3.5 — наблюдать, ниже — отклонить.",
    "Рынок выбирается кодом: лучший балл среди рынков, прошедших обязательные условия.",
]


def _clamp(x, lo=0.0, hi=10.0):
    return max(lo, min(hi, x))


def _f(v):
    try:
        number = float(v)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def fresh_observation(value: dict, max_days=90) -> bool:
    retrieved = value.get("retrieved_at")
    if not retrieved:
        return False
    try:
        observed = datetime.fromisoformat(str(retrieved)[:10]).date()
    except ValueError:
        return False
    today = datetime.now(timezone.utc).date()
    if not 0 <= (today-observed).days <= max_days:
        return False
    years = [int(year) for year in re.findall(r"\b20\d{2}\b",str(value.get("period") or ""))]
    return not years or max(years) >= today.year-2


def evidence_quality(mreport: dict) -> dict:
    urls = set()
    demand = set()
    prices = set()
    for s in (mreport.get("demand", {}) or {}).get("signals", []) or []:
        url = canonical_url(s.get("source_url"))
        if s.get("type") == "observation" and s.get("verified") and url and fresh_observation(s):
            urls.add(url)
            if s.get("scope") == "product":
                demand.add(url)
    for c in mreport.get("competitors", []) or []:
        url = canonical_url(c.get("url"))
        if c.get("verified") and url and c.get("price") not in (None, "") and fresh_observation(c,30):
            urls.add(url)
            prices.add(url)
    domains = {urlsplit(url).hostname for url in urls}
    quality = "высокое" if len(domains)>=3 and len(demand)>=2 and len(prices)>=2 else "среднее" if demand and len(urls)>=3 else "низкое"
    return {"observations":len(urls),"demand_observations":len(demand),"price_observations":len(prices),
            "independent_domains":len(domains),"data_quality":quality}


def count_observations(mreport: dict) -> int:
    return evidence_quality(mreport)["observations"]


def days_until(dstr: str | None, today: date | None = None):
    if not dstr:
        return None
    try:
        d = datetime.fromisoformat(str(dstr)[:10]).date()
    except ValueError:
        return None
    return (d - (today or date.today())).days


def market_gates(mk: str, mreport: dict, econ_m: dict, profile: dict) -> list[dict]:
    """Список проверок обязательных условий: status ok | fail_hard | fail_soft | unknown."""
    gates = []
    lf = mreport.get("launch_feasibility", {}) or {}
    for key, label in (("payments", "Приём платежей"), ("legal", "Законный запуск продаж"),
                       ("fulfilment", "Выполнение заказов"), ("product_requirements", "Требования к товару и права на дизайн")):
        g = lf.get(key) or {}
        st = g.get("status", "unknown")
        if st=="ok" and mreport.get("provenance_checked") and not g.get("verified"):
            st = "unknown"
            g = {**g,"note":(g.get("note") or "") + " Источник этого условия ещё не подтверждён."}
        status = {"ok": "ok", "risk": "fail_soft", "blocked": "fail_hard"}.get(st, "unknown")
        gates.append({"key": key, "label": label, "status": status, "note": g.get("note", "нет данных")})

    quality = evidence_quality(mreport)
    gates.append({"key":"demand","label":"Подтверждение спроса на товар",
                  "status":"ok" if quality["demand_observations"] else "unknown",
                  "note":f"Проверенных источников спроса на конкретный товар — {quality['demand_observations']}"})
    season = mreport.get("seasonality") or {}
    if season.get("is_seasonal"):
        du = days_until(season.get("next_peak_date"),datetime.now(ZoneInfo(config.MARKET_TZ[mk])).date())
        hints = mreport.get("economics_hints") or {}
        lead = _f(hints.get("lead_time_days"))
        if econ_m.get("status") == "ok":
            lead = econ_m["scenarios"]["base"]["lead_days"]
        if du is not None and du < 14:
            status = "fail_hard"
        elif du is None or lead is None:
            status = "unknown"
        else:
            status = "ok" if du >= lead+14 else "fail_hard"
        gates.append({"key":"season","label":"Успеваем к сезону","status":status,
                      "note":f"До пика {du if du is not None else 'неизвестно'} дней; подготовка {lead if lead is not None else 'не подтверждена'} дней + 14 дней запаса"})
    if econ_m.get("status") != "ok":
        gates.append({"key": "economics", "label": "Экономика", "status": "unknown",
                      "note": "Не хватает входов: " + ", ".join(econ_m.get("missing_labels", []))})
        for key,label in (("budget","Бюджет старта"),("hours","Рабочее время")):
            gates.append({"key":key,"label":label,"status":"unknown","note":"Нужна полная экономика для проверки"})
        return gates
    base = econ_m["scenarios"]["base"]
    u = base["per_order"]
    cur = econ_m["currency"]
    if u["CM1"] <= 0:
        gates.append({"key": "economics", "label": "Экономика", "status": "fail_hard",
                      "note": f"CM1 = {u['CM1']:.2f} {cur} ≤ 0 — убыток с каждого заказа ещё до рекламы"})
    elif u["CM2"] <= 0:
        gates.append({"key": "economics", "label": "Экономика", "status": "fail_soft",
                      "note": f"CM2 = {u['CM2']:.2f} {cur} ≤ 0 — CAC {u['CAC']:.2f} выше безубыточного {u['CM1']:.2f}"})
    else:
        gates.append({"key": "economics", "label": "Экономика", "status": "ok",
                      "note": f"CM2 = {u['CM2']:.2f} {cur} на заказ в базовом сценарии"})

    budget = _f(profile.get("budget_start"))
    need_usd = base["in_base"]["startup_cash_need"]
    if budget is None:
        gates.append({"key": "budget", "label": "Бюджет старта", "status": "unknown",
                      "note": f"Бюджет не указан в профиле; нужно ≈ {need_usd:,.0f} USD"})
    elif need_usd > budget:
        gates.append({"key": "budget", "label": "Бюджет старта", "status": "fail_soft",
                      "note": f"Нужно ≈ {need_usd:,.0f} USD, бюджет {budget:,.0f} USD"})
    else:
        gates.append({"key": "budget", "label": "Бюджет старта", "status": "ok",
                      "note": f"Нужно ≈ {need_usd:,.0f} USD из {budget:,.0f} USD"})

    hours = _f(profile.get("hours_per_week"))
    need_h = base["hours_per_month"]
    if hours is not None:
        avail = hours * 4.3
        gates.append({"key": "hours", "label": "Рабочее время",
                      "status": "ok" if need_h <= avail else "fail_soft",
                      "note": f"Нужно ≈ {need_h:.0f} ч/мес на заказы, доступно ≈ {avail:.0f} ч/мес"})
    else:
        gates.append({"key":"hours","label":"Рабочее время","status":"unknown","note":"Часы владельца не указаны"})
    return gates


def score_market(mk: str, mreport: dict, econ_m: dict, profile: dict) -> dict:
    sub = mreport.get("scores", {}) or {}
    comps = {}
    for k in ("demand", "intent", "competition", "feasibility"):
        s = sub.get(k) or {}
        val = _f(s.get("score"))
        comps[k] = {"score": _clamp(val) if val is not None else None, "why": s.get("why", "")}
    obs = count_observations(mreport)
    if comps["demand"]["score"] is not None and obs == 0:
        comps["demand"]["score"] = min(comps["demand"]["score"], 4)
        comps["demand"]["why"] += " (ограничено: нет наблюдений с источником)"
    econ_score = None
    if econ_m.get("status") == "ok":
        m = econ_m["scenarios"]["base"]["per_order"]["margin_after_ads"]
        econ_score = _clamp((m or 0) / 0.35 * 10)
        comps["economics"] = {"score": round(econ_score, 1),
                              "why": f"Маржинальность после рекламы (базовый) {m*100:.1f}%"}
    else:
        comps["economics"] = {"score": None, "why": "Недостаточно входных данных"}

    total_w = sum(WEIGHTS[k] for k, c in comps.items() if c["score"] is not None)
    raw = (sum(WEIGHTS[k] * c["score"] for k, c in comps.items() if c["score"] is not None) / total_w) if total_w else 0
    gates = market_gates(mk, mreport, econ_m, profile)
    hard = [g for g in gates if g["status"] == "fail_hard"]
    soft = [g for g in gates if g["status"] == "fail_soft"]
    unknown = [g for g in gates if g["status"] == "unknown"]

    score, caps = raw, []
    if hard:
        score = min(score, 3.0); caps.append("жёсткий стоп")
    elif soft:
        score = min(score, 4.5); caps.append("мягкий стоп")
    quality = evidence_quality(mreport)
    if obs < 3:
        score = min(score, 6.0); caps.append("мало данных")
    if econ_score is None:
        score = min(score, 5.0); caps.append("нет экономики")
    if unknown:
        score = min(score, 5.0)
        caps.append("обязательные условия не подтверждены")

    if hard:
        verdict = "reject"
    elif econ_score is None:
        verdict = "insufficient"
    elif unknown:
        verdict = "insufficient"
    elif soft:
        verdict = "improve"
    elif obs < 3:
        verdict = "insufficient"
    elif score >= 7:
        verdict = "test"
    elif score >= 5:
        verdict = "improve"
    elif score >= 3.5:
        verdict = "watch"
    else:
        verdict = "reject"
    return {"market": mk, "raw": round(raw, 2), "score": round(score, 1), "components": comps,
            "gates": gates, "caps": caps, "verdict": verdict, **quality,
            "unknown_gates": [g["label"] for g in unknown]}


def evaluate(report: dict, econ: dict, profile: dict) -> dict:
    per = {}
    for mk in config.MARKETS:
        mrep = (report.get("markets") or {}).get(mk) or {}
        per[mk] = score_market(mk, mrep, econ["markets"].get(mk, {"status": "insufficient", "missing_labels": []}), profile)
    passing = [s for s in per.values() if s["verdict"] not in ("insufficient","reject") and
               not any(g["status"] in ("fail_hard","unknown") for g in s["gates"])]
    pool = passing or list(per.values())
    best = max(pool, key=lambda s: s["score"])
    priority = profile.get("market_priority","USA")
    preferred = per.get(priority)
    if preferred in passing and best["score"]-preferred["score"] <= 0.5:
        best = preferred
    chosen = best["market"] if passing else "none"
    if best["verdict"] in ("insufficient", "reject"):
        chosen = "none"  # без достаточных данных или при отклонении рынок не рекомендуем
    llm_choice = report.get("recommended_market")
    note = None
    if chosen in config.MARKETS:
        note = f"Приоритет владельца — {config.MARKET_LABEL.get(priority,'равное сравнение')}. Выбраны подтверждённые условия, экономика и оценка обоих рынков."
    if llm_choice in config.MARKETS and chosen in config.MARKETS and llm_choice != chosen:
        note = (f"Исследование предлагало {config.MARKET_LABEL[llm_choice]}, но по расчёту и обязательным "
                f"условиям выше {config.MARKET_LABEL[chosen]}.")
    return {
        "score": best["score"], "verdict": best["verdict"], "verdict_label": VERDICTS[best["verdict"]],
        "market": chosen, "market_label": config.MARKET_LABEL[chosen], "per_market": per,
        "rules": RULES_TEXT, "weights": WEIGHTS, "labels": COMPONENT_LABEL, "market_note": note,
        "is_probability": False,
    }

