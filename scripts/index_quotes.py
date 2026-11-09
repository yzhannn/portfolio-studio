"""Daily public market-index snapshots. Prices are indicative, not real-time."""
import csv
import datetime as dt
import io
import json
import re
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

TPEX_ENDPOINTS = [
    "https://www.tpex.org.tw/openapi/v1/tpex_index",
    "https://www.tpex.org.tw/openapi/v1/tpex_reward_index",
]
US_INDICES = {
    "SPX": ("^GSPC", "^spx", "S&P 500"),
    "IXIC": ("^IXIC", "^ndq", "NASDAQ Composite"),
}


def _request(url, max_bytes=2_000_000):
    request = urllib.request.Request(url, headers={
        "Accept": "application/json,text/csv,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    })
    with urllib.request.urlopen(request, timeout=18) as response:
        if response.status != 200:
            raise ValueError(f"HTTP {response.status}")
        payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError("Index response unexpectedly large")
    return payload.decode("utf-8-sig")


def _date(value):
    value = str(value or "").strip().replace("-", "/").replace(".", "/")
    if re.fullmatch(r"\d{7,8}", value):
        if len(value) == 7:
            value = value[:3] + "/" + value[3:5] + "/" + value[5:]
        else:
            value = value[:4] + "/" + value[4:6] + "/" + value[6:]
    match = re.search(r"(\d{3,4})/(\d{1,2})/(\d{1,2})", value)
    if not match:
        return None
    year, month, day = (int(x) for x in match.groups())
    if year < 1911:
        year += 1911
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def _positive(value, minimum=1, maximum=100_000):
    try:
        number = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None
    return number if minimum < number < maximum else None


def parse_tpex(rows):
    if not isinstance(rows, list):
        raise ValueError("TPEx index must be a JSON list")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        date = _date(next((row[k] for k in ("資料日期", "Date", "date", "TradeDate") if row.get(k)), None))
        price = _positive(next((row[k] for k in ("收市", "櫃買指數", "收市指數", "Close", "ClosingIndex", "Index") if row.get(k)), None), 50, 5000)
        if date and price:
            result.append({"price": price, "date": date, "source": "TPEx 官方櫃買指數"})
    if not result:
        raise ValueError("TPEx response has no valid index/date rows")
    return max(result, key=lambda value: value["date"])


def fetch_tpex_index():
    failures = []
    for url in TPEX_ENDPOINTS:
        try:
            return parse_tpex(json.loads(_request(url, 2_000_000)))
        except Exception as error:
            failures.append(str(error))
    raise RuntimeError("; ".join(failures))


def parse_yahoo_chart(payload, code):
    data = payload.get("chart", {}).get("result", [None])
    result = data[0] if data else None
    if not isinstance(result, dict):
        raise ValueError("Yahoo index chart missing result")
    dates = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or []
    closes = quotes[0].get("close", []) if quotes and isinstance(quotes[0], dict) else []
    latest = []
    for timestamp, raw_price in zip(dates, closes):
        price = _positive(raw_price, 100, 100_000)
        if not price:
            continue
        day = dt.datetime.fromtimestamp(timestamp, ZoneInfo("America/New_York")).date().isoformat()
        latest.append({"price": price, "date": day, "source": f"Yahoo Finance · {code}"})
    if not latest:
        raise ValueError("Yahoo index has no valid close prices")
    return max(latest, key=lambda v: v["date"])


def fetch_yahoo_index(symbol, code):
    failures = []
    escaped = urllib.parse.quote(symbol, safe="")
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        try:
            url = f"https://{host}/v8/finance/chart/{escaped}?range=10d&interval=1d"
            return parse_yahoo_chart(json.loads(_request(url)), code)
        except Exception as error:
            failures.append(f"{host}: {error}")
    raise RuntimeError("; ".join(failures))


def parse_stooq_csv(raw, code):
    rows = csv.DictReader(io.StringIO(raw))
    result = []
    for row in rows:
        date = _date(row.get("Date"))
        price = _positive(row.get("Close"), 100, 100_000)
        if date and price:
            result.append({"price": price, "date": date, "source": f"Stooq · {code}"})
    if not result:
        raise ValueError("Stooq index missing daily prices")
    return max(result, key=lambda v: v["date"])



def fetch_nasdaq_index(ticker, label):
    """Nasdaq public quote endpoint; requires a recent dated index value."""
    url = f"https://api.nasdaq.com/api/quote/{ticker}/info?assetclass=index"
    data = json.loads(_request(url, 1_500_000)).get("data")
    if not isinstance(data, dict) or str(data.get("symbol", "")).upper() != ticker:
        raise ValueError("Nasdaq API has no matching index result")
    primary = data.get("primaryData") or {}
    price = _positive(primary.get("lastSalePrice") or primary.get("lastSale"), 100, 100000)
    raw_date = str(primary.get("lastTradeTimestamp") or data.get("lastTradeTimestamp") or "")
    match = re.search(r"[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4}", raw_date)
    date = None
    if match:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                date = dt.datetime.strptime(match.group(0), fmt).date()
                break
            except ValueError:
                pass
    now = dt.datetime.now(ZoneInfo("America/New_York")).date()
    if not price or not date or not 0 <= (now-date).days <= 7:
        raise ValueError("Nasdaq index result has no fresh dated quote")
    return {"price":price,"date":date.isoformat(),"source":"Nasdaq · "+label}


def fetch_google_index(name, label):
    """Scrape a public index page conservatively; refuse stale or ambiguous pages."""
    from html.parser import HTMLParser
    url = ("https://www.google.com/finance/quote/.INX:INDEXSP"
           if name == "SPX" else "https://www.google.com/finance/quote/.IXIC:INDEXNASDAQ")
    html = _request(url, 3_000_000)
    match = re.search(r'YMlKec\s+fxKbKc[^>]*>\s*([\d,]+\.\d+)\s*<', html)
    if not match:
        raise ValueError("Index price element not found")
    price = _positive(match.group(1), 100, 100000)
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.items = []
        def handle_data(self, part):
            if part.strip():
                self.items.append(part.strip())
    parser = Text()
    parser.feed(html)
    text = " ".join(parser.items)
    title = "S&P 500" if name == "SPX" else "Nasdaq Composite"
    found = re.search(re.escape(title)+r"\s+([\d,]+\.\d+)\s+([A-Z][a-z]{2}\s+\d{1,2}),", text)
    if not found or abs(float(found.group(1).replace(",", ""))-price)>0.5:
        raise ValueError("Google index title/quote mismatch")
    now = dt.datetime.now(ZoneInfo("America/New_York")).date()
    for year in (now.year, now.year-1):
        try:
            date = dt.datetime.strptime(found.group(2)+", "+str(year),"%b %d, %Y").date()
            if 0 <= (now-date).days <= 7:
                return {"price":price,"date":date.isoformat(),"source":"Google Finance · "+label}
        except ValueError:
            pass
    raise ValueError("Google index quote is stale")


def fetch_nasdaq_composite_site():
    """Secondary official Nasdaq Composite quote, never confuse with Nasdaq-100."""
    from html.parser import HTMLParser
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.items = []
        def handle_data(self, part):
            if part.strip():
                self.items.append(part.strip())
    parser = Text()
    parser.feed(_request("https://indexes.nasdaq.com/Index/Overview/COMP",3_000_000))
    text = " ".join(parser.items)
    match = re.search(r"DATA AS OF\s+(\d{1,2}/\d{1,2}/\d{4})\s+([\d,]+\.\d+)",text,re.I)
    if not match:
        raise ValueError("Nasdaq index overview missing quote")
    date = _date(match.group(1))
    now = dt.datetime.now(ZoneInfo("America/New_York")).date()
    price = _positive(match.group(2),100,100000)
    if not price or not date or not 0 <= (now-dt.date.fromisoformat(date)).days <= 7:
        raise ValueError("Nasdaq overview index quote stale")
    return {"price":price,"date":date,"source":"Nasdaq 指數官網 · NASDAQ Composite"}


def fetch_fred_index(series, label):
    """FRED publishes daily close for exact SP500/NASDAQCOM series, not ETF proxies."""
    if series not in ("SP500", "NASDAQCOM"):
        raise ValueError("Unsupported index series")
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=" + series + "&cosd=2026-10-01"
    data = _request(url, 2_000_000)
    values = []
    for row in csv.DictReader(io.StringIO(data)):
        day = _date(row.get("DATE") or row.get("observation_date"))
        price = _positive(row.get(series), 100, 100_000)
        if day and price:
            values.append({"price": price, "date": day, "source": "FRED · " + label + " 收盤"})
    if not values:
        raise ValueError("FRED has no valid close in returned CSV")
    return max(values, key=lambda value: value["date"])


def fetch_stooq_index(symbol, code):
    symbol = urllib.parse.quote(symbol, safe="")
    return parse_stooq_csv(_request(f"https://stooq.com/q/d/l/?s={symbol}&i=d&d1=20261001"), code)


def fetch_public_indices():
    """Return only successfully fetched indices; callers can retain prior dates on failure."""
    found, errors = {}, []
    try:
        found["TPEX"] = fetch_tpex_index()
    except Exception as error:
        errors.append(f"TPEX: {error}")
    for name, (yahoo, stooq, label) in US_INDICES.items():
        candidates = [("Nasdaq API", lambda n=name, l=label:fetch_nasdaq_index("SPX" if n=="SPX" else "COMP",l))]
        if name == "IXIC":
            candidates.append(("Nasdaq 官網",fetch_nasdaq_composite_site))
        candidates += [
            ("Google Finance",lambda n=name,l=label:fetch_google_index(n,l)),
            ("FRED",lambda n=name,l=label:fetch_fred_index("SP500" if n=="SPX" else "NASDAQCOM",l)),
            ("Yahoo",lambda y=yahoo,l=label:fetch_yahoo_index(y,l)),
            ("Stooq",lambda st=stooq,l=label:fetch_stooq_index(st,l)),
        ]
        failures = []
        for provider, fn in candidates:
            try:
                found[name] = fn()
                break
            except Exception as exc:
                failures.append(f"{provider}: {exc}")
        if name not in found:
            errors.append(name+": "+"; ".join(failures))
    return found, errors
