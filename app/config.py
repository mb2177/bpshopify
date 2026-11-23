"""Настройки приложения из переменных окружения (.env)."""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


DATA_DIR = Path(_env("DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = Path(_env("DB_PATH", str(DATA_DIR / "app.db")))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

SITE_URL = _env("SITE_URL", "http://localhost:8000").rstrip("/")
SECRET_KEY = _env("SECRET_KEY", "change-me-please")
ADMIN_EMAIL = _env("ADMIN_EMAIL", "owner@example.com")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")

# Telegram
TELEGRAM_BOT_TOKEN = _env("TELEGRAM_BOT_TOKEN")

# Anthropic (исследование)
ANTHROPIC_API_KEY = _env("ANTHROPIC_API_KEY")
MODEL_RESEARCH = _env("MODEL_RESEARCH", "claude-sonnet-5-5")
MODEL_FAST = _env("MODEL_FAST", "claude-haiku-5-5")
MODEL_DEEP = _env("MODEL_DEEP", "claude-opus-5-5")  # «Глубокий разбор» по кнопке
WEB_SEARCH_TOOL = _env("WEB_SEARCH_TOOL", "web_search_20260209")
WEB_SEARCH_MAX_USES = int(_env("WEB_SEARCH_MAX_USES", "3"))
MARKET_MAX_TOKENS = int(_env("MARKET_MAX_TOKENS", "12000"))
PRODUCTION_MAX_TOKENS = int(_env("PRODUCTION_MAX_TOKENS", "7000"))
SEARCH_INPUT_RESERVE_TOKENS = int(_env("SEARCH_INPUT_RESERVE_TOKENS", "200000"))
GOOGLE_ADS_DEVELOPER_TOKEN = _env("GOOGLE_ADS_DEVELOPER_TOKEN")
GOOGLE_ADS_CUSTOMER_ID = _env("GOOGLE_ADS_CUSTOMER_ID").replace("-", "")
GOOGLE_ADS_CLIENT_ID = _env("GOOGLE_ADS_CLIENT_ID")
GOOGLE_ADS_CLIENT_SECRET = _env("GOOGLE_ADS_CLIENT_SECRET")
GOOGLE_ADS_REFRESH_TOKEN = _env("GOOGLE_ADS_REFRESH_TOKEN")
GOOGLE_ADS_LOGIN_CUSTOMER_ID = _env("GOOGLE_ADS_LOGIN_CUSTOMER_ID").replace("-", "")
GOOGLE_ADS_API_VERSION = _env("GOOGLE_ADS_API_VERSION", "v24")
CENSUS_API_KEY = _env("CENSUS_API_KEY")

# Цены (USD). Проверяйте актуальные цены на platform.claude.com/docs.
PRICE_RESEARCH_IN = _float("PRICE_RESEARCH_IN_PER_MTOK", 2.0)
PRICE_RESEARCH_OUT = _float("PRICE_RESEARCH_OUT_PER_MTOK", 10.0)
PRICE_FAST_IN = _float("PRICE_FAST_IN_PER_MTOK", 1.0)
PRICE_FAST_OUT = _float("PRICE_FAST_OUT_PER_MTOK", 5.0)
PRICE_DEEP_IN = _float("PRICE_DEEP_IN_PER_MTOK", 4.0)
PRICE_DEEP_OUT = _float("PRICE_DEEP_OUT_PER_MTOK", 20.0)
DEEP_BUDGET_MULT = _float("DEEP_BUDGET_MULT", 2.5)  # лимит на глубокий разбор = лимит на разбор × это
PRICE_PER_SEARCH =_float("PRICE_PER_WEB_SEARCH", 0.01)

# Голосовые (необязательно)
OPENAI_API_KEY = _env("OPENAI_API_KEY")
TRANSCRIBE_MODEL = _env("TRANSCRIBE_MODEL", "whisper-1")
PRICE_TRANSCRIBE_PER_MIN = _float("PRICE_TRANSCRIBE_PER_MIN", 0.006)

# Режим без реальных API (для локальной проверки). Все данные помечаются как ДЕМО.
FAKE_LLM = _env("FAKE_LLM", "0") == "1"

# Официальная привязка дирхама ОАЭ к доллару США (Центральный банк ОАЭ).
AED_PER_USD = _float("AED_PER_USD", 3.6725)
FX_SOURCE = _env("FX_SOURCE", "Фиксированный курс Центрального банка ОАЭ (CBUAE), 1 USD = 3.6725 AED")

MARKETS = ("USA", "UAE")
MARKET_LABEL = {"USA": "США (USA)", "UAE": "ОАЭ (UAE)", "none": "пока не выбран", None: "пока не выбран", "": "пока не выбран"}
MARKET_CURRENCY = {"USA": "USD", "UAE": "AED"}
MARKET_TZ = {"USA": "America/New_York", "UAE": "Asia/Dubai"}

