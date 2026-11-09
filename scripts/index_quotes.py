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
        try:
            found[name] = fetch_yahoo_index(yahoo, label)
        except Exception as first:
            try:
                found[name] = fetch_stooq_index(stooq, label)
            except Exception as second:
                errors.append(f"{name}: Yahoo: {first}; Stooq: {second}")
    return found, errors
