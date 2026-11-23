from app import economics, scoring


def filled(market="USA", **over):
    inp = economics.default_inputs(15)
    m = inp["markets"][market]
    m.update(price=30, unit_cost=8, cac_cons=12, cac_base=8, cac_opt=5, orders_cons=20, orders_base=40, orders_opt=60)
    m.update(over)
    return inp


def test_missing_required_gives_insufficient():
    res = economics.compute(economics.default_inputs())
    for mk in ("USA", "UAE"):
        assert res["markets"][mk]["status"] == "insufficient"


def test_formulas_usa():
    res = economics.compute(filled())
    u = res["markets"]["USA"]["scenarios"]["base"]["per_order"]
    # R = 30 * 0.95 (скидка 5%), без VAT
    assert abs(u["R"] - 28.5) < 1e-9
    assert abs(u["CM1"] - (u["R"] - u["V"])) < 1e-9
    assert abs(u["CM2"] - (u["CM1"] - 8)) < 1e-9
    assert abs(u["margin_after_ads"] - u["CM2"] / u["R"]) < 1e-9
    # маржа и наценка не перепутаны
    assert abs(u["gross_margin"] - (u["R"] - u["goods_cost"]) / u["R"]) < 1e-9
    assert abs(u["markup"] - (u["R"] - u["goods_cost"]) / u["goods_cost"]) < 1e-9
    assert u["markup"] > u["gross_margin"]


def test_uae_vat_excluded_from_revenue():
    inp = filled("UAE", price=105, unit_cost=8, unit_cost_currency="USD", discount_pct=0, cac_base=20)
    u = economics.compute(inp)["markets"]["UAE"]["scenarios"]["base"]["per_order"]
    assert abs(u["R"] - 100) < 1e-9          # 105 с VAT 5% → 100 выручки
    assert abs(u["vat_collected"] - 5) < 1e-9
    assert abs(u["unit_cost"] - 8 * 3.6725) < 1e-6  # конвертация USD→AED


def test_recalc_on_change():
    inp = filled()
    base = economics.compute(inp)["markets"]["USA"]["scenarios"]["base"]
    new, log = economics.apply_changes(inp, "USA", {"price": 40, "cac_base": 10, "batch_size": 100, "labor_minutes": 20})
    after = economics.compute(new)["markets"]["USA"]["scenarios"]["base"]
    assert log and after["per_order"]["R"] > base["per_order"]["R"]
    assert after["per_order"]["labor"] > base["per_order"]["labor"]
    assert after["inventory_cost"] > base["inventory_cost"]
    assert after["per_order"]["CAC"] == 10


def test_negative_economics_not_positive_verdict():
    inp = filled(price=10, unit_cost=12)
    econ = economics.compute(inp)
    assert econ["markets"]["USA"]["scenarios"]["base"]["per_order"]["CM1"] < 0
    assert econ["markets"]["USA"]["notes"]
    report = {"markets": {"USA": {"scores": {k: {"score": 10} for k in ("demand", "intent", "competition", "feasibility")},
                                  "demand": {"signals": [{"type": "observation", "source_url": "https://x"}] * 6}}}}
    sc = scoring.evaluate(report, econ, {"budget_start": 100000, "hours_per_week": 40})
    pm = sc["per_market"]["USA"]
    assert pm["verdict"] == "reject" and pm["score"] <= 3
    assert sc["verdict"] not in ("test",)


def test_missing_data_not_positive():
    econ = economics.compute(economics.default_inputs())
    report = {"markets": {mk: {"scores": {k: {"score": 9} for k in ("demand", "intent", "competition", "feasibility")}}
                          for mk in ("USA", "UAE")}}
    sc = scoring.evaluate(report, econ, {})
    assert sc["verdict"] == "insufficient" and sc["market"] == "none" and sc["score"] <= 5


def test_missed_season_fails():
    from datetime import date, timedelta
    econ = economics.compute(filled())
    peak = (date.today() + timedelta(days=10)).isoformat()
    report = {"markets": {"USA": {"seasonality": {"is_seasonal": True, "next_peak_date": peak},
                                  "scores": {k: {"score": 9} for k in ("demand", "intent", "competition", "feasibility")},
                                  "demand": {"signals": [{"type": "observation", "source_url": "https://x"}] * 6}}}}
    sc = scoring.evaluate(report, econ, {"budget_start": 100000})
    gates = {g["key"]: g for g in sc["per_market"]["USA"]["gates"]}
    assert gates["season"]["status"] == "fail_hard"
    assert sc["per_market"]["USA"]["verdict"] == "reject"


def test_market_changes_with_conditions():
    inp = filled()
    um = inp["markets"]["UAE"]
    um.update(price=200, unit_cost=8, cac_cons=20, cac_base=15, cac_opt=10, orders_cons=20, orders_base=40, orders_opt=60)
    from app import db
    sig = [{"type":"observation","verified":True,"scope":"product","retrieved_at":db.now_iso(),"source_url":f"https://source{i}.example/item"} for i in range(6)]
    rep = {"markets": {mk: {"scores": {k: {"score": 7} for k in ("demand", "intent", "competition", "feasibility")},
                            "demand": {"signals": sig}, "launch_feasibility": {key:{"status":"ok"} for key in ("payments","legal","fulfilment","product_requirements")}} for mk in ("USA", "UAE")}}
    prof = {"budget_start": 100000, "hours_per_week": 40}
    sc1 = scoring.evaluate(rep, economics.compute(inp), prof)
    assert sc1["market"] == "UAE"  # выше маржа
    rep["markets"]["UAE"]["launch_feasibility"] = {"payments": {"status": "blocked", "note": "нет провайдера"}}
    sc2 = scoring.evaluate(rep, economics.compute(inp), prof)
    assert sc2["market"] == "USA"   # платежи в ОАЭ недоступны → рынок меняется

