"""Bounded, redacted runtime diagnostics for the application's own deployment log."""
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, credentials, db


def redact(text, user_id):
    value = str(text or "")
    secrets = [config.ADMIN_PASSWORD,config.SECRET_KEY,config.ANTHROPIC_API_KEY,
               config.TELEGRAM_BOT_TOKEN,config.OPENAI_API_KEY]
    for fields in credentials.FIELDS.values():
        for name,_ in fields:
            try:
                secrets.append(credentials.get(user_id,name))
            except ValueError:
                pass
    for secret in secrets:
        if secret and len(secret)>4:
            value = value.replace(secret,"[redacted]")
    return re.sub(r"([?&](?:key|api_key|token|password)=)[^\s&]+",r"\1[redacted]",value,flags=re.I)


def snapshot():
    with db.db() as conn:
        user = conn.execute("SELECT id,tg_chat_id FROM users WHERE email=?",(config.ADMIN_EMAIL,)).fetchone()
        if not user:
            return {"error":"owner_not_found"}
        uid=user["id"]
        settings=db.get_settings(conn,uid)
        ideas=[dict(row) for row in conn.execute("SELECT id,origin,title,status,stage,current_version FROM ideas WHERE user_id=? ORDER BY id",(uid,))]
        jobs=[dict(row) for row in conn.execute("SELECT id,idea_id,kind,status,error,created_at,finished_at FROM jobs WHERE user_id=? ORDER BY id DESC LIMIT 20",(uid,))]
        failures=[]
        for row in conn.execute("SELECT place,message,created_at FROM error_log ORDER BY id DESC LIMIT 8"):
            # Only stack locations and final exception lines; API payloads and prompts are excluded.
            lines=[line.strip() for line in row["message"].splitlines() if line.strip().startswith(('File ', 'ValueError:', 'ResponseTooLong:', 'JSONDecodeError:', 'BudgetExceeded:', 'RuntimeError:', 'TypeError:', 'KeyError:'))]
            failures.append({"place":row["place"],"at":row["created_at"],"trace":redact("\n".join(lines[-8:]),uid)[:1800]})
        for row in jobs:
            row["error"]=redact(row.get("error"),uid)[:400]
        totals=dict(conn.execute("SELECT COALESCE(SUM(cost_usd),0) cost,COALESCE(SUM(searches),0) searches FROM usage WHERE user_id=?",(uid,)).fetchone())
        profile=db.get_profile(conn,uid)
        calls=[]
        def tool_info(value, info):
            if isinstance(value,dict):
                kind=value.get("type")
                if kind:
                    info["types"][kind]=info["types"].get(kind,0)+1
                for key in ("error_code","code"):
                    if value.get(key):
                        info["errors"].append(redact(value[key],uid)[:180])
                for item in value.values():
                    if isinstance(item,(dict,list)):
                        tool_info(item,info)
            elif isinstance(value,list):
                for item in value:
                    tool_info(item,info)
        for row in conn.execute("SELECT c.job_id,c.step,c.result FROM step_cache c JOIN jobs j ON j.id=c.job_id WHERE j.user_id=? "
            "AND c.step LIKE '%:pending' ORDER BY c.job_id DESC LIMIT 5",(uid,)):
            pending=db.jload(row["result"],{})
            info={"job_id":row["job_id"],"step":row["step"],"stop_reason":pending.get("stop_reason"),
                "searches_used":pending.get("searches_used"),"source_count":len(pending.get("sources") or []),"types":{},"errors":[]}
            tool_info(pending.get("messages"),info)
            calls.append(info)
    return {"utc":datetime.now(timezone.utc).isoformat(),"new_york":datetime.now(ZoneInfo("America/New_York")).isoformat(),
        "telegram_connected":bool(user["tg_chat_id"]),"schedule":{key:settings.get(key) for key in
            ("daily_enabled","daily_time","timezone","per_idea_budget_usd","monthly_budget_usd","require_analytics")},
        "profile_fields_filled":[key for key,value in profile.items() if value not in (None,"")],
        "ideas":ideas,"jobs":jobs,"failures":failures,"totals":totals,"recent_call_status":calls}


if __name__=="__main__":
    print("APP_DIAGNOSTICS "+json.dumps(snapshot(),ensure_ascii=False))
