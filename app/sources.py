"""Паспорта источников, адаптеры (Google Trends CSV, UN Comtrade, загрузка ссылок)."""
import csv
import io
import re
from datetime import datetime
from statistics import mean

import httpx

from . import config, db

AUTO, KEY, MANUAL, UNAVAILABLE, LATER = "автоматически", "нужен ключ", "ручной импорт", "недоступно", "после запуска магазина"

PASSPORTS = [
    {"id": "claude_web_search", "name": "Веб-поиск через Claude API",
     "what": "Основной автоматический сбор: страницы конкурентов, маркетплейсов (через поиск), подрядчиков, новости",
     "docs": "https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool",
     "access": "API-ключ Anthropic", "free_quota": "Нет: $10 за 1000 поисков + токены (учитывается в бюджете)",
     "commercial": "Разрешено по условиям Anthropic API", "coverage": "USA и UAE (локализация поиска)",
     "fields": "URL, заголовок, дата страницы, цитаты", "freshness": "При каждом исследовании",
     "needs": "ANTHROPIC_API_KEY"},
    {"id": "google_trends", "name": "Google Trends",
     "what": "Динамика интереса и сезонность (не продажи, не число покупателей)",
     "docs": "https://developers.google.com/search/apis/trends",
     "access": "Официальный API — ограниченный доступ по заявке; в MVP — импорт CSV из trends.google.com",
     "free_quota": "Веб-интерфейс бесплатен", "commercial": "Проверить условия API при получении доступа",
     "coverage": "Отдельно US и AE (выбирайте страну при выгрузке)", "fields": "Индекс 0–100 по неделям/месяцам",
     "freshness": "Ручное обновление при импорте", "needs": None,
     "howto": "trends.google.com → запрос → страна United States или United Arab Emirates → период 5 лет → "
              "значок загрузки CSV → загрузите файл на странице идеи. Независимо выгруженные файлы нельзя сравнивать напрямую."},
    {"id": "keyword_planner", "name": "Google Ads Keyword Planner",
     "what": "Коммерческие запросы, диапазоны объёма, ставки", "docs": "https://support.google.com/google-ads/answer/7337243",
     "access": "Аккаунт Google Ads (нужны платёжные данные). API — developer token и разрешение",
     "free_quota": "Интерфейс бесплатен", "commercial": "API — по правилам Google Ads API",
     "coverage": "USA, UAE", "fields": "Диапазоны объёма, конкуренция рекламодателей, ставки",
     "freshness": "Ручное", "needs": None,
     "howto": "Сделайте выгрузку в Keyword Planner и внесите ключевые цифры как наблюдение на странице идеи. "
              "Ставка за клик ≠ CAC; конкуренция рекламодателей ≠ конкуренция рынка."},
    {"id": "meta_ad_library", "name": "Meta Ad Library", "what": "Офферы и креативы конкурентов",
     "docs": "https://www.facebook.com/ads/library/", "access": "Публичный поиск; API не покрывает всю коммерческую рекламу",
     "free_quota": "Бесплатно", "commercial": "—", "coverage": "Выбирайте страну US / AE",
     "fields": "Объявления, даты показа", "freshness": "Ручное", "needs": None,
     "howto": "Найдите рекламу конкурентов и добавьте ссылку и наблюдение. Наличие рекламы не доказывает продажи."},
    {"id": "tiktok_cc", "name": "TikTok Creative Center", "what": "Товарные и креативные сигналы",
     "docs": "https://ads.tiktok.com/business/creativecenter/", "access": "Публичный ресурс, часть разделов с авторизацией",
     "free_quota": "Бесплатно", "commercial": "—", "coverage": "Проверяйте наличие US/AE в разделе",
     "fields": "Темы, форматы, объявления", "freshness": "Ручное", "needs": None,
     "howto": "Добавьте ссылку и наблюдение. Не переносите данные другой страны."},
    {"id": "marketplaces", "name": "Amazon.com, Amazon.ae, Noon, Etsy",
     "what": "Локальные цены, отзывы, жалобы, ранги", "docs": "https://sell.amazon.com/blog/amazon-best-sellers-rank",
     "access": "Через веб-поиск (публичные результаты) + ручной импорт; без скрапинга",
     "free_quota": "—", "commercial": "Автосбор только разрешённым способом", "coverage": "USA (Amazon.com, Etsy), UAE (Amazon.ae, Noon)",
     "fields": "Цены, комплектация, отзывы", "freshness": "При исследовании", "needs": "ANTHROPIC_API_KEY"},
    {"id": "suppliers", "name": "Сайты подрядчиков и перевозчиков", "what": "Себестоимость, MOQ, сроки, образцы",
     "docs": "—", "access": "Через веб-поиск + ручное внесение полученных котировок", "free_quota": "—",
     "commercial": "—", "coverage": "USA, UAE, страны производства", "fields": "Прайсы, MOQ, сроки",
     "freshness": "При исследовании", "needs": "ANTHROPIC_API_KEY"},
    {"id": "un_comtrade", "name": "UN Comtrade (public API)", "what": "Импорт категории в США (842) и ОАЭ (784)",
     "docs": "https://comtrade.un.org/en/api-docs", "access": "Public preview без ключа, ограниченные выборки",
     "free_quota": "Ограниченные запросы", "commercial": "Проверить условия для SaaS", "coverage": "USA, UAE",
     "fields": "Стоимость импорта по HS-коду и году", "freshness": "Годовые данные, кеш",
     "needs": None, "auto": True},
    {"id": "census", "name": "U.S. Census / ACS", "what": "Контекст сегментов США", "docs": "https://www.census.gov/data/developers.html",
     "access": "Бесплатный ключ", "free_quota": "Бесплатно", "commercial": "Открытые данные", "coverage": "USA",
     "fields": "Население, доходы", "freshness": "Годовые", "needs": "CENSUS_API_KEY", "not_in_mvp": True},
    {"id": "uae_stat", "name": "FCSC / UAE.Stat", "what": "Контекст сегментов ОАЭ", "docs": "https://uaestat.fcsc.gov.ae/en",
     "access": "Открытые таблицы — ручной импорт", "free_quota": "Бесплатно", "commercial": "Открытые данные",
     "coverage": "UAE", "fields": "Население, показатели", "freshness": "Годовые", "needs": None},
    {"id": "clarity", "name": "Microsoft Clarity", "what": "Поведение посетителей тестовой страницы",
     "docs": "https://learn.microsoft.com/en-us/clarity/setup-and-installation/clarity-data-export-api",
     "access": "Бесплатно, токен Data Export API", "free_quota": "С ограничениями API", "commercial": "—",
     "coverage": "Наш трафик", "fields": "Сессии, клики, скроллы", "freshness": "После запуска", "needs": None, "later": True},
    {"id": "shopify", "name": "Shopify (наш магазин)", "what": "Фактические заказы, возвраты, маржа",
     "docs": "https://shopify.dev/docs/api", "access": "Приложение с минимальными правами", "free_quota": "—",
     "commercial": "—", "coverage": "Наш магазин", "fields": "Заказы", "freshness": "После запуска", "needs": None, "later": True},
    {"id": "voice", "name": "Расшифровка голосовых (OpenAI)", "what": "Голосовые сообщения в Telegram",
     "docs": "https://platform.openai.com/docs/guides/speech-to-text", "access": "Ключ OpenAI",
     "free_quota": "Нет: ≈ $0.006/мин (учитывается в бюджете)", "commercial": "—", "coverage": "—",
     "fields": "Текст", "freshness": "—", "needs": "OPENAI_API_KEY"},
]


def passport_status(p: dict) -> str:
    if p.get("later"):
        return LATER
    if p["id"] in ("google_trends", "keyword_planner", "meta_ad_library", "tiktok_cc", "uae_stat"):
        return MANUAL
    if p.get("not_in_mvp"):
        return KEY
    need = p.get("needs")
    if need == "ANTHROPIC_API_KEY":
        return AUTO if (config.ANTHROPIC_API_KEY or config.FAKE_LLM) else KEY
    if need == "OPENAI_API_KEY":
        return AUTO if config.OPENAI_API_KEY else KEY
    if p.get("auto"):
        return AUTO
    return MANUAL


def all_passports() -> list[dict]:
    with db.db() as conn:
        checks = {r["source"]: dict(r) for r in conn.execute("SELECT * FROM source_checks")}
    out = []
    for p in PASSPORTS:
        q = dict(p)
        q["status"] = passport_status(p)
        q["check"] = checks.get(p["id"], {})
        out.append(q)
    return out


# ---------- Google Trends CSV ----------

def parse_trends_csv(content: bytes) -> dict:
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        first = line.split(",")[0].strip().lower()
        if first in ("week", "month", "day", "time", "неделя", "месяц", "день", "время") and "," in line:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("Не похоже на CSV Google Trends: не найдена строка заголовка (Week/Month/Day).")
    reader = csv.reader(io.StringIO("\n".join(lines[header_idx:])))
    header = next(reader)
    series = {h.strip(): [] for h in header[1:]}
    dates = []
    for row in reader:
        if len(row) < 2 or not row[0].strip():
            continue
        try:
            d = datetime.fromisoformat(row[0].strip()[:10]).date()
        except ValueError:
            continue
        dates.append(d.isoformat())
        for h, v in zip(header[1:], row[1:]):
            v = v.strip()
            series[h.strip()].append(0.5 if v == "<1" else float(v) if v else None)
    if not dates:
        raise ValueError("В CSV нет данных.")
    stats = {}
    for name, vals in series.items():
        pts = [(d, v) for d, v in zip(dates, vals) if v is not None]
        if len(pts) < 8:
            continue
        values = [v for _, v in pts]
        last4 = mean(values[-4:])
        prev12 = mean(values[-16:-4]) if len(values) >= 16 else None
        yoy = None
        if len(values) >= 56:
            year_ago = mean(values[-56:-48])
            yoy = (last4 - year_ago) / year_ago if year_ago else None
        by_month = {}
        for d, v in pts:
            by_month.setdefault(int(d[5:7]), []).append(v)
        month_avg = {m: round(mean(vs), 1) for m, vs in sorted(by_month.items())}
        peak = max(month_avg, key=month_avg.get)
        overall = mean(values)
        stats[name] = {
            "last4_avg": round(last4, 1), "prev12_avg": round(prev12, 1) if prev12 else None,
            "momentum": round((last4 - prev12) / prev12, 3) if prev12 else None,
            "yoy": round(yoy, 3) if yoy is not None else None, "peak_month": peak,
            "month_avg": month_avg, "overall_avg": round(overall, 1),
            "seasonality_ratio": round(month_avg[peak] / overall, 2) if overall else None,
            "low_base": overall < 5,
        }
    return {"period": f"{dates[0]} — {dates[-1]}", "points": len(dates), "series": list(series), "stats": stats,
            "note": "Нормированный индекс Google Trends 0–100 внутри этой выгрузки; не равен продажам."}


# ---------- UN Comtrade ----------

REPORTER = {"USA": 842, "UAE": 784}


def comtrade_imports(hs_code: str, market: str, years=(2023, 2022, 2021)) -> dict:
    """Импорт категории (HS) в страну из всех стран (partner 0). Public preview API."""
    hs = re.sub(r"\D", "", str(hs_code or ""))[:6]
    if len(hs) < 2:
        raise ValueError("Нет HS-кода")
    url = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
    out = []
    with httpx.Client(timeout=30) as client:
        for y in years:
            r = client.get(url, params={"reporterCode": REPORTER[market], "period": y, "partnerCode": 0,
                                        "cmdCode": hs, "flowCode": "M"})
            r.raise_for_status()
            data = r.json().get("data") or []
            val = sum(float(d.get("primaryValue") or 0) for d in data)
            out.append({"year": y, "import_usd": val if data else None})
    db.source_ok("un_comtrade")
    return {"hs_code": hs, "market": market, "series": out, "source_url": "https://comtradeplus.un.org/",
            "note": "Импорт категории (включая реэкспорт для ОАЭ) — не равен розничным покупкам конкретного товара."}


# ---------- загрузка ссылки ----------

def fetch_url_text(url: str, limit: int = 12000) -> dict:
    """Получает текст публичной страницы. Не обходит защиту: при отказе сайта честно сообщает."""
    from bs4 import BeautifulSoup
    if not re.match(r"^https?://", url):
        return {"url": url, "ok": False, "error": "Некорректная ссылка"}
    try:
        with httpx.Client(timeout=20, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (research bot; contact: owner)"}) as c:
            r = c.get(url)
        if r.status_code >= 400:
            return {"url": url, "ok": False, "error": f"Сайт вернул {r.status_code} — страница недоступна для автоматического чтения"}
        ctype = r.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype:
            return {"url": url, "ok": False, "error": f"Не текстовая страница ({ctype})"}
        soup = BeautifulSoup(r.text[:2_000_000], "html.parser")
        for t in soup(["script", "style", "noscript", "svg"]):
            t.decompose()
        title = (soup.title.string or "").strip() if soup.title else ""
        text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
        return {"url": str(r.url), "ok": True, "title": title, "text": text[:limit]}
    except Exception as e:
        return {"url": url, "ok": False, "error": f"Не удалось загрузить: {type(e).__name__}"}
