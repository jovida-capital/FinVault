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

        return {
            'price': round(price, 4),
            'currency': currency,
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

    # Extract unique tickers
    tickers = {}
    for inv in state.get('investissements', []):
        ticker = (inv.get('ticker') or '').strip()
        if ticker and ticker not in tickers:
            tickers[ticker] = inv.get('nom', ticker)

    print(f"Found {len(tickers)} tickers: {', '.join(tickers.keys())}")

    # Fetch prices
    prices = {}
    for i, (ticker, nom) in enumerate(tickers.items()):
        print(f"  [{i+1}/{len(tickers)}] {ticker} ({nom[:40]})...", end=' ', flush=True)
        result = fetch_yahoo(ticker)
        if result:
            prices[ticker] = result
            print(f"✓ {result['price']} {result['currency']} ({len(result['history'])} pts)")
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
