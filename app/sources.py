"""Паспорта источников, адаптеры (Google Trends CSV, UN Comtrade, загрузка ссылок)."""
import csv
import io
import re
from datetime import datetime, timedelta, date
from statistics import mean
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import time
import socket
import ipaddress
import hashlib
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import config, db


class SourceNeedsConnection(RuntimeError):
    pass


def canonical_url(value: str | None) -> str:
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in ("http","https") or not parts.hostname or parts.username or parts.password:
            return ""
        host = parts.hostname.lower().removeprefix("www.")
        query = [(k,v) for k,v in parse_qsl(parts.query) if not k.startswith("utm_") and k not in ("fbclid","gclid")]
        return urlunsplit(("https",host,parts.path.rstrip("/"),urlencode(sorted(query)),""))
    except ValueError:
        return ""


def google_ads_ready() -> bool:
    return all((config.GOOGLE_ADS_DEVELOPER_TOKEN,config.GOOGLE_ADS_CUSTOMER_ID,
                config.GOOGLE_ADS_CLIENT_ID,config.GOOGLE_ADS_CLIENT_SECRET,config.GOOGLE_ADS_REFRESH_TOKEN))


def keyword_metrics(keywords: list[str], market: str) -> dict:
    if not google_ads_ready():
        return {"status":"needs_connection","market":market,"source":"Google Ads Keyword Planner"}
    keywords = sorted({str(k).strip() for k in keywords if str(k).strip()})[:15]
    key = "keywords:" + hashlib.sha256(db.jdump([keywords,market]).encode()).hexdigest()
    cached = db.cache_get(key)
    if cached:
        return cached
    with httpx.Client(timeout=25) as client:
        token = client.post("https://oauth2.googleapis.com/token",data={
            "client_id":config.GOOGLE_ADS_CLIENT_ID,"client_secret":config.GOOGLE_ADS_CLIENT_SECRET,
            "refresh_token":config.GOOGLE_ADS_REFRESH_TOKEN,"grant_type":"refresh_token"})
        token.raise_for_status()
        headers = {"Authorization":"Bearer " + token.json()["access_token"],
                   "developer-token":config.GOOGLE_ADS_DEVELOPER_TOKEN}
        if config.GOOGLE_ADS_LOGIN_CUSTOMER_ID:
            headers["login-customer-id"] = config.GOOGLE_ADS_LOGIN_CUSTOMER_ID
        url = f"https://googleads.googleapis.com/{config.GOOGLE_ADS_API_VERSION}/customers/{config.GOOGLE_ADS_CUSTOMER_ID}:generateKeywordHistoricalMetrics"
        response = client.post(url,headers=headers,json={"keywords":keywords,
            "geoTargetConstants":["geoTargetConstants/" + ("2840" if market=="USA" else "2784")],
            "language":"languageConstants/1000","keywordPlanNetwork":"GOOGLE_SEARCH"})
        response.raise_for_status()
    result = {"status":"ok","source":"Google Ads Keyword Planner","market":market,
              "retrieved_at":db.now_iso(),"results":response.json().get("results",[]),
              "source_url":"https://ads.google.com/home/tools/keyword-planner/",
              "note":"Исторические оценки запросов Google; не продажи и не прогноз CAC."}
    db.cache_put(key,result,86400*30)
    db.source_ok("keyword_planner")
    return result


def census_context() -> dict:
    year = datetime.now().year-2
    key = f"census:{year}"
    cached = db.cache_get(key)
    if cached:
        return cached
    if not config.CENSUS_API_KEY:
        raise SourceNeedsConnection("Нужен бесплатный ключ CENSUS_API_KEY. С мая 2026 Census требует ключ для запросов данных.")
    url = f"https://api.census.gov/data/{year}/acs/acs5"
    params = {"get":"NAME,B01003_001E,B19013_001E","for":"us:*"}
    if config.CENSUS_API_KEY:
        params["key"] = config.CENSUS_API_KEY
    response = httpx.get(url,params=params,timeout=15)
    response.raise_for_status()
    rows = response.json()
    if len(rows)<2:
        raise ValueError("Нет данных Census")
    values = dict(zip(rows[0],rows[1]))
    result = {"source":"U.S. Census ACS","market":"USA","scope":"context","period":str(year),
              "population":int(values["B01003_001E"]),"median_household_income_usd":int(values["B19013_001E"]),
              "source_url":url,"retrieved_at":db.now_iso(),
              "note":"Демографический контекст, без вывода о спросе на конкретный товар."}
    db.cache_put(key,result,86400*30)
    db.source_ok("census")
    return result


def parse_keyword_csv(content: bytes) -> dict:
    text = content.decode("utf-16" if content.startswith((b"\xff\xfe",b"\xfe\xff")) else "utf-8-sig",errors="replace")
    lines = text.splitlines()
    for i,line in enumerate(lines):
        delimiter = "\t" if "\t" in line else ","
        headers = next(csv.reader([line],delimiter=delimiter),[])
        if any(header.strip().casefold() in ("keyword","ключевое слово","ключове слово") for header in headers):
            rows = list(csv.DictReader(lines[i:],delimiter=delimiter))
            if rows:
                return {"rows":rows[:500],"raw_csv":text[:500000],"retrieved_at":db.now_iso(),
                        "note":"Диапазоны и округления выгрузки сохранены. Объём поиска не равен продажам."}
    raise ValueError("Не найден заголовок Keyword в выгрузке Keyword Planner")


def validate_evidence(report: dict, manual_rows: list) -> None:
    """Verify provenance against search results and source text; keep failed claims as estimates."""
    if config.FAKE_LLM:
        for market in (report.get("market",{}).get("markets") or {}).values():
            for signal in (market.get("demand") or {}).get("signals",[]):
                signal.update(verified=True,scope="product",retrieved_at=db.now_iso())
            for competitor in market.get("competitors",[]):
                competitor["verified"] = True
                competitor["retrieved_at"] = db.now_iso()
        return
    known = {canonical_url(s.get("url")) for s in report.get("seen_sources",[])}
    owner = {canonical_url(row["url"]) for row in manual_rows if row["url"]}
    urls = []
    for market in (report.get("market",{}).get("markets") or {}).values():
        urls += [s.get("source_url") for s in (market.get("demand") or {}).get("signals",[]) if s.get("scope")=="product"]
        urls += [c.get("url") for c in market.get("competitors",[])[:3]]
        urls += [g.get("source_url") for g in (market.get("launch_feasibility") or {}).values() if isinstance(g,dict)]
    urls = list(dict.fromkeys(u for u in urls if canonical_url(u) in known and u))[:14]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(fetch_url_text,urls))
    documents = {canonical_url(url):page for url,page in zip(urls,pages)}
    for mk,market in (report.get("market",{}).get("markets") or {}).items():
        market["provenance_checked"] = True
        for gate in (market.get("launch_feasibility") or {}).values():
            if not isinstance(gate,dict):
                continue
            url = canonical_url(gate.get("source_url"))
            page = documents.get(url,{})
            quote = " ".join(str(gate.get("quote") or "").lower().split())
            text = " ".join(page.get("text","").lower().split())
            gate["verified"] = bool(url and url in known and len(quote)>=15 and quote in text)
        for signal in (market.get("demand") or {}).get("signals",[]):
            url = canonical_url(signal.get("source_url"))
            page = documents.get(url,{})
            quote = " ".join(str(signal.get("quote") or "").lower().split())
            text = " ".join(page.get("text","").lower().split())
            signal["verified"] = bool(url and signal.get("market")==mk and
                                      (url in owner or (url in known and len(quote)>=20 and quote in text)))
            signal["retrieved_at"] = page.get("retrieved_at")
            if signal.get("type")=="observation" and not signal["verified"]:
                signal["type"] = "estimate"
                signal["verification_note"] = "Цитата и первоисточник требуют проверки"
        for competitor in market.get("competitors",[]):
            url = canonical_url(competitor.get("url"))
            page = documents.get(url,{})
            price = str(competitor.get("price") or "")
            quote = " ".join(str(competitor.get("price_quote") or "").lower().split())
            text = " ".join(page.get("text","").lower().split())
            price_variants = {price,price.replace(".",","),price.removesuffix(".0")}
            competitor["verified"] = bool(url and (url in owner or
                (url in known and page.get("ok") and price and len(quote)>=5 and quote in text and any(p in quote for p in price_variants))))
            competitor["retrieved_at"] = page.get("retrieved_at")
    report["source_validation"] = {"checked_at":db.now_iso(),"pages_checked":len(pages),
                                   "readable_pages":sum(bool(p.get("ok")) for p in pages)}

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
    if p["id"] == "keyword_planner" and google_ads_ready():
        return AUTO
    if p["id"] == "census":
        return AUTO if config.CENSUS_API_KEY else KEY
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
        check = q["check"]
        if q["status"] == AUTO and not config.FAKE_LLM:
            if check.get("last_error_at") and (check["last_error_at"] > (check.get("last_ok_at") or "")):
                q["status"] = UNAVAILABLE
            elif not check.get("last_ok_at"):
                q["status"] = "ожидает проверки"
        if p["id"] == "census":
            q.update(access="ACS API требует бесплатный активированный ключ с мая 2026", needs="CENSUS_API_KEY",
                     howto="Получите и активируйте ключ на api.census.gov/data/key_signup.html, затем задайте CENSUS_API_KEY на сервере.",
                     fields="Население и медианный доход США, контекст рынка",not_in_mvp=False)
        if p["id"] == "keyword_planner":
            q["howto"] = "Загрузите CSV Keyword Planner на странице идеи. Для API задайте GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CUSTOMER_ID, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET и GOOGLE_ADS_REFRESH_TOKEN на сервере."
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
            raw_date = row[0].strip()[:10]
            if len(raw_date) == 7:
                raw_date += "-01"
            d = datetime.fromisoformat(raw_date).date()
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
        pts = sorted({d:v for d,v in zip(dates,vals) if v is not None}.items())
        if len(pts) < 8:
            continue
        values = [v for _, v in pts]
        intervals = [(date.fromisoformat(b[0])-date.fromisoformat(a[0])).days for a,b in zip(pts,pts[1:])]
        spacing = sorted(intervals)[len(intervals)//2]
        window = 28 if spacing <= 2 else 4 if spacing <= 10 else 1
        period_unit = "дни" if spacing <= 2 else "недели" if spacing <= 10 else "месяцы"
        last4 = mean(values[-window:])
        previous = values[-window*4:-window]
        prev12 = mean(previous) if previous else None
        yoy = None
        last_date = date.fromisoformat(pts[-1][0])
        year_window = [(last_date-timedelta(days=365+spacing*(window-1))), last_date-timedelta(days=365-spacing)]
        old_values = [v for d,v in pts if year_window[0] <= date.fromisoformat(d) <= year_window[1]]
        if old_values:
            year_ago = mean(old_values)
            yoy = (last4 - year_ago) / year_ago if year_ago else None
        by_month = {}
        for d, v in pts:
            by_month.setdefault(int(d[5:7]), []).append(v)
        month_avg = {m: round(mean(vs), 1) for m, vs in sorted(by_month.items())}
        peak = max(month_avg, key=month_avg.get)
        overall = mean(values)
        stats[name] = {
            "interval":period_unit,"recent_window_points":window,
            "last4_avg": round(last4, 1), "prev12_avg": round(prev12, 1) if prev12 else None,
            "momentum": round((last4 - prev12) / prev12, 3) if prev12 else None,
            "yoy": round(yoy, 3) if yoy is not None else None, "peak_month": peak,
            "month_avg": month_avg, "overall_avg": round(overall, 1),
            "seasonality_ratio": round(month_avg[peak] / overall, 2) if overall else None,
            "low_base": overall < 5,
        }
    return {"period": f"{min(dates)} — {max(dates)}", "points": len(dates), "series": list(series), "stats": stats,
            "raw_csv":text[:500000],"retrieved_at":db.now_iso(),
            "note": "Нормированный индекс Google Trends 0–100 внутри этой выгрузки; не равен продажам."}


# ---------- UN Comtrade ----------

REPORTER = {"USA": 842, "UAE": 784}


def comtrade_imports(hs_code: str, market: str, years=None) -> dict:
    """Импорт категории (HS) в страну из всех стран (partner 0). Public preview API."""
    hs = re.sub(r"\D", "", str(hs_code or ""))[:6]
    if len(hs) < 2:
        raise ValueError("Нет HS-кода")
    years = years or tuple(range(datetime.now().year-2,datetime.now().year-5,-1))
    key = f"comtrade:{market}:{hs}:{years}"
    cached = db.cache_get(key)
    if cached:
        if cached.get("error"):
            raise RuntimeError(cached["error"])
        return cached
    url = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
    out = []
    with httpx.Client(timeout=30) as client:
        for y in years:
            r = client.get(url, params={"reporterCode": REPORTER[market], "period": y, "partnerCode": 0,
                                        "cmdCode": hs, "flowCode": "M"})
            if r.status_code == 429:
                try:
                    delay = min(3,float(r.headers.get("retry-after","2")))
                except ValueError:
                    delay = 2
                time.sleep(max(0,delay))
                r = client.get(url,params={"reporterCode":REPORTER[market],"period":y,"partnerCode":0,"cmdCode":hs,"flowCode":"M"})
            if r.status_code in (429,403):
                db.cache_put(key,{"error":f"UN Comtrade HTTP {r.status_code}"},1800)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json().get("data") or []
            matches = [d for d in data if str(d.get("cmdCode",hs)) == hs and int(d.get("partnerCode",0))==0]
            val = float(matches[0]["primaryValue"]) if matches and matches[0].get("primaryValue") is not None else None
            out.append({"year": y, "import_usd": val if data else None})
    result = {"hs_code": hs, "market": market, "series": out, "source_url": "https://comtradeplus.un.org/",
            "retrieved_at":db.now_iso(),"period":str(years),"scope":"context",
            "note": "Импорт категории (включая реэкспорт для ОАЭ) — не равен розничным покупкам конкретного товара."}
    if not any(row["import_usd"] is not None for row in out):
        raise ValueError("Нет данных UN Comtrade для выбранного HS и периода")
    db.source_ok("un_comtrade")
    db.cache_put(key,result,86400*30)
    return result


# ---------- загрузка ссылки ----------

def fetch_url_text(url: str, limit: int = 12000, fresh=False) -> dict:
    """Получает текст публичной страницы. Не обходит защиту: при отказе сайта честно сообщает."""
    from bs4 import BeautifulSoup
    if not canonical_url(url):
        return {"url": url, "ok": False, "error": "Некорректная ссылка"}
    key = "page:" + hashlib.sha256(canonical_url(url).encode()).hexdigest()
    cached = None if fresh else db.cache_get(key)
    if cached:
        return cached
    try:
        with httpx.Client(timeout=12, follow_redirects=False,
                          headers={"User-Agent": "Mozilla/5.0 (research bot; contact: owner)"}) as c:
            target = url
            for _ in range(4):
                host = urlsplit(target).hostname
                addresses = socket.getaddrinfo(host,None,type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
                    raise ValueError("Требуется публичный адрес")
                r = c.get(target)
                if not r.is_redirect:
                    break
                target = str(r.url.join(r.headers["location"]))
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
        result = {"url": str(r.url), "ok": True, "title": title, "text": text[:limit],"retrieved_at":db.now_iso()}
        db.cache_put(key,result,86400)
        return result
    except Exception as e:
        return {"url": url, "ok": False, "error": f"Не удалось загрузить: {type(e).__name__}"}

