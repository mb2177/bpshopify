"""Telegram-бот: только ввод идей и уведомления. Все разборы — на сайте."""
import asyncio
import logging
import re
import sqlite3

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update)

from . import config, db, jobs
from .web.auth import ensure_admin, make_login_token

log = logging.getLogger("bot")
URL_RE = re.compile(r"https?://\S+")
IDEA_HINTS = ("провер", "иде", "товар", "продава", "ниш", "магазин", "бизнес", "запуст", "футбол", "сделать",
              "производ", "кастом", "персонал", "подар")

dp = Dispatcher()
album_tasks: dict[str, asyncio.Task] = {}
BOT_USERNAME = ""


# ---------- инфраструктура ----------

class DedupeMiddleware(BaseMiddleware):
    """Повторно доставленное событие Telegram не обрабатывается второй раз."""
    async def __call__(self, handler, event: Update, data):
        item = event.message or event.callback_query
        actor = getattr(item, "from_user", None)
        if actor is None or actor.is_bot:
            return None
        try:
            with db.db() as conn:
                conn.execute("INSERT INTO tg_updates(update_id,received_at) VALUES(?,?)", (event.update_id, db.now_iso()))
        except sqlite3.IntegrityError:
            return None
        return await handler(event, data)


dp.update.outer_middleware(DedupeMiddleware())


def owner_of(chat_id: int):
    with db.db() as conn:
        row = conn.execute("SELECT id FROM users WHERE tg_chat_id=?", (chat_id,)).fetchone()
    return row["id"] if row else None


async def guard(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return None
    uid = owner_of(message.chat.id)
    if not uid:
        await message.answer("🔒 Доступ закрыт. Владелец привязывает бота кодом с сайта: Настройки → Telegram → "
                             "отправьте сюда /start КОД.")
    return uid


def map_msg(chat_id: int, message_id: int, idea_id: int):
    with db.db() as conn:
        conn.execute("INSERT OR IGNORE INTO tg_message_map(chat_id,message_id,idea_id) VALUES(?,?,?)",
                     (chat_id, message_id, idea_id))


def pending_put(key: str, user_id: int, data: dict):
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO tg_pending(key,user_id,data,created_at) VALUES(?,?,?,?)",
                     (key, user_id, db.jdump(data), db.now_iso()))


def pending_take(key: str, user_id: int):
    """Забирает черновик один раз (повторное нажатие кнопки ничего не создаст)."""
    with db.db() as conn:
        with db.tx(conn):
            row = conn.execute("SELECT data FROM tg_pending WHERE key=? AND user_id=?", (key, user_id)).fetchone()
            if not row:
                return None
            conn.execute("DELETE FROM tg_pending WHERE key=?", (key,))
    return db.jload(row["data"], {})


def pending_get(key: str, user_id: int):
    with db.db() as conn:
        row = conn.execute("SELECT data FROM tg_pending WHERE key=? AND user_id=?", (key, user_id)).fetchone()
    return db.jload(row["data"], {}) if row else None


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(**b) for b in row] for row in rows])


def link_btn(text, url):
    # Telegram принимает URL-кнопки только с публичным адресом
    if "localhost" in url or "127.0.0.1" in url:
        return None
    return {"text": text, "url": url}


# ---------- создание идей ----------

async def start_idea(message: Message, user_id: int, text: str, media: list, dedupe: str):
    links = [{"url": u.rstrip(").,")} for u in URL_RE.findall(text or "")]
    with db.db() as conn:
        exists = conn.execute("SELECT idea_id FROM jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
        if exists:
            return
        idea_id = db.create_idea(conn, user_id, "telegram", text or "Идея по фото", links=links, media=media)
        jobs.enqueue(conn, user_id, "research", idea_id, dedupe_key=dedupe)
        title = (text or "идея по фото").strip().split("\n")[0][:70]
    extra = []
    if links:
        extra.append("ссылки разберу")
    if media:
        extra.append(f"фото: {len(media)} (по фото не подтверждаю материалы и себестоимость)")
    msg = await message.answer(
        f"📥 Идея #{idea_id}: {title}\nПроверю спрос, сравню США (USA) и ОАЭ (UAE), производство и экономику."
        f"{' ' + '; '.join(extra) + '.' if extra else ''}\nКогда разбор будет готов — пришлю оценку и ссылку.")
    map_msg(message.chat.id, msg.message_id, idea_id)
    map_msg(message.chat.id, message.message_id, idea_id)


async def start_refine(message: Message, user_id: int, idea_id: int, text: str, dedupe: str):
    busy = False
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, user_id)
        if not idea:
            await message.answer("Не нашёл эту идею.")
            return
        if is_confused(text):
            return await explain_question(message,idea)
        with db.tx(conn):
            busy = conn.execute("SELECT 1 FROM jobs WHERE idea_id=? AND status IN ('queued','running')", (idea_id,)).fetchone()
            exists = conn.execute("SELECT 1 FROM jobs WHERE dedupe_key=?", (dedupe,)).fetchone()
            if exists:
                return
            if not busy:
                jobs.enqueue(conn, user_id, "refine", idea_id, {"text": text}, dedupe_key=dedupe)
    if busy:
        await message.answer(f"Идея #{idea_id} уже обрабатывается. Результат появится в её карточке.")
        return
    msg = await message.answer(f"✏️ Идея #{idea_id}: принял уточнение. Обновлю только нужные разделы и пришлю ссылку.")
    map_msg(message.chat.id, msg.message_id, idea_id)


def looks_like_idea(text: str) -> bool:
    t = (text or "").lower()
    return bool(URL_RE.search(t)) or len(t) >= 40 or any(h in t for h in IDEA_HINTS)


def mode_key(message, uid, actor=None):
    actor = actor or message.from_user
    return f"mode:{uid}:{message.chat.id}:{actor.id}"


def intended_for_bot(message, uid):
    """Group chatter and messages from bots must never become paid research."""
    if not message.from_user or message.from_user.is_bot:
        return False
    if message.chat.type == "private":
        return True
    text = (message.text or message.caption or "").strip().lower()
    if text.startswith("/") or re.match(r"^(?:идея\s*:|проверь\b|разбери\b|проанализируй\b|оцени\b)", text):
        return True
    if BOT_USERNAME and re.search(r"@" + re.escape(BOT_USERNAME) + r"\b", text, re.I):
        return True
    if pending_get(mode_key(message,uid), uid):
        return True
    if message.reply_to_message:
        with db.db() as conn:
            return bool(conn.execute("SELECT 1 FROM tg_message_map WHERE chat_id=? AND message_id=?",
                (message.chat.id, message.reply_to_message.message_id)).fetchone())
    return False


async def download(bot: Bot, file_id: str, user_id: int, name: str) -> str:
    d = config.UPLOAD_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / name
    f = await bot.get_file(file_id)
    await bot.download_file(f.file_path, destination=path)
    return str(path)


# ---------- команды ----------

@dp.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    code = (command.args or "").strip().upper()
    uid = owner_of(message.chat.id)
    if not uid and code:
        with db.db() as conn:
            row = conn.execute("SELECT id FROM users WHERE tg_bind_code=? AND tg_chat_id IS NULL", (code,)).fetchone()
            if row:
                conn.execute("UPDATE users SET tg_chat_id=?, tg_bind_code=NULL WHERE id=?", (message.chat.id, row["id"]))
                uid = row["id"]
    if not uid:
        await message.answer("🔒 Бот закрытый. Откройте сайт → Настройки → Telegram и отправьте сюда /start КОД.")
        return
    await message.answer(
        "👋 Бот подключён.\n\nПросто пришлите идею: текст, ссылку, фото или голосовое. Например:\n"
        "«Проверь идею: наборы для регулярного ухода за комнатными растениями, рынок США».\n\n"
        "В группе начинайте с /new или «Проверь идею:», упомяните бота либо ответьте на сообщение об идее.\n"
        "Я сразу подтвержу приём, а когда разбор будет готов — пришлю оценку /10 и ссылку на полный разбор на сайте.\n"
        "Чтобы уточнить идею — ответьте (reply) на моё сообщение о ней.\n\n/help — все команды")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not await guard(message):
        return
    await message.answer(
        "Как пользоваться:\n• Пришлите идею текстом, ссылкой, фото или голосом.\n"
        "• В группе: /new, «Идея:», «Проверь…», упоминание бота или reply к сообщению об идее. Обычная переписка игнорируется.\n"
        "• Ответьте на сообщение об идее, чтобы уточнить: «а если партия 50 шт?», «найди конкурентов в Дубае», "
        "«не хотим покупать оборудование».\n\nКоманды:\n/new текст — точно запустить как новую идею (обычно можно просто написать идею)\n"
        "/ideas — последние идеи\n/status — что сейчас исследуется\n/cancel — остановить исследование\n"
        "/login — ссылка для входа на сайт без пароля (10 мин)\n/help — эта справка")


@dp.message(Command("new"))
async def cmd_new(message: Message, command: CommandObject):
    uid = await guard(message)
    if not uid:
        return
    text = (command.args or "").strip()
    if text:  # «/new идея» одним сообщением — запускаем сразу
        await start_idea(message, uid, text, [], f"tg-idea:{message.chat.id}:{message.message_id}")
        return
    pending_put(mode_key(message,uid), uid, {"mode": "new"})
    await message.answer("Жду идею: текст, ссылку, фото или голосовое.")


@dp.message(Command("login"))
async def cmd_login(message: Message):
    uid = await guard(message)
    if uid:
        tok = make_login_token(uid)
        url = f"{config.SITE_URL}/auth/tg/{tok}"
        btn = link_btn("🔑 Войти на сайт", url)
        await message.answer(f"Одноразовая ссылка входа (10 минут):\n{url}" if not btn else "Одноразовая ссылка входа (10 минут):",
                             reply_markup=kb([[btn]]) if btn else None)


@dp.message(Command("ideas"))
async def cmd_ideas(message: Message):
    uid = await guard(message)
    if not uid:
        return
    with db.db() as conn:
        rows = conn.execute("SELECT * FROM ideas WHERE user_id=? AND archived=0 ORDER BY updated_at DESC LIMIT 10",
                            (uid,)).fetchall()
    if not rows:
        await message.answer("Идей пока нет — пришлите первую.")
        return
    lines, buttons = [], []
    for r in rows:
        sc = f"{r['score']:.1f}/10" if r["score"] is not None else r["status"]
        star = "⭐ " if r["is_project"] else ""
        lines.append(f"#{r['id']} {star}{r['title'][:50]} — {sc}, {config.MARKET_LABEL.get(r['market'], '—')}")
        row = [{"text": f"✏️ Уточнить #{r['id']}", "callback_data": f"ref:{r['id']}"}]
        b = link_btn(f"📊 #{r['id']}", f"{config.SITE_URL}/ideas/{r['id']}")
        if b:
            row.insert(0, b)
        buttons.append(row)
    await message.answer("Последние идеи:\n" + "\n".join(lines) + f"\n\nВсе идеи: {config.SITE_URL}/ideas",
                         reply_markup=kb(buttons), disable_web_page_preview=True)


@dp.message(Command("status"))
async def cmd_status(message: Message):
    uid = await guard(message)
    if not uid:
        return
    with db.db() as conn:
        rows = jobs.active_jobs(conn, uid)
        stages = {r["id"]: r["stage"] for r in conn.execute("SELECT id, stage FROM ideas WHERE user_id=?", (uid,))}
    if not rows:
        await message.answer("Сейчас ничего не исследуется.")
        return
    kinds = {"research": "исследование", "refine": "уточнение", "daily": "идея дня"}
    lines = [f"• {kinds.get(r['kind'], r['kind'])} #{r['idea_id'] or '—'} {r['title'] or ''}: "
             f"{'в очереди' if r['status'] == 'queued' else (stages.get(r['idea_id']) or 'выполняется')}" for r in rows]
    await message.answer("Сейчас:\n" + "\n".join(lines))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    uid = await guard(message)
    if not uid:
        return
    with db.db() as conn:
        rows = jobs.active_jobs(conn, uid)
    if not rows:
        await message.answer("Нечего останавливать.")
        return
    await message.answer("Что остановить?", reply_markup=kb(
        [[{"text": f"⏹ #{r['idea_id'] or '—'} {(r['title'] or r['kind'])[:30]}", "callback_data": f"cancel:{r['id']}"}]
         for r in rows]))


# ---------- кнопки ----------

CONFUSED = ("о чем", "о чём", "не понял", "не понимаю", "что это", "какая идея", "что за", "какой идеи", "откуда", "зачем", "кто ты")


def is_confused(text: str) -> bool:
    """Ответ «ты о чём?» — это не ответ на вопрос, платное исследование не запускаем."""
    t = (text or "").lower().strip()
    return len(t) <= 60 and (any(c in t for c in CONFUSED) or t in ("?", "??", "что?", "а?"))


async def explain_question(message: Message, idea):
    where = ("Её выбрал сам бот в ежедневном поиске (идея дня), вы её не присылали."
             if idea["origin"] == "daily" else "Это ваша идея.")
    states = {"failed":"Предыдущий разбор не завершился. Продолжить его можно в карточке идеи.",
        "researching":"Разбор уже выполняется.", "queued":"Разбор находится в очереди.",
        "needs_input":"Комплектацию и размеры для названного товара бот выбирает самостоятельно."}
    await message.answer(
        f"Идея #{idea['id']} «{idea['title']}». {where}\n"
        f"{states.get(idea['status'], 'Результат и основания находятся в карточке идеи.')}\n"
        f"{config.SITE_URL}/ideas/{idea['id']}\nНового платного исследования это сообщение не запускает.",
        disable_web_page_preview=True)


@dp.callback_query(F.data.startswith("auto:"))
async def cb_auto(cb: CallbackQuery):
    """«Реши сам»: продолжить исследование, бот выбирает вариант и пишет допущение."""
    uid = owner_of(cb.message.chat.id)
    if not uid:
        return await cb.answer("Нет доступа")
    idea_id = int(cb.data.split(":")[1])
    with db.db() as conn:
        idea = db.get_idea(conn, idea_id, uid)
        if not idea or idea["status"] != "needs_input":
            return await cb.answer("Уже обработано")
        jobs.enqueue(conn, uid, "research", idea_id, {"auto_assume": True}, dedupe_key=f"auto:{idea_id}:{idea['updated_at']}")
        db.set_idea(conn, idea_id, status="queued", stage="Выбираю вариант сам и продолжаю исследование")
    await cb.answer("Продолжаю")
    await cb.message.edit_reply_markup(reply_markup=None)
    msg = await cb.message.answer(f"🤖 Идея #{idea_id}: выберу вариант сам и напишу в разборе, что предположил. Пришлю оценку и ссылку.")
    map_msg(cb.message.chat.id, msg.message_id, idea_id)


@dp.callback_query(F.data.startswith("cancel:"))
async def cb_cancel(cb: CallbackQuery):
    uid = owner_of(cb.message.chat.id)
    if not uid:
        return await cb.answer("Нет доступа")
    job_id = int(cb.data.split(":")[1])
    with db.db() as conn:
        ok = jobs.request_cancel(conn, job_id, uid)
    await cb.answer("Останавливаю" if ok else "Задача уже завершена")
    if ok:
        await cb.message.edit_text("⏹ Остановка запрошена. Уже сохранённое не потеряется.")


@dp.callback_query(F.data.startswith("ref:"))
async def cb_ref(cb: CallbackQuery):
    uid = owner_of(cb.message.chat.id)
    if not uid:
        return await cb.answer("Нет доступа")
    idea_id = int(cb.data.split(":")[1])
    pending_put(mode_key(cb.message,uid,cb.from_user), uid, {"mode": "refine", "idea_id": idea_id})
    await cb.answer()
    await cb.message.answer(f"Напишите уточнение к идее #{idea_id}.")


@dp.callback_query(F.data.startswith("amb:"))
async def cb_amb(cb: CallbackQuery):
    uid = owner_of(cb.message.chat.id)
    if not uid:
        return await cb.answer("Нет доступа")
    _, action, key = cb.data.split(":", 2)
    data = pending_take(f"amb:{key}", uid)
    if not data:
        return await cb.answer("Уже обработано")
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    if action == "new":
        await start_idea(cb.message, uid, data["text"], data.get("media", []), f"tg-idea:{key}")
    elif action.startswith("ref"):
        await start_refine(cb.message, uid, int(action[3:]), data["text"], f"tg-ref:{key}")
    else:
        await cb.message.answer("Ок, ничего не запускаю.")


@dp.callback_query(F.data.startswith("voice:"))
async def cb_voice(cb: CallbackQuery):
    uid = owner_of(cb.message.chat.id)
    if not uid:
        return await cb.answer("Нет доступа")
    _, action, key = cb.data.split(":", 2)
    data = pending_take(f"voice:{key}", uid)
    if not data:
        return await cb.answer("Уже обработано")
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    if action == "go":
        if data.get("idea_id"):
            await start_refine(cb.message, uid, data["idea_id"], data["text"], f"tg-ref:voice:{key}")
        else:
            await start_idea(cb.message, uid, data["text"], [], f"tg-idea:voice:{key}")
    else:
        await cb.message.answer("Отменено. Можно прислать текстом.")


# ---------- сообщения ----------

async def route_text(message: Message, uid: int, text: str, media: list):
    """Only deliberate human requests can create paid jobs."""
    if not intended_for_bot(message,uid):
        return
    if BOT_USERNAME:
        text = re.sub(r"@" + re.escape(BOT_USERNAME) + r"\b", "", text or "", flags=re.I).strip()
    if (text or "").strip().lower().strip(".! ") in ("ок", "окей", "спасибо", "понятно", "ясно", "да", "нет", "ok", "thanks") and not media:
        return
    key = f"{message.chat.id}:{message.message_id}"
    if message.reply_to_message:
        with db.db() as conn:
            row = conn.execute("SELECT idea_id FROM tg_message_map WHERE chat_id=? AND message_id=?",
                               (message.chat.id, message.reply_to_message.message_id)).fetchone()
        if row:
            with db.db() as conn:
                idea = db.get_idea(conn, row["idea_id"], uid)
            if not idea:
                return
            if is_confused(text):
                return await explain_question(message,idea)
            if any(term in text.lower() for term in ("не нравится", "не подходит", "мне нравится", "запомни", "учитывай", "предпочитаем")):
                rating = "dislike" if any(term in text.lower() for term in ("не нравится","не подходит")) else "revise"
                with db.db() as conn:
                    db.save_feedback(conn,uid,row["idea_id"],rating,text,True)
                await message.answer(f"Отзыв к идее #{row['idea_id']} сохранён. Учту причину в следующих идеях. Сохранение бесплатно.")
                return
            return await start_refine(message, uid, row["idea_id"], text, f"tg-ref:{key}")
    mode = pending_get(mode_key(message,uid), uid)
    if is_confused(text):
        with db.db() as conn:
            idea = (db.get_idea(conn,mode["idea_id"],uid) if mode and mode.get("mode")=="refine" else
                conn.execute("SELECT * FROM ideas WHERE user_id=? AND archived=0 ORDER BY updated_at DESC LIMIT 1",(uid,)).fetchone())
        if idea:
            return await explain_question(message,idea)
        return
    mode = pending_take(mode_key(message,uid), uid)
    if mode and mode.get("mode") == "refine":
        return await start_refine(message, uid, mode["idea_id"], text, f"tg-ref:{key}")
    if (mode and mode.get("mode") == "new") or media or looks_like_idea(text):
        return await start_idea(message, uid, text, media, f"tg-idea:{key}")
    if message.chat.type == "private":
        await message.answer("Для нового разбора пришлите название товара, фото или ссылку. Отзыв к готовой идее можно оставить ответом на её сообщение.")


async def finalize_album(bot: Bot, group_key: str, uid: int, message: Message):
    await asyncio.sleep(2.5)
    data = pending_take(f"album:{group_key}", uid)
    album_tasks.pop(group_key, None)
    if data:
        await route_text(message, uid, data.get("text", ""), data.get("media", []))


@dp.message(F.photo)
async def on_photo(message: Message, bot: Bot):
    uid = await guard(message)
    album_known = uid and message.media_group_id and pending_get(f"album:{message.chat.id}:{message.media_group_id}",uid)
    if not uid or not (album_known or intended_for_bot(message,uid)):
        return
    ph = message.photo[-1]
    path = await download(bot, ph.file_id, uid, f"{ph.file_unique_id}.jpg")
    item = {"type": "photo", "path": path, "mime": "image/jpeg"}
    if message.media_group_id:
        gk = f"{message.chat.id}:{message.media_group_id}"
        data = pending_get(f"album:{gk}", uid) or {"text": "", "media": []}
        data["media"].append(item)
        if message.caption:
            data["text"] = (data["text"] + "\n" + message.caption).strip()
        pending_put(f"album:{gk}", uid, data)
        if gk not in album_tasks:
            album_tasks[gk] = asyncio.create_task(finalize_album(bot, gk, uid, message))
        return
    await route_text(message, uid, message.caption or "", [item])


@dp.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot):
    uid = await guard(message)
    if not uid or not intended_for_bot(message,uid):
        return
    if not config.OPENAI_API_KEY:
        await message.answer("🎙 Голосовые пока не подключены (нужен ключ расшифровки OPENAI_API_KEY, ≈ $0.006/мин). "
                             "Пришлите, пожалуйста, текстом.")
        return
    v = message.voice or message.audio
    path = await download(bot, v.file_id, uid, f"{v.file_unique_id}.ogg")
    from .transcribe import transcribe
    try:
        text = await transcribe(path, v.duration or 0, uid)
    except Exception as e:
        db.log_error("voice", repr(e))
        await message.answer("Не удалось расшифровать голосовое. Пришлите текстом.")
        return
    key = f"{message.chat.id}:{message.message_id}"
    idea_id = None
    if message.reply_to_message:
        with db.db() as conn:
            row = conn.execute("SELECT idea_id FROM tg_message_map WHERE chat_id=? AND message_id=?",
                               (message.chat.id, message.reply_to_message.message_id)).fetchone()
        idea_id = row["idea_id"] if row else None
    pending_put(f"voice:{key}", uid, {"text": text, "idea_id": idea_id})
    what = f"уточнение к идее #{idea_id}" if idea_id else "новую идею"
    await message.answer(f"🎙 Распознал:\n«{text}»\n\nЗапустить как {what}?", reply_markup=kb(
        [[{"text": "▶️ Запустить", "callback_data": f"voice:go:{key}"},
          {"text": "❌ Отмена", "callback_data": f"voice:no:{key}"}]]))


@dp.message(F.text)
async def on_text(message: Message):
    uid = await guard(message)
    if not uid:
        return
    if message.text.startswith("/"):
        await message.answer("Не знаю такую команду. /help")
        return
    await route_text(message, uid, message.text, [])


async def main():
    global BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ensure_admin()
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN не задан — бот не запущен")
        while True:
            await asyncio.sleep(3600)
    bot = Bot(config.TELEGRAM_BOT_TOKEN)
    BOT_USERNAME = (await bot.get_me()).username or ""
    await bot.delete_webhook(drop_pending_updates=False)  # используем только polling — без конкуренции с webhook
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())

