"""Расшифровка голосовых через OpenAI (необязательное подключение)."""
import httpx

from . import config, db


async def transcribe(path: str, duration_s: int, user_id: int) -> str:
    if not config.OPENAI_API_KEY:
        raise RuntimeError("Расшифровка голосовых не подключена (нужен OPENAI_API_KEY).")
    async with httpx.AsyncClient(timeout=120) as c:
        with open(path, "rb") as f:
            r = await c.post("https://api.openai.com/v1/audio/transcriptions",
                             headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                             data={"model": config.TRANSCRIBE_MODEL},
                             files={"file": ("voice.ogg", f, "audio/ogg")})
    r.raise_for_status()
    cost = max(duration_s, 1) / 60 * config.PRICE_TRANSCRIBE_PER_MIN
    with db.db() as conn:
        db.add_usage(conn, user_id, None, "openai", config.TRANSCRIBE_MODEL, 0, 0, 0, cost)
    db.source_ok("voice")
    return r.json().get("text", "").strip()
