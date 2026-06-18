import json, time, sys
from datetime import datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

REPO_RAW = "https://raw.githubusercontent.com/jovida-capital/FinVault/main/data/state.json"

def fetch_json(url, retries=3):
    for i in range(retries):
        try:
            req = Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'application/json,*/*',
            })
            resp = urlopen(req, timeout=15)
            return json.loads(resp.read())
        except (HTTPError, URLError) as e:
            print(f"  Retry {i+1}/{retries}: {e}")
            time.sleep(2)
    return None

def fetch_stooq_history(ticker):
    """Fallback: fetch full daily history from Stooq (no API key, works server-side)."""
    # Stooq ticker format: lowercase, same suffix as Yahoo (.pa, .de, etc.)
    stooq_ticker = ticker.lower()
    url = f"https://stooq.com/q/d/l/?s={stooq_ticker}&i=d"
    try:
        req = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/csv,*/*',
            'Referer': 'https://stooq.com/',
        })
        resp = urlopen(req, timeout=15)
        raw = resp.read().decode('utf-8', errors='ignore')
        lines = raw.strip().split('\n')
        if len(lines) < 2 or 'Date' not in lines[0]:
            return {}
        history = {}
        for line in lines[1:]:
            parts = line.split(',')
            if len(parts) < 5:
                continue
            date, close = parts[0], parts[4]
            try:
                cp = float(close)
                if cp > 0:
                    history[date] = round(cp, 4)
            except ValueError:
                continue
        return history
    except Exception as e:
        print(f"    Stooq fallback failed: {e}")
        return {}

def fetch_yahoo(ticker):
    """Fetch current price + 2y weekly history from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1wk&range=2y"
    data = fetch_json(url)
    if not data:
        url2 = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1wk&range=2y"
        data = fetch_json(url2)
    if not data:
        return None

    try:
        result = data['chart']['result'][0]
        meta   = result['meta']
        price  = meta.get('regularMarketPrice') or meta.get('chartPreviousClose')
        if not price or price <= 0:
            return None

        currency = meta.get('currency', 'EUR').upper()
        if currency == 'GBX':
            price = price / 100
            currency = 'GBP'
        # European ETFs (.PA, .AS, .DE, .MI) trade in EUR despite Yahoo sometimes returning USD
        if currency == 'USD' and any(ticker.upper().endswith(s) for s in ['.PA', '.AS', '.DE', '.MI']):
            currency = 'EUR'

        # Build price history
        history = {}
        timestamps = result.get('timestamp', [])
        closes = result.get('indicators', {}).get('quote', [{}])[0].get('close', [])
        for ts, close in zip(timestamps, closes):
            if close and close > 0:
                date = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
                val = close / 100 if currency == 'GBX' else close
                history[date] = round(val, 4)

        # Daily last 90 days
        url_d = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=3mo"
        data_d = fetch_json(url_d)
        if data_d:
            try:
                result_d = data_d['chart']['result'][0]
                ts_d = result_d.get('timestamp', [])
                cl_d = result_d.get('indicators', {}).get('quote', [{}])[0].get('close', [])
                for ts, close in zip(ts_d, cl_d):
                    if close and close > 0:
                        date = datetime.utcfromtimestamp(ts).strftime('%Y-%m-%d')
                        val = close / 100 if currency == 'GBX' else close
                        history[date] = round(val, 4)
            except Exception:
                pass

        name = meta.get('longName') or meta.get('shortName') or ticker
        return {
            'price': round(price, 4),
            'currency': currency,
            'name': name,
            'history': history,
            'updated': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        }
    except (KeyError, IndexError, TypeError) as e:
        print(f"  Parse error: {e}")
        return None

def main():
    print(f"=== FinVault Price Fetcher — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===")

    # Load state.json from repo or local
    state_path = "data/state.json"
    try:
        with open(state_path) as f:
            state = json.load(f)
        print(f"Loaded state.json ({len(state.get('investissements', []))} positions)")
    except FileNotFoundError:
        print(f"state.json not found at {state_path}, trying remote...")
        state = fetch_json(REPO_RAW)
        if not state:
            print("ERROR: could not load state.json")
            sys.exit(1)

    # Load PREVIOUS prices.json to preserve accumulated history
    # (self-building history for tickers with no chart data on Yahoo)
    prev_prices = {}
    try:
        with open('data/prices.json') as f:
            prev_data = json.load(f)
            prev_prices = prev_data.get('prices', {})
        print(f"Loaded previous prices.json ({len(prev_prices)} tickers)")
    except FileNotFoundError:
        print("No previous prices.json found (first run)")

    # Extract unique tickers
    tickers = {}
    for inv in state.get('investissements', []):
        ticker = (inv.get('ticker') or '').strip()
        if ticker and ticker not in tickers:
            tickers[ticker] = inv.get('nom', ticker)

    print(f"Found {len(tickers)} tickers: {', '.join(tickers.keys())}")

    # Fetch prices
    prices = {}
    today_str = datetime.utcnow().strftime('%Y-%m-%d')
    for i, (ticker, nom) in enumerate(tickers.items()):
        print(f"  [{i+1}/{len(tickers)}] {ticker} ({nom[:40]})...", end=' ', flush=True)
        result = fetch_yahoo(ticker)
        if result:
            # If Yahoo gave little/no history, try Stooq as a one-time backfill
            if len(result['history']) < 5:
                stooq_hist = fetch_stooq_history(ticker)
                if stooq_hist:
                    merged_stooq = dict(stooq_hist)
                    merged_stooq.update(result['history'])
                    result['history'] = merged_stooq
                    print(f"(+{len(stooq_hist)} pts via Stooq) ", end='')
            # Self-building history: merge with previous history if Yahoo/Stooq gave none/little
            prev_hist = prev_prices.get(ticker, {}).get('history', {})
            if prev_hist:
                merged = dict(prev_hist)
                merged.update(result['history'])  # new data wins on overlapping dates
                result['history'] = merged
            # Always record today's price as a history point
            result['history'][today_str] = result['price']
            prices[ticker] = result
            print(f"✓ {result['price']} {result['currency']} ({len(result['history'])} pts)")
        else:
            # Yahoo failed entirely — keep previous data if we have it (stale but not lost)
            if ticker in prev_prices:
                prices[ticker] = prev_prices[ticker]
                print(f"✗ Yahoo failed, kept previous ({prev_prices[ticker]['price']})")
            else:
                print("✗ no data")
        time.sleep(1.5)  # be polite

    # Write prices.json
    import os
    os.makedirs('data', exist_ok=True)
    with open('data/prices.json', 'w') as f:
        json.dump({
            'updated': datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'prices': prices
        }, f, indent=2)

    print(f"\n✓ Wrote data/prices.json ({len(prices)}/{len(tickers)} tickers)")

if __name__ == '__main__':
    main()
