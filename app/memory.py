"""Persistent feedback retrieval, explicit owner rules, and a reproducible context trace."""
import re

from . import db

RULES = {
    "evergreen": {"label":"Только круглогодичные товары","field":"is_seasonal","expected":False},
    "low_manual_work": {"label":"Рост без ручной работы над каждым заказом","field":"requires_per_order_handwork","expected":False},
    "not_fragile": {"label":"Исключать хрупкие товары","field":"fragile","expected":False},
    "repeat_purchase": {"label":"Нужна повторная покупка","field":"recurring_purchase","expected":True},
}


def save_rules(conn, user_id, feedback_id, keys):
    row = conn.execute("SELECT id FROM idea_feedback WHERE id=? AND user_id=?",(feedback_id,user_id)).fetchone()
    if not row:
        raise ValueError("Отзыв не найден")
    if any(key not in RULES for key in keys):
        raise ValueError("Неизвестное предпочтение")
    with db.tx(conn):
        conn.execute("DELETE FROM feedback_rules WHERE feedback_id=?",(feedback_id,))
        conn.executemany("INSERT INTO feedback_rules VALUES(?,?)",[(feedback_id,key) for key in set(keys)])


def active_rules(conn, user_id):
    result = {}
    for row in conn.execute("SELECT r.rule_key,f.id,f.comment FROM feedback_rules r JOIN idea_feedback f ON f.id=r.feedback_id "
                            "WHERE f.user_id=? AND f.remember=1 ORDER BY f.id DESC",(user_id,)):
        key = row["rule_key"]
        if key in RULES and key not in result:
            result[key] = {"key":key,"label":RULES[key]["label"],"feedback_id":row["id"]}
    return list(result.values())


def _tokens(text):
    return set(re.findall(r"[\w]{3,}",str(text).lower())) - {"это","для","что","как","with","the","and","this"}


def snapshot(conn, user_id, idea_id=None, query=""):
    settings = db.get_settings(conn,user_id)
    rows = [dict(row) for row in conn.execute("SELECT f.*,i.title FROM idea_feedback f JOIN ideas i ON i.id=f.idea_id "
        "WHERE f.user_id=? AND (f.remember=1 OR f.idea_id=?) ORDER BY f.id DESC",(user_id,idea_id or -1))]
    if idea_id and not query:
        idea = db.get_idea(conn,idea_id,user_id)
        query = ((idea["title"] or "")+" "+(idea["input_text"] or "")) if idea else ""
    rules = active_rules(conn,user_id)
    mandatory_ids = {rule["feedback_id"] for rule in rules}
    tokens = _tokens(query)
    recent_ids = {row["id"] for row in rows[:8]}
    def priority(row):
        overlap = len(tokens & _tokens(row["comment"]+" "+(row["title"] or "")))
        return (row["id"] in mandatory_ids, row["idea_id"]==idea_id, overlap>0,
                overlap,row["id"] in recent_ids,row["id"])
    picked = sorted(rows,key=priority,reverse=True)[:20]
    feedback = [{"id":row["id"],"idea_id":row["idea_id"],"version":row["version"],"title":row["title"],
        "rating":row["rating"],"reason":row["comment"],"scope":"future" if row["remember"] else "this_idea"}
        for row in picked]
    previous = []
    for idea in conn.execute("SELECT id,title,verdict FROM ideas WHERE user_id=? ORDER BY updated_at DESC LIMIT 8",(user_id,)):
        version = db.latest_version(conn,idea["id"])
        report = db.jload(version["report"],{}) if version else {}
        previous.append({"title":idea["title"],"verdict":idea["verdict"],"risk":(report.get("market") or {}).get("main_risk")})
    instructions = []
    if idea_id:
        instructions = [row["change_request"][:1000] for row in conn.execute("SELECT change_request FROM idea_versions WHERE idea_id=? "
            "AND version>1 AND change_request IS NOT NULL ORDER BY version DESC LIMIT 5",(idea_id,))]
    return {"captured_at":db.now_iso(),"market_priority":settings["market_priority"],"evergreen_first":settings["evergreen_first"],
            "feedback":feedback,"rules":rules,"previous_analyses":previous,"previous_instructions":instructions,
            "active_feedback_total":len(rows),"retrieval":"relevant_and_recent_v2"}


def block(value):
    # A stable snapshot remains associated with the version that actually used it.
    return "Предпочтения и обратная связь владельца. " + db.jdump({k:v for k,v in value.items() if k!="captured_at"})


def check_rules(market, rules):
    attributes = market.get("product_attributes") or {}
    season = market.get("seasonality") or {}
    out = []
    for rule in rules:
        definition = RULES.get(rule["key"])
        if not definition:
            continue
        field = definition["field"]
        value = season.get(field) if field=="is_seasonal" else attributes.get(field)
        status = "unknown" if not isinstance(value,bool) else "ok" if value is definition["expected"] else "fail_soft"
        out.append({"key":"preference:"+rule["key"],"label":definition["label"],"status":status,
                    "note":f"Обязательное предпочтение из отзыва #{rule['feedback_id']}. Характеристика товара оценивается моделью и требует подтверждения.",
                    "feedback_id":rule["feedback_id"]})
    return out
