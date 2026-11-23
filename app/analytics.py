"""Structured analytical APIs. Values come from provider responses, never from model text."""
import hashlib
import math
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median
from urllib.parse import urlencode, urlsplit

import httpx

from . import config, credentials, db, jobs, llm

PROVIDERS = {
    "dataforseo": {"name":"DataForSEO","signup":"https://app.dataforseo.com/register",
        "docs":"https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/",
        "what":"Google Trends, объёмы запросов Google и Bing, динамика по месяцам и ставки за клик",
        "billing":"Оплата за запрос. Фактическая сумма из ответа включается в бюджет приложения. Пополнение от $50.",
        "coverage":"США и ОАЭ, запросы на английском. Наличие данных проверяется отдельно."},
    "searchapi": {"name":"SearchApi","signup":"https://www.searchapi.io/",
        "docs":"https://www.searchapi.io/docs/meta-ad-library-api",
        "what":"Активные объявления Meta, даты запуска и рекламодатели; предложения и цены Google Shopping",
        "billing":"Запросы расходуют кредиты вашего тарифа. Подписка оплачивается отдельно, учёт запросов виден здесь.",
        "coverage":"США и ОАЭ, с проверкой страны в ответе. Объявления не доказывают продажи."},
    "keepa": {"name":"Keepa","signup":"https://keepa.com/#!api",
        "docs":"https://keepa.com/api-docs/product.html",
        "what":"История цен Amazon, наличие, sales rank и доступная оценка месячных покупок сопоставимых ASIN",
        "billing":"Нужен API-тариф. Обычно 1 токен на ASIN без платных дополнительных полей. Подписка оплачивается отдельно.",
        "coverage":"Только Amazon.com для США. Amazon.ae не поддерживается; данные США в ОАЭ не переносятся."},
    "census": {"name":"U.S. Census ACS","signup":"https://api.census.gov/data/key_signup.html",
        "docs":"https://www.census.gov/data/developers.html",
        "what":"Население и медианный доход США. Контекст рынка, без вывода о продажах товара.",
        "billing":"Бесплатный ключ с подтверждением по email.","coverage":"США"},
}
COUNTRY = {"USA":"US","UAE":"AE"}
LOCATION = {"USA":2840,"UAE":2784}
MEASUREMENT_KINDS = {"trends_api","search_volume_api","marketplace_api","ad_library_api","shopping_api"}


class AnalyticsError(RuntimeError):
    pass


def number(value, minimum=0):
    if isinstance(value,bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) and value>=minimum else None
    except (ValueError,TypeError):
        return None


def _safe(value):
    """Keep credentials and private provider history URLs out of caches, reports, and errors."""
    if isinstance(value,dict):
        return {key:_safe(item) for key,item in value.items() if key.lower() not in
            {"api_key","key","password","authorization","login","request_url","html_url","json_url"}}
    if isinstance(value,list):
        return [_safe(item) for item in value]
    return value


def _send(user_id, provider, endpoint, payload):
    with httpx.Client(timeout=50,follow_redirects=False) as client:
        if provider=="dataforseo":
            return client.post("https://api.dataforseo.com/v3/"+endpoint,json=[payload],
                auth=(credentials.get(user_id,"DATAFORSEO_LOGIN"),credentials.get(user_id,"DATAFORSEO_PASSWORD")))
        if provider=="searchapi":
            return client.get("https://www.searchapi.io/api/v1/search",params=payload,
                headers={"Authorization":"Bearer "+credentials.get(user_id,"SEARCHAPI_API_KEY")})
        if provider=="keepa":
            return client.get("https://api.keepa.com/product",params={**payload,"key":credentials.get(user_id,"KEEPA_API_KEY")})
    raise AnalyticsError("Неизвестный провайдер")


def request(ctx, provider, endpoint, payload, ttl, units=1):
    if not credentials.ready(ctx.user_id,provider):
        raise credentials_missing(provider)
    identity = [credentials.get(ctx.user_id,name) for name,_ in credentials.FIELDS[provider]]
    fingerprint = hashlib.sha256(db.jdump(identity).encode()).hexdigest()
    key = "analytics:"+hashlib.sha256(db.jdump([ctx.user_id,provider,fingerprint,endpoint,payload]).encode()).hexdigest()
    cached = None if getattr(ctx,"analytics_force_fresh",False) else db.cache_get(key)
    group = ctx.budget_group or ctx.job_id
    if cached:
        with db.db() as conn:
            conn.execute("INSERT INTO analytics_calls(user_id,job_id,budget_group,provider,endpoint,cache_key,status,cost_usd,units,created_at) "
                "VALUES(?,?,?,?,?,?,'cache',0,0,?)",(ctx.user_id,ctx.job_id,group,provider,endpoint,key,db.now_iso()))
        return {**cached,"_from_cache":True}
    jobs.check_cancel(ctx.job_id)
    estimate = config.ANALYTICS_CALL_RESERVE_USD if provider=="dataforseo" else 0.0
    month = datetime.now(timezone.utc).strftime("%Y-%m-01")
    with db.db() as conn, db.tx(conn):
        settings = db.get_settings(conn,ctx.user_id)
        if ctx.job_id and conn.execute("SELECT 1 FROM analytics_calls WHERE user_id=? AND job_id=? AND cache_key=? "
            "AND status IN ('reserved','unknown','done','failed')",(ctx.user_id,ctx.job_id,key)).fetchone():
            raise AnalyticsError("Этот аналитический запрос уже выполнялся. Повторная оплата остановлена.")
        count = conn.execute("SELECT COUNT(*) FROM analytics_calls WHERE user_id=? AND created_at>=? "
            "AND status NOT IN ('cache','blocked')",(ctx.user_id,month)).fetchone()[0]
        if count>=settings["analytics_calls_per_month"]:
            raise llm.BudgetExceeded("Лимит аналитических запросов за месяц исчерпан")
        if group:
            rows = conn.execute("SELECT provider,status,cost_usd FROM analytics_calls WHERE user_id=? AND budget_group=? "
                "AND status NOT IN ('cache','blocked')",(ctx.user_id,group)).fetchall()
            if len(rows)>=settings["analytics_calls_per_idea"]:
                raise llm.BudgetExceeded("Лимит аналитических запросов на разбор исчерпан")
            cost = sum(row["cost_usd"] if row["cost_usd"] is not None else
                config.ANALYTICS_CALL_RESERVE_USD if row["provider"]=="dataforseo" else 0 for row in rows)
            if cost+estimate>settings["analytics_per_idea_usd"]+1e-9:
                raise llm.BudgetExceeded("Лимит расходов на аналитические данные исчерпан")
        if provider=="keepa":
            spent_units = conn.execute("SELECT COALESCE(SUM(units),0) FROM analytics_calls WHERE user_id=? AND provider='keepa' "
                "AND created_at>=? AND status NOT IN ('cache','blocked')",(ctx.user_id,month)).fetchone()[0]
            if spent_units+units>settings["keepa_tokens_per_month"]:
                raise llm.BudgetExceeded("Лимит токенов Keepa за месяц исчерпан")
        cur = conn.execute("INSERT INTO analytics_calls(user_id,job_id,budget_group,provider,endpoint,cache_key,status,units,unit,created_at) "
            "VALUES(?,?,?,?,?,?,'reserved',?,?,?)",(ctx.user_id,ctx.job_id,group,provider,endpoint,key,units,
            "token" if provider=="keepa" else "request",db.now_iso()))
        call_id = cur.lastrowid
    reservation = f"analytics:{call_id}:{uuid.uuid4().hex}"
    try:
        llm.reserve(ctx,estimate,reservation)
    except Exception:
        with db.db() as conn:
            conn.execute("UPDATE analytics_calls SET status='blocked',units=0 WHERE id=?",(call_id,))
        raise
    raw = None
    cost = None
    try:
        response = _send(ctx.user_id,provider,endpoint,payload)
        if response.status_code>=400:
            raise AnalyticsError(f"HTTP {response.status_code}")
        if len(response.content)>6_000_000:
            raise AnalyticsError("Слишком большой ответ источника")
        raw = response.json()
        if provider=="dataforseo":
            cost = number(raw.get("cost"))
            if raw.get("status_code")!=20000 or not raw.get("tasks") or any(t.get("status_code")!=20000 for t in raw["tasks"]):
                code = (raw.get("tasks") or [{}])[0].get("status_code",raw.get("status_code"))
                raise AnalyticsError(f"DataForSEO status {code}")
            if cost is None:
                raise AnalyticsError("DataForSEO не вернул стоимость запроса")
        if raw.get("error") or (provider=="searchapi" and (raw.get("search_metadata") or {}).get("status") not in (None,"Success","success")):
            raise AnalyticsError("Источник вернул ошибку или незавершённый ответ")
        if provider=="searchapi" and not isinstance(raw.get("ads" if endpoint=="meta_ad_library" else "shopping_results"),list):
            raise AnalyticsError("Структура ответа SearchApi не подтверждена")
        if provider=="keepa" and not isinstance(raw.get("products"),list):
            raise AnalyticsError("Структура ответа Keepa не подтверждена")
        if provider=="keepa":
            units = number(raw.get("tokensConsumed")) or units
        raw = _safe(raw)
        raw["_retrieved_at"] = db.now_iso()
        with db.db() as conn, db.tx(conn):
            conn.execute("UPDATE analytics_calls SET status='done',cost_usd=?,units=? WHERE id=?",(cost,units,call_id))
            db.add_usage(conn,ctx.user_id,ctx.job_id,provider,endpoint,0,0,0,cost or 0,"analytics:"+endpoint)
            conn.execute("DELETE FROM budget_reservations WHERE reservation_key=?",(reservation,))
        db.cache_put(key,raw,ttl)
        db.source_ok(f"analytics:{ctx.user_id}:{provider}")
        return raw
    except Exception as exc:
        error = str(exc) if isinstance(exc,AnalyticsError) else type(exc).__name__
        with db.db() as conn, db.tx(conn):
            conn.execute("UPDATE analytics_calls SET status='unknown',cost_usd=?,error=? WHERE id=?",(cost,error,call_id))
            # An ambiguous failure may have been billed. Hold a conservative estimate in the USD budget.
            db.add_usage(conn,ctx.user_id,ctx.job_id,provider+"_uncertain",endpoint,0,0,0,
                         cost if cost is not None else estimate,"analytics:uncertain")
            conn.execute("DELETE FROM budget_reservations WHERE reservation_key=?",(reservation,))
        db.source_fail(f"analytics:{ctx.user_id}:{provider}",error)
        raise AnalyticsError(error) from None


def credentials_missing(provider):
    return NeedsConnection(PROVIDERS[provider]["name"]+" ожидает ключ подключения")


class NeedsConnection(AnalyticsError):
    pass


def _result(raw):
    return (raw.get("tasks") or [{}])[0].get("result") or []


def _bundle(raw, provider, kind, market, query, source_url, metrics, **extra):
    return {"provider":provider,"kind":kind,"market":market,"query":query,"source_url":source_url,
        "keywords":[part.strip() for part in query.split(",") if part.strip()],
        "retrieved_at":raw["_retrieved_at"],"cached":bool(raw.get("_from_cache")),
        "status":"ok" if metrics else "no_data","metrics":metrics,**extra}


def trends(ctx, keywords, market):
    keywords = keywords[:5]
    raw = request(ctx,"dataforseo","keywords_data/google_trends/explore/live",{
        "keywords":keywords,"location_code":LOCATION[market],"language_code":"en",
        "time_range":"past_5_years","item_types":["google_trends_graph"]},86400*7)
    results = _result(raw)
    if results and results[0].get("location_code") not in (None,LOCATION[market]):
        raise AnalyticsError("Страна Trends не совпала с запросом")
    result = results[0] if results else {}
    series = {keyword:[] for keyword in keywords}
    for item in result.get("items") or []:
        if item.get("type")!="google_trends_graph":
            continue
        returned = item.get("keywords") or keywords
        for point in item.get("data") or []:
            if point.get("missing_data"):
                continue
            for index,value in enumerate(point.get("values") or []):
                value = number(value)
                if index<len(returned) and returned[index] in series and value is not None and value<=100:
                    series[returned[index]].append({"date":point.get("date_from"),"value":value})
    metrics = []
    stats = {}
    for keyword,points in series.items():
        points.sort(key=lambda p:p["date"] or "")
        if not points:
            continue
        last_date = date.fromisoformat(points[-1]["date"])
        recent = [p["value"] for p in points if (last_date-date.fromisoformat(p["date"])).days<90]
        prior = [p["value"] for p in points if 365<=(last_date-date.fromisoformat(p["date"])).days<455]
        change = (mean(recent)/mean(prior)-1)*100 if recent and prior and mean(prior)>0 else None
        peak = max(points,key=lambda p:p["value"])
        stats[keyword] = {"index_90d":mean(recent) if recent else None,"yoy_pct":change,"peak":peak}
        metrics.append({"keyword":keyword,"name":keyword+" — средний интерес за 90 дней","value":round(mean(recent),2),"unit":"индекс 0–100"})
        if change is not None:
            metrics.append({"keyword":keyword,"name":keyword+" — к тем же 90 дням год назад","value":round(change,1),"unit":"%"})
    url = "https://trends.google.com/trends/explore?"+urlencode({"date":"today 5-y","geo":COUNTRY[market],"q":",".join(keywords)})
    all_dates = sorted(p["date"] for points in series.values() for p in points if p["date"])
    return _bundle(raw,"dataforseo","trends_api",market,", ".join(keywords),url,metrics,series=series,stats=stats,
        period=f"{all_dates[0]} — {all_dates[-1]}" if all_dates else None,
        limitation="Относительный поисковый интерес. Отдельные выгрузки US и AE не сравниваются по абсолютному индексу. Не продажи.")


def volumes(ctx, keywords, market, engine):
    endpoint = "keywords_data/"+("google_ads" if engine=="google" else "bing")+"/search_volume/live"
    raw = request(ctx,"dataforseo",endpoint,{"keywords":keywords[:8],"location_code":LOCATION[market],"language_code":"en"},86400*30)
    rows = []
    metrics = []
    for item in _result(raw):
        if item.get("location_code") not in (None,LOCATION[market]):
            raise AnalyticsError("Страна объёма поиска не совпала с запросом")
        volume = number(item.get("search_volume"))
        if volume is None:
            continue
        row = {"keyword":item.get("keyword"),"search_volume":volume,"cpc":number(item.get("cpc")),
            "competition":item.get("competition"),"monthly_searches":item.get("monthly_searches") or []}
        rows.append(row)
        metrics.append({"keyword":row["keyword"],"name":str(row["keyword"])+" — "+engine,"value":volume,"unit":"запросов/месяц"})
    periods = sorted(f"{int(p['year']):04d}-{int(p['month']):02d}" for row in rows for p in row["monthly_searches"]
                     if p.get("year") and p.get("month"))
    return _bundle(raw,"dataforseo","search_volume_api",market,", ".join(keywords),
        "https://dataforseo.com/"+"apis/keywords-data-api",metrics,engine=engine,rows=rows,
        period=f"{periods[0]} — {periods[-1]}" if periods else None,
        limitation="Оценка объёма запросов, без суммирования близких ключей. Ставка за клик не равна стоимости заказа.")


def ads(ctx, keyword, market):
    raw = request(ctx,"searchapi","meta_ad_library",{"engine":"meta_ad_library","q":keyword,
        "country":COUNTRY[market].lower(),"active_status":"active","ad_type":"all"},86400)
    country = (raw.get("search_parameters") or {}).get("country")
    if country and country.upper()!=COUNTRY[market]:
        raise AnalyticsError("Страна рекламной библиотеки не совпала с запросом")
    rows = []
    today = datetime.now(timezone.utc).date()
    for item in (raw.get("ads") or [])[:20]:
        start = str(item.get("start_date") or "")[:10]
        try:
            age = max(0,(today-date.fromisoformat(start)).days)
        except ValueError:
            age = None
        identifier = str(item.get("ad_archive_id") or "")
        if not identifier.isdigit():
            continue
        rows.append({"id":identifier,"advertiser":item.get("page_name") or (item.get("snapshot") or {}).get("page_name"),
            "start_date":start,"active_days":age,"active":item.get("is_active"),
            "url":"https://www.facebook.com/ads/library/?id="+identifier})
    metrics = [{"name":"Объявлений в полученной выборке","value":len(rows),"unit":"объявлений"}] if rows else []
    if rows:
        metrics.append({"name":"Активны не менее 30 дней в выборке","value":sum(r["active"] is True and (r["active_days"] or 0)>=30 for r in rows),"unit":"объявлений"})
    return _bundle(raw,"searchapi","ad_library_api",market,keyword,
        "https://www.facebook.com/ads/library/?"+urlencode({"country":COUNTRY[market],"q":keyword,"active_status":"active","ad_type":"all"}),
        metrics,ads=rows,reported_total=(raw.get("search_information") or {}).get("total_results"),
        period=today.isoformat(),limitation="Одна страница результатов. Объявления и длительность показа не доказывают продажи, прибыль или ROAS.")


def shopping(ctx, keyword, market):
    raw = request(ctx,"searchapi","google_shopping",{"engine":"google_shopping","q":keyword,"gl":COUNTRY[market].lower(),"hl":"en"},86400)
    returned = (raw.get("search_parameters") or {}).get("gl")
    if returned and returned.upper()!=COUNTRY[market]:
        raise AnalyticsError("Страна предложений не совпала с запросом")
    rows = []
    for item in (raw.get("shopping_results") or [])[:20]:
        price = number(item.get("extracted_price"))
        label = str(item.get("price") or "")
        currency = "AED" if "AED" in label.upper() or "د.إ" in label else "USD" if market=="USA" and "$" in label else None
        if price is None or currency!=config.MARKET_CURRENCY[market]:
            continue
        rows.append({"title":item.get("title"),"price":price,"currency":currency,
            "seller":item.get("seller") or item.get("source"),"url":item.get("product_link") or item.get("link")})
    metrics = [{"name":"Медиана цен предложений в выборке","value":round(median(r["price"] for r in rows),2),
                "unit":config.MARKET_CURRENCY[market]},
               {"name":"Предложений с распознанной ценой","value":len(rows),"unit":"предложений"}] if rows else []
    return _bundle(raw,"searchapi","shopping_api",market,keyword,
        "https://www.google.com/search?"+urlencode({"tbm":"shop","q":keyword,"gl":COUNTRY[market]}),metrics,
        products=rows,period=datetime.now(timezone.utc).date().isoformat(),
        limitation="Выборка предложений по запросу, без подтверждения сопоставимости комплектации, налогов и доставки. Не объём продаж.")


def asins_from_urls(urls):
    found = []
    for url in urls:
        parsed = urlsplit(str(url))
        if parsed.hostname not in ("amazon.com","www.amazon.com","smile.amazon.com"):
            continue
        match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$|\?)",parsed.path,re.I)
        if match and match[1].upper() not in found:
            found.append(match[1].upper())
    return found[:3]


def keepa(ctx, asins):
    asins = [value for value in asins[:3] if re.fullmatch(r"[A-Z0-9]{10}",value)]
    if not asins:
        raise AnalyticsError("Для Keepa нужны ссылки на сопоставимые товары Amazon.com с ASIN")
    raw = request(ctx,"keepa","product",{"domain":1,"asin":",".join(asins),"stats":90,"history":1,"days":365,"update":-1},86400,units=len(asins))
    products = []
    metrics = []
    for item in raw.get("products") or []:
        if item.get("asin") not in asins or item.get("domainId")!=1:
            continue
        stats = item.get("stats") or {}
        current = stats.get("current") or []
        avg = stats.get("avg90") or []
        def at(values,index,divisor=1):
            value = number(values[index]) if len(values)>index else None
            return value/divisor if value is not None else None
        price = at(current,1,100)
        rank = at(current,3)
        sold = number(item.get("monthlySold"))
        product = {"asin":item["asin"],"title":item.get("title"),"price_usd":price,"mean_price_90d_usd":at(avg,1,100),
            "sales_rank":rank,"monthly_sold_lower_bound":sold,"history":(item.get("csv") or [])[:4],
            "last_update_keepa_minutes":item.get("lastUpdate"),"url":"https://www.amazon.com/dp/"+item["asin"]}
        products.append(product)
        if price is not None:
            metrics.append({"name":item["asin"]+" — текущая цена New","value":price,"unit":"USD"})
        if rank is not None:
            metrics.append({"name":item["asin"]+" — sales rank","value":rank,"unit":"место в категории"})
        if sold is not None:
            metrics.append({"name":item["asin"]+" — нижняя граница месячных покупок","value":sold,"unit":"покупок, по данным Amazon/Keepa"})
    return _bundle(raw,"keepa","marketplace_api","USA",", ".join(asins),"https://keepa.com/",metrics,products=products,
        period="История до 365 дней; статистика 90 дней",limitation="Сопоставимые товары Amazon.com. Sales rank не равен числу продаж. monthlySold — нижняя граница, не продажи будущего Shopify-магазина.")


def save_bundle(conn, idea_id, bundle):
    fingerprint = hashlib.sha256(db.jdump({key:bundle.get(key) for key in ("provider","kind","market","query","retrieved_at")}).encode()).hexdigest()
    bundle = {**bundle,"fingerprint":fingerprint}
    exists = conn.execute("SELECT id FROM evidence WHERE idea_id=? AND kind=? AND payload LIKE ?",(idea_id,bundle["kind"],'%"fingerprint": "'+fingerprint+'"%')).fetchone()
    if not exists:
        conn.execute("INSERT INTO evidence(idea_id,kind,source,market,url,payload,period,retrieved_at) VALUES(?,?,?,?,?,?,?,?)",
            (idea_id,bundle["kind"],PROVIDERS[bundle["provider"]]["name"],bundle["market"],bundle["source_url"],
             db.jdump(bundle),bundle.get("period"),bundle["retrieved_at"]))
    return bundle


def collect(ctx, keywords, markets=None, existing=None):
    keywords = list(dict.fromkeys(str(value).strip()[:100] for value in keywords if str(value).strip()))[:5]
    if not keywords:
        return []
    out = []
    for market in markets or config.MARKETS:
        calls = [("trends_api","dataforseo",lambda:trends(ctx,keywords,market)),
                 ("search_volume_google","dataforseo",lambda:volumes(ctx,keywords,market,"google")),
                 ("search_volume_bing","dataforseo",lambda:volumes(ctx,keywords,market,"bing")),
                 ("ad_library_api","searchapi",lambda:ads(ctx,keywords[0],market)),
                 ("shopping_api","searchapi",lambda:shopping(ctx,keywords[0],market))]
        for kind,provider,call in calls:
            canonical_kind = "search_volume_api" if kind.startswith("search_volume_") else kind
            engine = "google" if kind=="search_volume_google" else "bing" if kind=="search_volume_bing" else None
            saved = next((bundle for bundle in existing or [] if bundle.get("kind")==canonical_kind and bundle.get("market")==market
                and (not engine or bundle.get("engine")==engine) and bundle.get("status")=="ok" and fresh(bundle)
                and keywords[0].casefold() in {value.casefold() for value in bundle.get("keywords",[])}) ,None)
            if saved:
                out.append({**saved,"cached":True})
                continue
            if not credentials.ready(ctx.user_id,provider):
                out.append({"kind":kind,"market":market,"provider":provider,"status":"needs_connection"})
                continue
            try:
                out.append(call())
            except Exception as exc:
                error = str(exc) if isinstance(exc,(AnalyticsError,llm.BudgetExceeded)) else type(exc).__name__
                out.append({"kind":kind,"market":market,"provider":provider,"status":"budget" if isinstance(exc,llm.BudgetExceeded) else "unavailable","error":error})
    return out


def fresh(bundle, days=35):
    try:
        age = datetime.now(timezone.utc)-datetime.fromisoformat(bundle["retrieved_at"])
        periods = [int(year) for year in re.findall(r"\b20\d{2}\b",str(bundle.get("period") or ""))]
        return 0<=age.total_seconds()<=days*86400 and (not periods or max(periods)>=datetime.now(timezone.utc).year-2)
    except (KeyError,ValueError,TypeError):
        return False


def attach(report, evidence):
    """Rebuild facts from immutable saved API evidence. Model-supplied metrics are not trusted."""
    bundles = []
    unique = set()
    queries = {str(value).casefold().strip() for value in report.get("analytics_queries") or []}
    asins = set(report.get("analytics_asins") or [])
    for row in sorted(evidence,key=lambda r:r["id"],reverse=True):
        if row["kind"] not in MEASUREMENT_KINDS:
            continue
        bundle = db.jload(row["payload"],{})
        if bundle.get("kind")=="marketplace_api":
            if not asins.intersection(bundle.get("keywords") or []):
                continue
        elif not queries.intersection(str(value).casefold().strip() for value in bundle.get("keywords") or []):
            continue
        key = (bundle.get("kind"),bundle.get("engine"),bundle.get("market"),bundle.get("query"))
        if key in unique:
            continue
        unique.add(key)
        bundle["evidence_id"] = row["id"]
        bundles.append(bundle)
    report["analytics"] = bundles
    for market in config.MARKETS:
        mr = report.setdefault("market",{}).setdefault("markets",{}).setdefault(market,{})
        available = [b for b in bundles if b.get("market")==market and b.get("status")=="ok" and fresh(b)
                     and b.get("kind") in ("trends_api","search_volume_api","marketplace_api") and b.get("metrics")]
        mr["analytics_status"] = "ok" if available else "missing"
        mr["analytics_count"] = len(available)
        values = [number(row.get("search_volume")) for b in available if b.get("kind")=="search_volume_api"
                  for row in b.get("rows",[]) if number(row.get("search_volume")) is not None]
        if values:
            volume = max(values)  # related keywords and overlapping search audiences are never summed
            score = round(min(10,2*math.log10(1+volume)),1)
            mr.setdefault("scores",{})["demand"] = {"score":score,
                "why":f"Измеренный поисковый интерес: максимум {volume:g} запросов/мес по одному ключу. Формула 2×log10(1+объём), максимум 10. Не прогноз продаж.",
                "method":"search_interest_v1","source":"analytics_api"}


def connections(user_id):
    with db.db() as conn:
        checks = {row["source"]:dict(row) for row in conn.execute("SELECT * FROM source_checks")}
    out = []
    for provider,definition in PROVIDERS.items():
        check = checks.get(f"analytics:{user_id}:{provider}",{})
        status = "нужен ключ"
        if credentials.ready(user_id,provider):
            status = "ожидает проверки"
            if check.get("last_ok_at"):
                status = "подключён"
            if check.get("last_error_at") and check["last_error_at"]>(check.get("last_ok_at") or ""):
                status = "ошибка подключения"
        out.append({**definition,"id":provider,"fields":credentials.FIELDS[provider],"status":status,"check":check})
    return out


def screen_candidates(ctx, candidates, settings):
    """Batch-check independent candidate queries before paying for a full analysis."""
    if config.FAKE_LLM:
        return candidates
    if not credentials.ready(ctx.user_id,"dataforseo"):
        for candidate in candidates:
            candidate["analytics_screen"] = {"status":"needs_connection"}
            if settings.get("require_analytics"):
                candidate["eligible"] = False
                candidate["screening_reasons"].append("Нужны аналитические измерения DataForSEO")
        return candidates
    keywords = [str(c.get("research_keyword") or "").strip() for c in candidates[:5]]
    keywords = list(dict.fromkeys(k for k in keywords if len(k)>2))
    if not keywords:
        return candidates
    market = settings.get("market_priority")
    market = market if market in config.MARKETS else "USA"
    bundles = []
    for call in (lambda:trends(ctx,keywords,market),lambda:volumes(ctx,keywords,market,"google"),lambda:volumes(ctx,keywords,market,"bing")):
        try:
            bundles.append(call())
        except (AnalyticsError,llm.BudgetExceeded):
            continue
    for candidate in candidates:
        keyword = str(candidate.get("research_keyword") or "").strip()
        selected = []
        for bundle in bundles:
            metrics = [metric for metric in bundle.get("metrics",[]) if str(metric.get("keyword","")).casefold()==keyword.casefold()]
            if not metrics:
                continue
            selected.append({**bundle,"query":keyword,"keywords":[keyword],"metrics":metrics,
                "rows":[row for row in bundle.get("rows",[]) if str(row.get("keyword","")).casefold()==keyword.casefold()],
                "series":{k:v for k,v in bundle.get("series",{}).items() if k.casefold()==keyword.casefold()},
                "stats":{k:v for k,v in bundle.get("stats",{}).items() if k.casefold()==keyword.casefold()}})
        volumes_found = [row["search_volume"] for bundle in selected for row in bundle.get("rows",[])]
        candidate["analytics_screen"] = {"status":"ok" if selected else "no_data","market":market,
            "keyword":keyword,"monthly_volume_max":max(volumes_found) if volumes_found else None,"bundles":selected}
        if not selected and settings.get("require_analytics"):
            candidate["eligible"] = False
            candidate["screening_reasons"].append("Не получены измерения по точному запросу кандидата")
        elif volumes_found and max(volumes_found)==0:
            candidate["eligible"] = False
            candidate["screening_reasons"].append("Полученные объёмы поиска равны нулю")
        elif volumes_found:
            candidate["screening_score"] += min(2,math.log10(1+max(volumes_found))/3)
    return sorted(candidates,key=lambda c:(c["eligible"],c["screening_score"]),reverse=True)


def sparkline(points):
    clean = [number(point.get("value")) for point in points]
    clean = [value for value in clean if value is not None and value<=100]
    if len(clean)<2:
        return ""
    return " ".join(f"{index*600/(len(clean)-1):.1f},{100-value:.1f}" for index,value in enumerate(clean))
