"""Отправка сообщений в Telegram из воркера (Bot API напрямую). Защита от дублей по dedupe_key."""
import html
import sqlite3

import httpx

from . import config, db

API = "https://api.telegram.org/bot{token}/{method}"


def _api(method: str, payload: dict) -> dict:
    if not config.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан")
    r = httpx.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=payload, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram error"))
    return data["result"]


def _chat(user_id: int):
    with db.db() as conn:
        row = conn.execute("SELECT tg_chat_id FROM users WHERE id=?", (user_id,)).fetchone()
    return row["tg_chat_id"] if row else None


def _claim(dedupe_key: str, user_id: int, idea_id: int | None) -> bool:
    try:
        with db.db() as conn:
            conn.execute("INSERT INTO notifications(dedupe_key,user_id,idea_id,sent_at) VALUES(?,?,?,?)",
                         (dedupe_key, user_id, idea_id, db.now_iso()))
        return True
    except sqlite3.IntegrityError:
        return False


def _release(dedupe_key: str):
    with db.db() as conn:
        conn.execute("DELETE FROM notifications WHERE dedupe_key=?", (dedupe_key,))


def send(user_id: int, text: str, dedupe_key: str, idea_id: int | None = None, url_button: tuple | None = None):
    chat = _chat(user_id)
    if not chat or not config.TELEGRAM_BOT_TOKEN:
        return None
    if not _claim(dedupe_key, user_id, idea_id):
        return None  # уже отправляли
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if url_button:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": url_button[0], "url": url_button[1]}]]}
    try:
        try:
            msg = _api("sendMessage", payload)
        except RuntimeError as e:
            if url_button and "url" in str(e).lower():  # Telegram не принимает адрес (например, без домена)
                payload.pop("reply_markup", None)
                payload["text"] = text + f"\n\n{html.escape(url_button[1])}"
                msg = _api("sendMessage", payload)
            else:
                raise
    except Exception as e:
        _release(dedupe_key)
        db.log_error("notify", f"{dedupe_key}: {e}")
        return None
    with db.db() as conn:
        conn.execute("UPDATE notifications SET tg_message_id=? WHERE dedupe_key=?", (msg["message_id"], dedupe_key))
        if idea_id:
            conn.execute("INSERT OR IGNORE INTO tg_message_map(chat_id,message_id,idea_id) VALUES(?,?,?)",
                         (chat, msg["message_id"], idea_id))
    return msg["message_id"]


def idea_link(idea_id: int) -> str:
    return f"{config.SITE_URL}/ideas/{idea_id}"


def idea_ready(user_id: int, idea_id: int, version: int, updated: bool = False):
    with db.db() as conn:
        idea = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
    if not idea:
        return
    head = "🔄 Разбор обновлён" if updated else ("✅ Разбор готов" if idea["status"] == "ready" else "🟡 Разбор готов частично")
    with db.db() as conn:
        ver = conn.execute("SELECT report FROM idea_versions WHERE idea_id=? AND version=?", (idea_id, version)).fetchone()
    if ver and db.jload(ver["report"], {}).get("deep"):
        head = "🔬 Глубокий разбор (Opus) готов"
    demo = "\n⚠️ ДЕМО-данные (тестовый режим), не настоящие." if idea["is_demo"] else ""
    origin = " · идея дня" if idea["origin"] == "daily" else ""
    score = f"{idea['score']:.1f}/10" if idea["score"] is not None else "—"
    text = (f"{head} · Идея #{idea_id}{origin}\n<b>{html.escape(idea['title'] or '')}</b>\n\n"
            f"Оценка: <b>{score}</b>\nВывод: {html.escape(idea['verdict'] or '—')}\n"
            f"Первый рынок: {config.MARKET_LABEL.get(idea['market'], idea['market'])}{demo}\n\n"
            f"<i>Ответьте на это сообщение, чтобы уточнить идею.</i>")
    send(user_id, text, f"ready:{idea_id}:v{version}", idea_id, ("📊 Полный разбор", idea_link(idea_id)))


def answer(user_id: int, idea_id: int, text: str, dedupe_key: str):
    send(user_id, f"💬 Идея #{idea_id}\n{html.escape(text)}", dedupe_key, idea_id,
         ("📊 Полный разбор", idea_link(idea_id)))


def simple(user_id: int, text: str, dedupe_key: str, idea_id: int | None = None):
    send(user_id, html.escape(text), dedupe_key, idea_id)
