"""Best-effort U.S. stock/ETF delayed quotes from the public Nasdaq website screener.

This is NOT a licensed real-time quote feed. Data availability, coverage and
redistribution terms may change. No private portfolio data enters this job.
"""
import datetime as dt
import json
import re
import sys
import urllib.request
from zoneinfo import ZoneInfo

US_FEEDS = {
    "US-STOCKS": "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true",
    "US-ETFS": "https://api.nasdaq.com/api/screener/etf?tableonly=true&limit=10000&offset=0&download=true",
}

def fetch_json(url):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (PortfolioStudio-Personal/1.0)",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(request, timeout=27) as response:
        raw = response.read(12 * 1024 * 1024 + 1)
    if len(raw) > 12 * 1024 * 1024:
        raise ValueError("US feed unexpectedly large")
    result = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("US feed is not a JSON object")
    return result

def parse_nasdaq(payload, now):
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Nasdaq response missing data")
    nested = data.get("data") if isinstance(data.get("data"), dict) else {}
    table = nested.get("table") if isinstance(nested.get("table"), dict) else data.get("table", {})
    rows = data.get("rows") or nested.get("rows") or (table.get("rows") if isinstance(table, dict) else None)
    if not isinstance(rows, list):
        raise ValueError("Nasdaq response missing rows")
    asof = data.get("asof") or nested.get("asof") or (table.get("asof") if isinstance(table, dict) else None)
    provider_date = None
    if isinstance(asof, str):
        match = re.search(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})", asof)
        if match:
            for fmt in ("%b %d %Y", "%B %d %Y"):
                try:
                    provider_date = dt.datetime.strptime(" ".join(match.groups()), fmt).date().isoformat()
                    break
                except ValueError:
                    pass
        if provider_date is None:
            match = re.search(r"\d{4}-\d{2}-\d{2}", asof)
            if match:
                provider_date = match.group(0)
    fetched_date = now.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("symbol") or row.get("Symbol") or "").strip().upper()
        raw_price = str(row.get("lastsale") or row.get("lastSale") or row.get("lastSalePrice") or "")
        try:
            price = float(raw_price.replace("$", "").replace(",", "").strip())
        except ValueError:
            continue
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", ticker) or not (0 < price < 10000000):
            continue
        result[ticker] = {
            "price": price,
            "date": provider_date or fetched_date,
            "dateKind": "provider" if provider_date else "retrieved",
            "source": "NASDAQ-SCREENER",
        }
    return result

def fetch_us_quotes(now):
    result, errors, succeeded = {}, [], []
    for market, url in US_FEEDS.items():
        try:
            rows = parse_nasdaq(fetch_json(url), now)
            if len(rows) < 10:
                raise ValueError(f"Only {len(rows)} valid quotes; unexpected feed")
            result.update(rows)
            succeeded.append(market)
            print(f"{market}: OK ({len(rows)} USD symbols)", file=sys.stderr)
        except Exception as exc:
            errors.append(f"{market}: {type(exc).__name__}: {exc}")
            print(f"{market}: FAILED ({exc})", file=sys.stderr)
    return result, errors, succeeded
