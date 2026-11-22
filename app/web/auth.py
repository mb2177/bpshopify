"""Авторизация: пароль (scrypt), сессии, CSRF, одноразовые ссылки входа из Telegram."""
import hashlib
import hmac
import os
import secrets
import time

from .. import config, db


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def ensure_admin() -> None:
    """Создаёт владельца из ADMIN_EMAIL/ADMIN_PASSWORD, если его нет. Пароль обновляется при смене в .env."""
    db.init_db()
    if not config.ADMIN_PASSWORD:
        return
    with db.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email=?", (config.ADMIN_EMAIL,)).fetchone()
        if not row:
            conn.execute("INSERT INTO users(email,password_hash,tg_bind_code,created_at) VALUES(?,?,?,?)",
                         (config.ADMIN_EMAIL, hash_password(config.ADMIN_PASSWORD), new_bind_code(), db.now_iso()))
        elif not verify_password(config.ADMIN_PASSWORD, row["password_hash"]):
            conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(config.ADMIN_PASSWORD), row["id"]))


def new_bind_code() -> str:
    return secrets.token_hex(4).upper()


def csrf_token(session: dict) -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def check_csrf(session: dict, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(session.get("csrf", ""), token)


def make_login_token(user_id: int, ttl: int = 600) -> str:
    tok = secrets.token_urlsafe(24)
    with db.db() as conn:
        conn.execute("INSERT INTO login_tokens(token,user_id,expires_at) VALUES(?,?,?)", (tok, user_id, time.time() + ttl))
    return tok


def use_login_token(tok: str):
    with db.db() as conn:
        with db.tx(conn):
            row = conn.execute("SELECT * FROM login_tokens WHERE token=?", (tok,)).fetchone()
            if not row or row["used"] or row["expires_at"] < time.time():
                return None
            conn.execute("UPDATE login_tokens SET used=1 WHERE token=?", (tok,))
    return row["user_id"]
