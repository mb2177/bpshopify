"""Practical test plans with preparation gates and owner-recorded outcomes."""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from . import config


def build(report: dict, econ: dict, profile: dict, scoring: dict) -> dict:
    market = scoring.get("market")
    ready = market in ("USA","UAE")
    market = market if ready else profile.get("market_priority","USA")
    if market not in ("USA","UAE"):
        market = "USA"
    base = ((econ.get("markets",{}).get(market,{}) or {}).get("scenarios",{}).get("base") or {})
    limit = profile.get("max_test_loss")
    if limit in (None,""):
        limit = None
    else:
        limit = max(0,float(limit))
    lead = base.get("lead_days")
    today = datetime.now(ZoneInfo(config.MARKET_TZ[market])).date()
    start = (today+timedelta(days=int(lead))).isoformat() if ready and lead is not None else None
    steps = [
        ("conditions","Подтвердить условия запуска","Владелец","До закупки",[],
         "Подтверждены платежи, требования к товару, склад и доставка. Выбран один товар и рынок.","Остановиться при невыполнимом условии запуска."),
        ("quotes","Запросить цены и образцы","Владелец и подрядчик","До закупки",["conditions"],
         "Есть сопоставимые котировки на малую партию с доставкой. Образец принят по качеству.","Остановиться, если цена и сроки не дают положительной экономики."),
        ("economics","Уточнить бюджет и размер теста","Владелец","До рекламных расходов",["quotes"],
         "Цена, себестоимость, труд и доставка подтверждены. CAC отмечен как гипотеза. Расходы укладываются в лимит теста.","Пересмотреть цену или производство при CM2 ≤ 0."),
        ("store","Подготовить предложение и выполнение заказа","Владелец","Дни 1–7 после подготовки",["economics"],
         "Готовы фото, Shopify-карточка, условия заказа, упаковка, контроль качества и возвраты.","Отложить запуск до готовности оплаты и выполнения заказа."),
        ("acquisition","Проверить один сегмент и один-два канала","Владелец","Дни 8–21",["store"],
         "Зафиксированы расходы, обращения, оплаченные заказы и фактический CAC по каждому каналу.","Остановить расходы при достижении согласованного лимита потерь или недопустимого CAC."),
        ("decision","Сопоставить результат и решить о росте","Оба партнёра","Дни 22–30",["acquisition"],
         "Сверены CM2, качество, возвраты, труд и мощность. Решение продолжать, изменить или остановить записано.","Масштабировать после подтверждения положительной экономики на выполненных заказах."),
    ]
    return {"market":market,"phase":"test" if ready else "preparation","earliest_sales_date":start,
            "duration_days":30,"budget_limit_usd":limit,"assumptions":"Сроки этапов предложены для планирования. Даты продаж зависят от котировок и условий запуска.",
            "steps":[{"key":key,"action":action,"owner":owner,"deadline":deadline,"depends_on":depends,
                      "success_criterion":criterion,"stop_criterion":stop,"estimated_cost":None,"currency":"USD"}
                     for key,action,owner,deadline,depends,criterion,stop in steps]}
