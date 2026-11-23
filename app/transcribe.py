"""Расшифровка голосовых через OpenAI (необязательное подключение)."""
import httpx
import uuid

from . import config, db, llm


async def transcribe(path: str, duration_s: int, user_id: int) -> str:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("Расшифровка голосовых не подключена (нужен OPENAI_API_KEY).")
    cost = max(duration_s, 1) / 60 * config.PRICE_TRANSCRIBE_PER_MIN
    with db.db() as conn:
        settings = db.get_settings(conn,user_id)
    if cost > settings["per_idea_budget_usd"]:
        raise llm.BudgetExceeded("Голосовое сообщение превышает лимит исследования")
    reservation = f"voice:{user_id}:{uuid.uuid4().hex}"
    ctx = llm.Ctx(user_id,monthly_limit=settings["monthly_budget_usd"])
    llm.reserve(ctx,cost,reservation)
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            with open(path, "rb") as f:
                r = await c.post("https://api.openai.com/v1/audio/transcriptions",
                                 headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                                 data={"model": config.TRANSCRIBE_MODEL},
                                 files={"file": ("voice.ogg", f, "audio/ogg")})
        r.raise_for_status()
        with db.db() as conn,db.tx(conn):
            db.add_usage(conn,user_id,None,"openai",config.TRANSCRIBE_MODEL,0,0,0,cost,"voice")
            conn.execute("DELETE FROM budget_reservations WHERE reservation_key=?",(reservation,))
    finally:
        llm.release(reservation)
    db.source_ok("voice")
    return r.json().get("text", "").strip()

