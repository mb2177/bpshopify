"""Калькулятор экономики. Только код и прозрачные формулы — модель сюда не вмешивается.

Определения (см. ТЗ):
  R    — выручка заказа после скидок, с платой за доставку, БЕЗ собранного VAT / sales tax.
  V    — переменные расходы заказа без рекламы (себестоимость с браком, труд, упаковка, отправка,
         комиссии, ожидаемые потери от возвратов).
  CM1  = R − V
  CM2  = CM1 − CAC
  Маржинальность после рекламы = CM2 / R
  Операционная прибыль месяца (до налогов на прибыль) = Σ CM2 − постоянные расходы
  Безубыточный CAC = CM1 (если CM1 > 0)
  Валовая маржа = (R − себестоимость товара) / R;  Наценка = (R − себестоимость) / себестоимость
"""
from copy import deepcopy
import math

from . import config

SCENARIOS = ("cons", "base", "opt")
SCENARIO_LABEL = {"cons": "Консервативный", "base": "Базовый", "opt": "Оптимистичный"}

# Поля, без которых экономику не считаем (неизвестное ≠ ноль)
REQUIRED = ("price", "unit_cost", "cac_base", "orders_base")

FIELD_LABELS = {
    "price": "Цена товара для покупателя",
    "price_includes_vat": "Цена включает VAT",
    "vat_rate": "Ставка VAT, %",
    "discount_pct": "Средняя скидка, %",
    "shipping_charged": "Доставка, оплаченная покупателем",
    "unit_cost": "Закупка/производство за единицу",
    "unit_cost_currency": "Валюта закупки",
    "duty_pct": "Пошлина при импорте, % от закупки",
    "inbound_per_unit": "Входящая логистика за единицу",
    "packaging": "Упаковка и расходники за заказ",
    "labor_minutes": "Наш труд на заказ, минут",
    "hourly_rate": "Стоимость нашего часа",
    "defect_pct": "Брак, %",
    "outbound_shipping": "Отправка покупателю (наш расход)",
    "payment_fee_pct": "Платёжная комиссия, %",
    "payment_fee_fixed": "Платёжная комиссия, фикс. за заказ",
    "fx_fee_pct": "Конвертация / международные платежи, %",
    "return_rate": "Доля возвратов, %",
    "return_cost": "Потеря на один возврат",
    "cac_cons": "CAC — консервативный",
    "cac_base": "CAC — базовый",
    "cac_opt": "CAC — оптимистичный",
    "orders_cons": "Заказов в месяц — консервативно",
    "orders_base": "Заказов в месяц — базово",
    "orders_opt": "Заказов в месяц — оптимистично",
    "batch_size": "Стартовая партия, шт.",
    "lead_time_days": "Срок до начала продаж, дней",
    "target_profit_month": "Требуемая прибыль в месяц",
}

PCT_FIELDS = {"vat_rate", "discount_pct", "duty_pct", "defect_pct", "payment_fee_pct", "fx_fee_pct", "return_rate"}


def default_market(market: str, hourly_rate: float | None = None) -> dict:
    """Шаблон допущений. Каждое значение — ДОПУЩЕНИЕ, пока не заменено наблюдением/котировкой."""
    cur = config.MARKET_CURRENCY[market]
    if market == "USA":
        d = dict(price=None, price_includes_vat=False, vat_rate=0.0, discount_pct=5, shipping_charged=0,
                 unit_cost=None, unit_cost_currency="USD", duty_pct=0, inbound_per_unit=0, packaging=1.0,
                 labor_minutes=5, hourly_rate=hourly_rate or 15, defect_pct=3, outbound_shipping=5.0,
                 payment_fee_pct=2.9, payment_fee_fixed=0.30, fx_fee_pct=0, return_rate=5, return_cost=8,
                 cac_cons=None, cac_base=None, cac_opt=None, orders_cons=None, orders_base=None, orders_opt=None,
                 batch_size=50, lead_time_days=21, target_profit_month=0,
                 fixed_monthly=[{"name": "Shopify (тариф Basic, проверить)", "amount": 39, "currency": "USD"},
                                {"name": "Приложения Shopify", "amount": 20, "currency": "USD"}],
                 one_time=[{"name": "Образцы", "amount": 100, "currency": "USD"},
                           {"name": "Фото и карточки", "amount": 150, "currency": "USD"}])
    else:
        d = dict(price=None, price_includes_vat=True, vat_rate=5.0, discount_pct=5, shipping_charged=0,
                 unit_cost=None, unit_cost_currency="USD", duty_pct=5, inbound_per_unit=0, packaging=4.0,
                 labor_minutes=5, hourly_rate=round((hourly_rate or 15) * config.AED_PER_USD, 2), defect_pct=3,
                 outbound_shipping=20.0, payment_fee_pct=2.9, payment_fee_fixed=1.0, fx_fee_pct=1.0,
                 return_rate=5, return_cost=30, cac_cons=None, cac_base=None, cac_opt=None,
                 orders_cons=None, orders_base=None, orders_opt=None, batch_size=50, lead_time_days=21,
                 target_profit_month=0,
                 fixed_monthly=[{"name": "Shopify (тариф Basic, проверить)", "amount": 39, "currency": "USD"},
                                {"name": "Приложения Shopify", "amount": 20, "currency": "USD"}],
                 one_time=[{"name": "Образцы", "amount": 100, "currency": "USD"},
                           {"name": "Фото и карточки", "amount": 150, "currency": "USD"}])
    d["currency"] = cur
    d["_meta"] = {k: {"type": "assumption", "source": "Шаблон приложения — требует проверки"}
                  for k in d if not k.startswith("_") and d[k] is not None}
    return d


def default_inputs(hourly_rate: float | None = None) -> dict:
    return {
        "base_currency": "USD",
        "fx": {"AED_per_USD": config.AED_PER_USD, "source": config.FX_SOURCE},
        "scenario_mod": {
            "price_factor": {"cons": 0.92, "base": 1.0, "opt": 1.05},
            "return_factor": {"cons": 1.5, "base": 1.0, "opt": 0.75},
            "extra_lead_days": {"cons": 14, "base": 0, "opt": 0},
        },
        "markets": {m: default_market(m, hourly_rate) for m in config.MARKETS},
    }


def convert(amount: float, from_cur: str, to_cur: str, aed_per_usd: float = config.AED_PER_USD) -> float:
    if amount is None:
        return None
    if from_cur == to_cur:
        return float(amount)
    if from_cur == "USD" and to_cur == "AED":
        return float(amount) * aed_per_usd
    if from_cur == "AED" and to_cur == "USD":
        return float(amount) / aed_per_usd
    raise ValueError(f"Неизвестная валюта: {from_cur}->{to_cur}")


def _num(v, default=0.0):
    if v is None or v == "":
        return default
    return float(v)


def per_order(m: dict, scenario: str, mod: dict, aed_per_usd: float) -> dict:
    """Юнит-экономика одного заказа в валюте рынка."""
    cur = m["currency"]
    pf = mod["price_factor"][scenario]
    rf = mod["return_factor"][scenario]
    vat = _num(m.get("vat_rate")) / 100 if m.get("price_includes_vat") else 0.0

    price = _num(m["price"]) * pf
    net_price = price * (1 - _num(m.get("discount_pct")) / 100)
    shipping_charged = _num(m.get("shipping_charged"))
    gross_charged = net_price + shipping_charged          # что платит покупатель (вкл. VAT для ОАЭ)
    R = gross_charged / (1 + vat)                         # выручка без VAT
    vat_collected = gross_charged - R

    unit_cost = convert(_num(m["unit_cost"]), m.get("unit_cost_currency") or cur, cur, aed_per_usd)
    landed = unit_cost * (1 + _num(m.get("duty_pct")) / 100) + _num(m.get("inbound_per_unit"))
    defect = min(_num(m.get("defect_pct")) / 100, 0.9)
    goods_cost = landed / (1 - defect)                    # брак увеличивает стоимость годной единицы
    labor = _num(m.get("labor_minutes")) / 60 * _num(m.get("hourly_rate"))
    packaging = _num(m.get("packaging"))
    outbound = _num(m.get("outbound_shipping"))
    payment = gross_charged * _num(m.get("payment_fee_pct")) / 100 + _num(m.get("payment_fee_fixed"))
    fx_fee = R * _num(m.get("fx_fee_pct")) / 100
    return_rate = min(_num(m.get("return_rate")) / 100 * rf, 1.0)
    returns = return_rate * _num(m.get("return_cost"))

    V = goods_cost + labor + packaging + outbound + payment + fx_fee + returns
    CM1 = R - V
    cac = _num(m.get(f"cac_{scenario}"), _num(m.get("cac_base")))
    CM2 = CM1 - cac
    return {
        "currency": cur,
        "price": price, "net_price": net_price, "gross_charged": gross_charged,
        "vat_collected": vat_collected, "R": R,
        "unit_cost": unit_cost, "landed_unit": landed, "goods_cost": goods_cost,
        "labor": labor, "packaging": packaging, "outbound": outbound, "payment": payment,
        "fx_fee": fx_fee, "returns": returns, "V": V, "CM1": CM1, "CAC": cac, "CM2": CM2,
        "margin_after_ads": (CM2 / R) if R > 0 else None,
        "gross_margin": ((R - goods_cost) / R) if R > 0 else None,
        "markup": ((R - goods_cost) / goods_cost) if goods_cost > 0 else None,
        "breakeven_cac": CM1 if CM1 > 0 else None,
        "breakeven_roas": (R / CM1) if CM1 > 0 else None,
    }


def _sum_items(items, cur, aed_per_usd):
    return sum(convert(_num(i.get("amount")), i.get("currency") or cur, cur, aed_per_usd) for i in (items or []))


def monthly(m: dict, scenario: str, mod: dict, aed_per_usd: float) -> dict:
    u = per_order(m, scenario, mod, aed_per_usd)
    cur = m["currency"]
    orders = _num(m.get(f"orders_{scenario}"), _num(m.get("orders_base")))
    fixed = _sum_items(m.get("fixed_monthly"), cur, aed_per_usd)
    one_time = _sum_items(m.get("one_time"), cur, aed_per_usd)
    target = _num(m.get("target_profit_month"))
    op_profit = orders * u["CM2"] - fixed
    breakeven_orders = math.ceil(fixed / u["CM2"]) if u["CM2"] > 0 else None
    target_cac = (u["CM1"] - (fixed + target) / orders) if orders > 0 else None
    batch = int(_num(m.get("batch_size")))
    inventory_cost = batch * u["landed_unit"]
    lead_days = _num(m.get("lead_time_days")) + mod["extra_lead_days"][scenario]
    cash = cashflow_90d(u, orders, fixed, one_time, batch, lead_days)
    startup_need = cash["peak_cash_need"]
    payback = (startup_need / op_profit) if op_profit > 0 else None
    return {
        "per_order": u, "orders": orders, "fixed_monthly": fixed, "one_time": one_time,
        "operating_profit": op_profit, "breakeven_orders": breakeven_orders, "target_cac": target_cac,
        "inventory_cost": inventory_cost, "lead_days": lead_days, "cashflow": cash,
        "startup_cash_need": startup_need, "payback_months": payback,
        "hours_per_month": orders * _num(m.get("labor_minutes")) / 60,
    }


def cashflow_90d(u: dict, orders_month: float, fixed: float, one_time: float, batch: int, lead_days: float) -> dict:
    """Помесячный денежный поток на 3 месяца. Партия оплачивается сразу; дозаказ — когда остаток заканчивается.
    Продажи начинаются после lead_days. Реклама и постоянные расходы идут с первого месяца продаж
    (постоянные — с первого месяца)."""
    months = []
    cash = -one_time
    inventory = 0
    bought_units = 0
    peak_need = -cash
    for mi in range(3):
        start_day, end_day = mi * 30, (mi + 1) * 30
        active_days = max(0.0, min(end_day, 90) - max(start_day, lead_days))
        sales = round(orders_month * active_days / 30)
        defect_share = (u["goods_cost"] - u["landed_unit"]) / u["goods_cost"] if u["goods_cost"] else 0
        units_used = math.ceil(sales / (1 - defect_share)) if sales else 0
        purchase = 0.0
        if mi == 0 or inventory < units_used:
            shortage = max(0, units_used - inventory)
            need_units = math.ceil(shortage / batch) * batch if batch > 0 else shortage
            if mi == 0:
                need_units = max(batch, need_units)
            purchase = need_units * u["landed_unit"]
            inventory += need_units
            bought_units += need_units
        inventory -= units_used
        cash_in = sales * u["R"]
        var_ex_goods = u["V"] - u["goods_cost"]
        before_receipts = purchase + sales * var_ex_goods + sales * u["CAC"] + fixed
        peak_need = max(peak_need, -(cash - before_receipts))
        cash_out = before_receipts
        cash += cash_in - cash_out
        peak_need = max(peak_need, -cash)
        months.append({"month": mi + 1, "sales": sales, "purchase": purchase, "cash_in": cash_in,
                       "cash_out": cash_out, "cumulative": cash, "inventory_left": inventory})
    return {"months": months, "end_cash": cash, "peak_cash_need": max(peak_need, 0.0),
            "timing_assumption": "Закупка, реклама и расходы месяца оплачены до получения выручки. Уточните отсрочки платежей для своего бизнеса.",
            "unsold_units": inventory, "unsold_value": inventory * u["landed_unit"], "bought_units": bought_units}


def missing_fields(m: dict) -> list[str]:
    missing = [f for f in REQUIRED if m.get(f) in (None, "")]
    if m.get("startup_unknown"):
        missing.append("startup_costs")
    return missing


def compute(inputs: dict) -> dict:
    """Полный расчёт по обоим рынкам и сценариям + сравнение в базовой валюте."""
    inputs = deepcopy(inputs)
    aed = _num(inputs.get("fx", {}).get("AED_per_USD"), config.AED_PER_USD)
    base_cur = inputs.get("base_currency", "USD")
    mod = inputs["scenario_mod"]
    out = {"base_currency": base_cur, "fx": inputs.get("fx"), "markets": {}}
    for mk, m in inputs["markets"].items():
        invalid = []
        for field,value in m.items():
            if field not in FIELD_LABELS or field in ("unit_cost_currency","price_includes_vat") or value in (None,""):
                continue
            try:
                number = float(value)
                if not math.isfinite(number) or number < 0 or (field in PCT_FIELDS and number > 100) or (field=="defect_pct" and number>=100):
                    invalid.append(FIELD_LABELS[field])
            except (TypeError,ValueError):
                invalid.append(FIELD_LABELS[field])
        if invalid:
            out["markets"][mk] = {"status":"insufficient","missing":[],"missing_labels":["Некорректное значение — "+v for v in invalid]}
            continue
        miss = missing_fields(m)
        if miss:
            out["markets"][mk] = {"status": "insufficient", "missing": miss,
                                  "missing_labels": [FIELD_LABELS.get(f,"Разовые расходы выбранного производства") for f in miss]}
            continue
        res = {"status": "ok", "currency": m["currency"], "scenarios": {}}
        for sc in SCENARIOS:
            r = monthly(m, sc, mod, aed)
            r["in_base"] = {k: convert(r[k], m["currency"], base_cur, aed) if r[k] is not None else None
                            for k in ("operating_profit", "startup_cash_need", "fixed_monthly")}
            r["in_base"]["CM2"] = convert(r["per_order"]["CM2"], m["currency"], base_cur, aed)
            r["in_base"]["R"] = convert(r["per_order"]["R"], m["currency"], base_cur, aed)
            res["scenarios"][sc] = r
        base = res["scenarios"]["base"]
        notes = []
        if base["per_order"]["CM1"] <= 0:
            notes.append("Маржинальный доход до рекламы (CM1) не положителен: рост числа заказов только "
                         "увеличит убыток. Нужно менять цену, себестоимость или модель.")
        elif base["per_order"]["CM2"] <= 0:
            notes.append("После рекламы (CM2) заказ убыточен: CAC выше безубыточного. Рост заказов сам по себе "
                         "проблему не решит — нужен дешевле канал, выше чек или ниже затраты.")
        res["notes"] = notes
        out["markets"][mk] = res
    variants = {}
    for mk,options in inputs.get("variants",{}).items():
        variants[mk] = {}
        for key,market in options.items():
            result = compute({**inputs,"variants":{},"markets":{mk:market}})
            variants[mk][key] = result["markets"][mk]
    out["variants"] = variants
    return out


def apply_changes(inputs: dict, market: str | None, changes: dict) -> tuple[dict, list[str]]:
    """Применяет правки параметров (из сайта или из уточнения). Возвращает новые входы и описание правок."""
    new = deepcopy(inputs)
    log = []
    targets = [market] if market in config.MARKETS else list(config.MARKETS)
    for mk in targets:
        m = new["markets"][mk]
        for k, v in changes.items():
            if k not in FIELD_LABELS:
                continue
            old = m.get(k)
            if k in ("price_includes_vat",):
                v = bool(v)
            elif k == "unit_cost_currency":
                v = str(v).upper()
            elif v not in (None, ""):
                v = float(v)
                if not math.isfinite(v) or v < 0 or (k in PCT_FIELDS and v>100) or (k=="defect_pct" and v>=100):
                    raise ValueError(f"Проверьте значение — {FIELD_LABELS[k]}")
            else:
                v = None
            if old != v:
                m[k] = v
                m.setdefault("_meta", {})[k] = {"type": "assumption", "source": "Задано владельцем"}
                log.append(f"{config.MARKET_LABEL[mk]}: {FIELD_LABELS[k]} {old} → {v}")
    return new, log

