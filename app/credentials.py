"""Owner-scoped encrypted integration credentials. Never return stored values to a page."""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import config, db

FIELDS = {
    "dataforseo": [("DATAFORSEO_LOGIN","API login"),("DATAFORSEO_PASSWORD","API password")],
    "searchapi": [("SEARCHAPI_API_KEY","API key")],
    "keepa": [("KEEPA_API_KEY","API key")],
    "census": [("CENSUS_API_KEY","API key")],
}


def cipher():
    if not config.SECRET_KEY or config.SECRET_KEY == "change-me-please":
        raise ValueError("Сначала задайте постоянный SECRET_KEY на сервере")
    key = hashlib.sha256(("integration-secrets-v1:"+config.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def get(user_id, name):
    if name not in {key for fields in FIELDS.values() for key,_ in fields}:
        raise ValueError("Неизвестный ключ подключения")
    with db.db() as conn:
        row = conn.execute("SELECT ciphertext FROM integration_secrets WHERE user_id=? AND name=?",(user_id,name)).fetchone()
    if row:
        try:
            return cipher().decrypt(row["ciphertext"].encode()).decode()
        except InvalidToken:
            raise ValueError("Ключ подключения не читается. Проверьте SECRET_KEY или сохраните ключ заново") from None
    return getattr(config,name,"")


def ready(user_id, provider):
    try:
        return all(get(user_id,name) for name,_ in FIELDS[provider])
    except ValueError:
        return False


def save(user_id, provider, values):
    if provider not in FIELDS:
        raise ValueError("Неизвестное подключение")
    clean = {name:str(values.get(name) or "").strip() for name,_ in FIELDS[provider]}
    if any(len(value)>4096 or "\n" in value or "\r" in value for value in clean.values()):
        raise ValueError("Некорректный ключ подключения")
    encoded = {name:cipher().encrypt(value.encode()).decode() for name,value in clean.items() if value}
    with db.db() as conn, db.tx(conn):
        for name,value in encoded.items():
            conn.execute("INSERT INTO integration_secrets VALUES(?,?,?,?) ON CONFLICT(user_id,name) "
                         "DO UPDATE SET ciphertext=excluded.ciphertext,updated_at=excluded.updated_at",
                         (user_id,name,value,db.now_iso()))
        if encoded:
            conn.execute("DELETE FROM source_checks WHERE source=?",(f"analytics:{user_id}:{provider}",))


def clear(user_id, provider):
    if provider not in FIELDS:
        raise ValueError("Неизвестное подключение")
    with db.db() as conn, db.tx(conn):
        for name,_ in FIELDS[provider]:
            conn.execute("DELETE FROM integration_secrets WHERE user_id=? AND name=?",(user_id,name))
        conn.execute("DELETE FROM source_checks WHERE source=?",(f"analytics:{user_id}:{provider}",))
