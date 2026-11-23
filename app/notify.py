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


def send(user_id: int, text: str, dedupe_key: str, idea_id: int | None = None, url_button: tuple | None = None,
         buttons: list | None = None):
    chat = _chat(user_id)
    if not chat or not config.TELEGRAM_BOT_TOKEN:
        return None
    if not _claim(dedupe_key, user_id, idea_id):
        return None  # уже отправляли
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if url_button:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": url_button[0], "url": url_button[1]}]]}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        try:
            msg = _api("sendMessage", payload)
        except RuntimeError as e:
            if buttons and "url" in str(e).lower():  # без публичного адреса оставляем только кнопки-действия
                kept = [[b for b in row if "url" not in b] for row in buttons]
                payload["reply_markup"] = {"inline_keyboard": [row for row in kept if row]}
                msg = _api("sendMessage", payload)
            elif url_button and "url" in str(e).lower():  # Telegram не принимает адрес (например, без домена)
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
        ver = conn.execute("SELECT report, scoring FROM idea_versions WHERE idea_id=? AND version=?",
                           (idea_id, version)).fetchone()
    report = db.jload(ver["report"], {}) if ver else {}
    sc = db.jload(ver["scoring"], {}) if ver else {}
    if report.get("deep"):
        head = "🔬 Глубокий разбор (Opus) готов"
    demo = "\n⚠️ ДЕМО-данные (тестовый режим), не настоящие." if idea["is_demo"] else ""
    origin = " · идея дня" if idea["origin"] == "daily" else ""
    score = f"{idea['score']:.1f}/10" if idea["score"] is not None else "—"
    link = idea_link(idea_id)
    text = (f"{head} · Идея #{idea_id}{origin}\n<b>{html.escape(idea['title'] or '')}</b>\n\n"
            f"Оценка: <b>{score}</b>\nВывод: {html.escape(idea['verdict'] or '—')}\n"
            f"Первый рынок: {config.MARKET_LABEL.get(idea['market'], idea['market'])}{demo}\n"
            f"{explain(sc, report)}\n"
            f"🔗 <a href=\"{html.escape(link)}\">Открыть полный разбор</a>\n\n"
            f"<i>Ответьте на это сообщение, чтобы уточнить идею.</i>")
    send(user_id, text, f"ready:{idea_id}:v{version}", idea_id, ("📊 Полный разбор", link))


def _cut(s, n=200):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def explain(sc: dict, report: dict) -> str:
    """Короткое «почему» для карточки в Telegram: из оценки и обязательных условий, без новых запросов."""
    per = sc.get("per_market") or {}
    if not per:
        return ""
    lines = ["", "По рынкам: " + " · ".join(
        f"{config.MARKET_LABEL[mk]} {per[mk]['score']:.1f}" for mk in config.MARKETS if mk in per)]
    best = per.get(sc.get("market")) or max(per.values(), key=lambda p: p.get("score") or 0)
    gates = best.get("gates") or []
    verdict = sc.get("verdict")
    reasons = []
    if verdict == "reject":
        reasons = [g["note"] for g in gates if g["status"] == "fail_hard"]
    elif verdict == "improve":
        reasons = [g["note"] for g in gates if g["status"] == "fail_soft"]
    elif verdict == "insufficient":
        for g in gates:
            if g["key"] == "economics" and g["status"] == "unknown":
                reasons.append(g["note"])
        if best.get("observations", 0) < 3:
            reasons.append(f"мало подтверждённых данных: {best.get('observations', 0)} наблюдений с источником")
    if not reasons:
        mr = (report.get("market") or {}).get("market_reasoning")
        if mr:
            reasons = [mr]
    if reasons:
        lines.append("Почему: " + html.escape(_cut("; ".join(reasons), 260)))
    mrep = report.get("market") or {}
    if mrep.get("main_risk"):
        lines.append("Риск: " + html.escape(_cut(mrep["main_risk"], 180)))
    ns = mrep.get("next_step") or {}
    if ns.get("action"):
        lines.append("Шаг: " + html.escape(_cut(ns["action"], 160)))
    return "\n".join(lines)


def answer(user_id: int, idea_id: int, text: str, dedupe_key: str):
    send(user_id, f"💬 Идея #{idea_id}\n{html.escape(text)}", dedupe_key, idea_id,
         ("📊 Полный разбор", idea_link(idea_id)))


def origin_label(idea) -> str:
    return "идея дня, её выбрал бот" if idea["origin"] == "daily" else "ваша идея"


def ask(user_id: int, idea_id: int, question: str, dedupe_key: str):
    """Вопрос до платного исследования — с контекстом и кнопкой «Реши сам»."""
    with db.db() as conn:
        idea = conn.execute("SELECT * FROM ideas WHERE id=?", (idea_id,)).fetchone()
    if not idea:
        return
    text = (f"❓ Идея #{idea_id} «{html.escape(idea['title'] or '')}» ({origin_label(idea)})\n\n"
            f"Прежде чем тратить деньги на поиск, уточню одно:\n<b>{html.escape(question)}</b>\n\n"
            f"Ответьте на это сообщение или нажмите «Реши сам» — тогда я выберу самый разумный вариант и напишу, что предположил.")
    send(user_id, text, dedupe_key, idea_id,
         buttons=[[{"text": "🤖 Реши сам", "callback_data": f"auto:{idea_id}"}],
                  [{"text": "📊 Открыть идею", "url": idea_link(idea_id)}]])


def simple(user_id: int, text: str, dedupe_key: str, idea_id: int | None = None):
    send(user_id, html.escape(text), dedupe_key, idea_id)
