"""Deterministic shortlist ranking with explicit preferences and evidence coverage."""
import re
from .sources import canonical_url


def similar(left: str, right: str) -> bool:
    first = set(re.findall(r"\w{3,}",left.lower()))
    second = set(re.findall(r"\w{3,}",right.lower()))
    return bool(first and second and len(first & second)/len(first | second) >= 0.65)


def rank(candidates: list, settings: dict, history: list[str], seen: list) -> list[dict]:
    known = {canonical_url(source.get("url")) for source in seen}
    ranked = []
    for index,candidate in enumerate(candidates[:8]):
        candidate = dict(candidate)
        urls = {canonical_url(e.get("source_url")) for e in candidate.get("evidence",[])} - {""}
        supported = urls & known
        repeated = any(similar(candidate.get("title", ""),title) for title in history)
        seasonal = candidate.get("is_seasonal") is not False
        market = candidate.get("market_guess")
        reasons = []
        if repeated:
            reasons.append("Похожая идея уже исследовалась")
        if not supported:
            reasons.append("Нет источника из выполненного поиска")
        if candidate.get("feasibility")=="blocked":
            reasons.append("Запуск заблокирован")
        if not candidate.get("scalability"):
            reasons.append("Путь масштабирования не описан")
        if market not in ("USA","UAE"):
            reasons.append("Рынок не определён")
        score = min(len(supported),3)*2
        if market==settings.get("market_priority","USA"):
            score += 1
        if not seasonal and settings.get("evergreen_first",True):
            score += 2
        if candidate.get("scalability"):
            score += 1
        price,cost = candidate.get("retail_price"),candidate.get("unit_cost")
        margin = None
        if isinstance(price,(int,float)) and isinstance(cost,(int,float)) and price>0 and cost>=0:
            margin = (price-cost)/price
            if margin <= 0:
                reasons.append("Цена не покрывает производство")
            else:
                score += min(2,margin*2)
        candidate.update(original_index=index,screening_score=round(score,2),
                         evidence_count=len(supported),screening_reasons=reasons,
                         eligible=not reasons,preliminary_gross_margin=margin,
                         screening_note="Предварительный отбор для исследования. Экономика и спрос требуют полного разбора.")
        ranked.append(candidate)
    ranked.sort(key=lambda c:(c["eligible"],c["screening_score"]),reverse=True)
    return ranked
