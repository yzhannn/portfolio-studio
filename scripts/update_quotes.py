#!/usr/bin/env python3
"""Fetch PUBLIC Taiwan market snapshots for GitHub Pages. No portfolio data is used.

The script runs during GitHub Actions deployment. The generated data/quotes.json is
served by GitHub Pages, avoiding browser CORS and separate serverless hosting.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from us_quotes import fetch_us_quotes
from index_quotes import fetch_public_indices

SOURCES = {
    'TWSE': 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL',
    'TPEx': 'https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes',
    'TAIEX': 'https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX',
}
OUTPUT = Path(__file__).resolve().parent.parent / 'data' / 'quotes.json'


def trading_date(raw):
    raw = re.sub(r'[^0-9]', '', str(raw or ''))
    if len(raw) == 7:
        iso = f'{int(raw[:3]) + 1911:04d}-{raw[3:5]}-{raw[5:7]}'
    elif len(raw) == 8:
        iso = f'{raw[:4]}-{raw[4:6]}-{raw[6:8]}'
    else:
        return None
    try:
        dt.date.fromisoformat(iso)
        return iso
    except ValueError:
        return None


def valid_price(value):
    value = str(value or '').strip().replace(',', '')
    try:
        n = float(value)
    except (ValueError, TypeError):
        return None
    return n if 0 < n < 10000000 and n == n else None


def load_json(url):
    # Official feeds occasionally return temporary HTML errors or reject cloud IPs.
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (compatible; PortfolioStudioMarketSnapshot/1.0)',
            })
            with urllib.request.urlopen(request, timeout=25) as response:
                if response.status != 200:
                    raise ValueError(f'HTTP {response.status}')
                raw = response.read(18 * 1024 * 1024 + 1)
                if len(raw) > 18 * 1024 * 1024:
                    raise ValueError('Response unexpectedly large')
                rows = json.loads(raw.decode('utf-8-sig'))
                if not isinstance(rows, list) or not rows:
                    raise ValueError('Expected nonempty JSON array')
                return rows
        except (urllib.error.URLError, ValueError, TimeoutError, UnicodeError) as ex:
            last_error = ex
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(str(last_error))


def stock_prices(rows, source):
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get('Code' if source == 'TWSE' else 'SecuritiesCompanyCode', '')).strip()
        price = valid_price(row.get('ClosingPrice' if source == 'TWSE' else 'Close'))
        day = trading_date(row.get('Date'))
        if symbol and re.fullmatch(r'[0-9A-Z]{3,8}', symbol) and price and day:
            prev = out.get(symbol)
            if not prev or day >= prev['date']:
                out[symbol] = {'price': price, 'date': day, 'source': source}
    if len(out) < 100:
        raise ValueError(f'Only {len(out)} valid {source} quotations; probable upstream schema change')
    return out


def taiex_index(rows):
    for row in rows:
        if not isinstance(row, dict) or row.get('指數') != '發行量加權股價指數':
            continue
        price, day = valid_price(row.get('收盤指數')), trading_date(row.get('日期'))
        if price and day:
            change = valid_price(row.get('漲跌點數'))
            sign = -1 if str(row.get('漲跌', '')).strip() == '-' else 1
            return {'price': price, 'date': day, 'change': change * sign if change else None, 'source': 'TWSE'}
    raise ValueError('TAIEX index not found in TWSE response')



FX_ENDPOINTS = [
    'https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.min.json',
    'https://latest.currency-api.pages.dev/v1/currencies/usd.min.json',
]


def fetch_daily_fx():
    """Public daily USD/TWD indicative FX quote, no API key or private data."""
    issues = []
    for endpoint in FX_ENDPOINTS:
        try:
            request = urllib.request.Request(endpoint, headers={
                'Accept': 'application/json',
                'User-Agent': 'Mozilla/5.0 (PortfolioStudio-PublicMarketSnapshot/1.0)',
            })
            with urllib.request.urlopen(request, timeout=12) as response:
                if response.status != 200:
                    raise ValueError(f'HTTP {response.status}')
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError('FX response exceeds size limit')
                data = json.loads(raw.decode('utf-8-sig'))
            rate = float(data['usd']['twd'])
            date = str(data['date'])
            dt.date.fromisoformat(date)
            if not 10 < rate < 100:
                raise ValueError('USD/TWD FX rate outside plausible range')
            return {
                'base': 'USD', 'quote': 'TWD', 'rate': rate,
                'date': date, 'source': 'Currency API (每日參考匯率)',
            }
        except Exception as exc:
            issues.append(f'{type(exc).__name__}: {exc}')
    raise RuntimeError('; '.join(issues))

def build_snapshot(fetcher=load_json, clock=None, fx_fetcher=fetch_daily_fx):
    if clock is None:
        clock = dt.datetime.now(dt.timezone.utc)
    quotes, indices, errors, succeeded = {}, {}, [], []
    for name, url in SOURCES.items():
        try:
            raw = fetcher(url)
            if name == 'TAIEX':
                indices['TAIEX'] = taiex_index(raw)
            else:
                # Never rely on the user's holdings list; these are publicly traded symbols.
                result = stock_prices(raw, name)
                for ticker, quote in result.items():
                    old = quotes.get(ticker)
                    if not old or quote['date'] >= old['date']:
                        quotes[ticker] = quote
            succeeded.append(name)
            print(f'{name}: OK', file=sys.stderr)
        except Exception as exc:
            errors.append(f'{name}: {type(exc).__name__}: {exc}')
            print(f'{name}: FAILED: {exc}', file=sys.stderr)
    us_quotes, us_errors, us_succeeded = fetch_us_quotes(clock)
    print(f'US quote coverage check: AAOI={us_quotes.get("AAOI", "MISSING")}', file=sys.stderr)
    errors.extend(us_errors)
    succeeded.extend(us_succeeded)
    try:
        fx = fx_fetcher()
        succeeded.append('USD/TWD')
        print(f'USD/TWD FX: OK ({fx["rate"]}, {fx["date"]})', file=sys.stderr)
    except Exception as exc:
        fx = None
        errors.append(f'USD/TWD FX: {type(exc).__name__}: {exc}')
        print(f'USD/TWD FX: FAILED: {exc}', file=sys.stderr)
    updated_indices, index_errors = fetch_public_indices()
    for name, quote in updated_indices.items():
        existing = indices.get(name)
        if not existing or quote["date"] >= existing.get("date", ""):
            indices[name] = quote
        succeeded.append(name)
        print(f'{name} index: OK ({quote["price"]}, {quote["date"]}, {quote["source"]})', file=sys.stderr)
    for item in index_errors:
        errors.append('Index ' + item)
        print('Index FAILED: ' + item, file=sys.stderr)

    # When an upstream index API is blocked, retain the last fetched quote
    # along with its original source and date, never substitute another index.
    if OUTPUT.exists():
        try:
            with OUTPUT.open('r', encoding='utf-8') as previous_file:
                previous = json.load(previous_file)
            for name, quote in (previous.get('indices') or {}).items():
                if name not in ('TAIEX', 'TPEX', 'SPX', 'IXIC'):
                    continue
                if not isinstance(quote, dict) or not valid_price(quote.get('price')):
                    continue
                original_date = str(quote.get('date') or '')
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', original_date):
                    continue
                if not indices.get(name) or original_date > indices[name].get('date', ''):
                    indices[name] = quote
        except Exception as exc:
            errors.append(f'Prior index cache: {type(exc).__name__}: {exc}')

    if not quotes:
        raise RuntimeError('Both stock markets unavailable. Keep the last successfully published site; do not overwrite it with empty prices.')
    return {
        'schema': 1,
        'asOf': clock.isoformat(timespec='seconds'),
        'priceType': 'official-daily-close',
        'isRealtime': False,
        'quotes': quotes,
        'usQuotes': us_quotes,
        'indices': indices,
        'fx': fx,
        'sourcesSucceeded': succeeded,
        'errors': errors,
    }


def main():
    data = build_snapshot()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open('w', encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, separators=(',', ':'))
    print(f'Wrote {OUTPUT}, {len(data["quotes"])} TW stock quotes, {len(data["usQuotes"])} US quotes, {len(data["indices"])} indices, FX: {data["fx"]["rate"] if data["fx"] else "unavailable"}')


if __name__ == '__main__':
    main()
