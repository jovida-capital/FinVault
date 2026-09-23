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

        # Série quotidienne des 3 derniers mois.
        # C'est elle qui alimente J-1, J-2, J-3 : sans elle, seules subsistent
        # les bornes hebdomadaires (datées au lundi) et les valeurs héritées du
        # run précédent, ce qui donne un historique récent incohérent.
        # L'échec est donc signalé explicitement et non ignoré.
        daily_count = 0
        url_d = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=3mo"
        data_d = fetch_json(url_d)
        if not data_d:
            url_d2 = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=3mo"
            data_d = fetch_json(url_d2)
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
                        daily_count += 1
            except (KeyError, IndexError, TypeError) as e:
                print(f"\n    ATTENTION {ticker} : série quotidienne illisible ({e})", end='')
        if daily_count == 0:
            print(f"\n    ATTENTION {ticker} : aucune clôture quotidienne récupérée "
                  f"— J-1 et J-2 s'appuieront sur des données hebdomadaires ou héritées", end='')

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
            # Self-building history: merge with previous history if Yahoo gave none/little
            prev_hist = prev_prices.get(ticker, {}).get('history', {})
            if prev_hist:
                merged = dict(prev_hist)
                merged.update(result['history'])  # new data wins on overlapping dates
                result['history'] = merged
            # Le cours instantané ne sert que de valeur d'attente :
            # si Yahoo publie déjà une clôture pour aujourd'hui, elle fait foi.
            # Écraser cette clôture par regularMarketPrice réintroduirait une
            # valeur provisoire (dernier échange) au lieu du fixing de clôture.
            if today_str not in result['history']:
                result['history'][today_str] = result['price']
                print('(cours provisoire) ', end='')
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

    # Contrôle de couverture : les 5 derniers jours ouvrés doivent être présents
    from datetime import timedelta
    ouvres, d = [], datetime.utcnow().date()
    while len(ouvres) < 5:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            ouvres.append(d.strftime('%Y-%m-%d'))
    print("\nCouverture des 5 derniers jours ouvrés :")
    for ticker, info in prices.items():
        h = info.get('history', {})
        manquants = [j for j in ouvres if j not in h]
        etat = 'complet' if not manquants else 'manque ' + ', '.join(manquants)
        print(f"  {ticker:<14} {etat}")

if __name__ == '__main__':
    main()
