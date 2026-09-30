import json, time, sys
from datetime import datetime, timedelta, timezone, date


def _utcnow():
    """Horodatage UTC sans dependre de utcnow(), deprecie depuis Python 3.12."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _from_ts(ts):
    """Convertit un timestamp epoch en datetime naif UTC."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)
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
            print(f" Retry {i+1}/{retries}: {e}")
            time.sleep(2)
    return None


def fetch_rates():
    """Recupere les taux de change (devise -> EUR) depuis Yahoo Finance.

    Publies dans prices.json pour que FinVault n'ait plus a se fier a une
    valeur statique (state.rates cote client, jamais mise a jour toute
    seule) : la conversion des positions en devise etrangere (ex. C50U.PA
    en USD) suit alors le vrai cours, pas un taux fige au moment du
    developpement.
    """
    paires = {'USD': 'USDEUR=X', 'GBP': 'GBPEUR=X', 'CHF': 'CHFEUR=X'}
    rates = {}
    for devise, ticker in paires.items():
        data = None
        for hote in ('query1', 'query2'):
            data = fetch_json(f"https://{hote}.finance.yahoo.com/v8/finance/chart/"
                              f"{ticker}?interval=1d&range=5d")
            if data:
                break
        try:
            meta = data['chart']['result'][0]['meta']
            taux = meta.get('regularMarketPrice') or meta.get('chartPreviousClose')
            if taux and taux > 0:
                rates[devise] = round(taux, 6)
                print(f" Taux {devise}->EUR : {round(taux, 4)}")
            else:
                print(f" Taux {devise}->EUR : pas de valeur recuperee")
        except (KeyError, IndexError, TypeError):
            print(f" Taux {devise}->EUR : echec de recuperation")
        time.sleep(1)
    return rates


def _lire_series(data, gmtoffset, diviser_par_cent, decalage_fin_semaine=0):
    """Extrait {date: cloture} d'une reponse chart Yahoo."""
    serie = {}
    result = data['chart']['result'][0]
    timestamps = result.get('timestamp', []) or []
    quote = (result.get('indicators', {}).get('quote') or [{}])[0]
    closes = quote.get('close', []) or []
    for ts, close in zip(timestamps, closes):
        if not close or close <= 0:
            continue
        d = _from_ts(ts + (gmtoffset or 0))
        if decalage_fin_semaine:
            d = d + timedelta(days=decalage_fin_semaine)
        val = close / 100 if diviser_par_cent else close
        serie[d.strftime('%Y-%m-%d')] = round(val, 4)
    return serie


def fetch_yahoo(ticker):
    """Recupere le cours courant et l'historique quotidien sur 2 ans.

    L'historique provient d'une SEULE serie quotidienne. Les bougies
    hebdomadaires, utilisees auparavant, portent la cloture de fin de semaine
    mais sont horodatees au debut de celle-ci : chaque valeur se retrouvait
    datee cinq jours trop tot. Elles ne servent plus que de repli degrade, et
    sont alors recalees sur le dernier jour ouvre de leur semaine.
    """
    data = None
    for hote in ('query1', 'query2'):
        data = fetch_json(f"https://{hote}.finance.yahoo.com/v8/finance/chart/"
                          f"{ticker}?interval=1d&range=2y")
        if data:
            break
    if not data:
        return None

    try:
        result = data['chart']['result'][0]
        meta = result['meta']
        price = meta.get('regularMarketPrice') or meta.get('chartPreviousClose')
        if not price or price <= 0:
            return None

        gmtoffset = meta.get('gmtoffset', 0)

        # Devise. Le drapeau "cotation en pence" doit etre capture AVANT la
        # normalisation en GBP, sans quoi le test devient toujours faux et
        # l'historique reste en pence alors que le cours passe en livres.
        currency = (meta.get('currency') or 'EUR').upper()
        en_pence = (currency == 'GBX') # a capturer AVANT la normalisation
        if currency == 'GBX':
            price = price / 100
            currency = 'GBP'
        # La plupart des ETF europeens (.PA, .AS, .DE, .MI) sont libelles en
        # EUR meme lorsque Yahoo annonce USD dans ses metadonnees (cotation
        # croisee, cas WSRI.PA). Mais certains sont de VRAIES parts USD
        # cotees sur une place europeenne (ex. C50U.PA, "...UCITS ETF USD
        # Acc" — Boursorama le confirme en cotant nativement en USD) : le
        # nom du fonds le dit explicitement, donc on ne force pas l'EUR
        # dans ce cas, sous peine de fausser la valorisation dans l'autre
        # sens.
        nom_fonds = (meta.get('longName') or meta.get('shortName') or '').upper()
        if currency == 'USD' and 'USD' not in nom_fonds and any(
                ticker.upper().endswith(s) for s in ('.PA', '.AS', '.DE', '.MI')):
            currency = 'EUR'

        # ── Historique quotidien sur 2 ans : source unique et correctement datee
        history = _lire_series(data, gmtoffset, en_pence)
        source_hist = 'quotidien'

        if len(history) < 10:
            # Repli hebdomadaire, recalé sur le vendredi (dernier jour ouvre).
            print(f"\n REPLI {ticker} : serie quotidienne trop courte "
                  f"({len(history)} pts) — bascule sur l'hebdomadaire", end='')
            for hote in ('query1', 'query2'):
                dw = fetch_json(f"https://{hote}.finance.yahoo.com/v8/finance/chart/"
                                f"{ticker}?interval=1wk&range=2y")
                if dw:
                    hebdo = _lire_series(dw, gmtoffset, en_pence,
                                         decalage_fin_semaine=4)
                    hebdo.update(history) # le quotidien reste prioritaire
                    history = hebdo
                    source_hist = 'hebdomadaire recale'
                    break

        if not history:
            print(f"\n ATTENTION {ticker} : aucune cloture historique recuperee", end='')

        # ── Comblement du dernier jour via le quote (page sommaire Yahoo) ──
        # Le tableau des clotures quotidiennes de Yahoo publie la cloture
        # officielle avec un delai (parfois jusqu'au lendemain). A l'heure ou
        # tourne ce script (23h Paris, marche ferme), regularMarketPrice est
        # deja la cloture du jour — c'est exactement la valeur "Dernière
        # clôture" affichee sur la page sommaire de Yahoo Finance. On l'utilise
        # pour combler la date du jour quand le tableau historique ne l'a pas
        # encore, plutot que d'attendre un run ulterieur. regularMarketTime
        # donne la date exacte a laquelle rattacher cette valeur (pas de
        # supposition sur "aujourd'hui" : marche ferme, jour ferie, etc.).
        regular_time = meta.get('regularMarketTime')
        if regular_time and meta.get('regularMarketPrice'):
            date_quote = _from_ts(regular_time + (gmtoffset or 0)).strftime('%Y-%m-%d')
            if date_quote not in history:
                valeur_quote = meta['regularMarketPrice']
                if en_pence:
                    valeur_quote = valeur_quote / 100
                history[date_quote] = round(valeur_quote, 4)
                print(f"\n COMBLE {ticker} : {date_quote} absente du tableau "
                      f"historique — cloture prise sur le quote "
                      f"({round(valeur_quote, 4)})", end='')

        # ── Prix retenu : la DERNIERE CLOTURE de la serie (tableau historique
        # complete par le quote ci-dessus le cas echeant) ──
        quote_brut = price
        ecart_quote = None
        if history:
            derniere_cloture = history[max(history)]
            if derniere_cloture > 0:
                ecart_quote = (price - derniere_cloture) / derniere_cloture
                price = derniere_cloture # la cloture fait foi
                if abs(ecart_quote) > 0.05:
                    print(f"\n INFO {ticker} : quote {round(quote_brut, 4)} ecarte "
                          f"({ecart_quote * 100:+.1f} %) au profit de la cloture "
                          f"{derniere_cloture}", end='')
        else:
            print(f"\n ATTENTION {ticker} : aucune cloture — repli sur le quote "
                  f"{round(price, 4)}", end='')

        name = meta.get('longName') or meta.get('shortName') or ticker
        return {
            'price': round(price, 4),
            'quote_brut': round(quote_brut, 4),
            'ecart_quote_pct': round(ecart_quote * 100, 2) if ecart_quote is not None else None,
            'currency': currency,
            'name': name,
            'history': history,
            'source_hist': source_hist,
            'updated': _utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
        }
    except (KeyError, IndexError, TypeError) as e:
        print(f" Parse error: {e}")
        return None

def main():
    print(f"=== FinVault Price Fetcher — {_utcnow().strftime('%Y-%m-%d %H:%M UTC')} ===")

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
    prev_data = {}
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

    # Taux de change (devise -> EUR), publies pour que FinVault n'ait plus a
    # se fier a une valeur statique cote client.
    print("Fetching exchange rates...")
    rates = fetch_rates()

    # Fetch prices
    prices = {}
    for i, (ticker, nom) in enumerate(tickers.items()):
        print(f" [{i+1}/{len(tickers)}] {ticker} ({nom[:40]})...", end=' ', flush=True)
        result = fetch_yahoo(ticker)
        if result:
            # Self-building history: merge with previous history if Yahoo gave none/little
            prev_hist = prev_prices.get(ticker, {}).get('history', {})
            if prev_hist:
                merged = dict(prev_hist)
                merged.update(result['history']) # new data wins on overlapping dates
                # Purge des dates de week-end heritees. Elles proviennent des
                # anciennes bougies hebdomadaires, datees au dimanche par un
                # defaut de fuseau : la nouvelle serie quotidienne ne les
                # recouvre jamais, elles resteraient donc figees a vie.
                # On ne purge que si l'instrument ne cote pas le week-end,
                # deduit de la serie du jour plutot que suppose.
                cote_we = any(date.fromisoformat(d).weekday() >= 5
                              for d in result['history'])
                if not cote_we:
                    parasites = [d for d in merged
                                 if date.fromisoformat(d).weekday() >= 5]
                    for d in parasites:
                        del merged[d]
                    if parasites:
                        print(f"\n PURGE {ticker} : {len(parasites)} date(s) de "
                              f"week-end heritees supprimees", end='')
                result['history'] = merged
            # Le cours instantané ne sert que de valeur d'attente :
            # si Yahoo publie déjà une clôture pour aujourd'hui, elle fait foi.
            # L'historique n'est plus retouche : la cloture publiee par Yahoo
            # est prise telle quelle, sans seuil ni correction automatique.
            # Le prix retenu est toujours la derniere cloture de l'historique
            # (deja fixe dans fetch_yahoo(), avant meme la fusion ci-dessus).
            if result['history']:
                derniere = result['history'][max(result['history'])]
                if derniere > 0:
                    result['price'] = round(derniere, 4)
            prices[ticker] = result
            print(f"✓ {result['price']} {result['currency']} ({len(result['history'])} pts)")
        else:
            # Yahoo failed entirely — keep previous data if we have it (stale but not lost).
            if ticker in prev_prices:
                conserve = dict(prev_prices[ticker])
                prices[ticker] = conserve
                print(f"✗ Yahoo failed, kept previous ({conserve['price']})")
            else:
                print("✗ no data")
        time.sleep(1.5) # be polite

    # Tri chronologique systematique de l'historique de chaque ticker.
    # dict.update() (fusions ci-dessus, et l'hebdo -> quotidien dans
    # fetch_yahoo) ne reordonne jamais les cles deja presentes : un
    # historique construit par fusions successives peut donc se retrouver
    # dans un ordre non chronologique dans le JSON, meme si les valeurs
    # elles-memes sont correctes. Inoffensif pour ce script (qui compare
    # des dates, pas un ordre d'iteration), mais potentiellement trompeur
    # pour tout consommateur (FinVault) qui parcourrait l'historique en
    # supposant qu'il est deja trie.
    for ticker in prices:
        h = prices[ticker].get('history')
        if h:
            prices[ticker]['history'] = dict(sorted(h.items()))

    # Un taux manque (echec Yahoo pour cette devise precise) : on garde le
    # dernier taux connu plutot que de le faire disparaitre de prices.json.
    prev_rates = prev_data.get('rates', {})
    for devise, taux in prev_rates.items():
        if devise not in rates:
            rates[devise] = taux
            print(f" Taux {devise}->EUR : echec, conserve le precedent ({taux})")

    # Write prices.json
    import os
    os.makedirs('data', exist_ok=True)
    with open('data/prices.json', 'w') as f:
        json.dump({
            'updated': _utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'rates': rates,
            'prices': prices
        }, f, indent=2)

    print(f"\n✓ Wrote data/prices.json ({len(prices)}/{len(tickers)} tickers)")

    # Contrôle de couverture : les 5 derniers jours ouvrés doivent être présents
    from datetime import timedelta
    ouvres, d = [], _utcnow().date()
    while len(ouvres) < 5:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            ouvres.append(d.strftime('%Y-%m-%d'))
    print("\nCouverture des 5 derniers jours ouvrés :")
    for ticker, info in prices.items():
        h = info.get('history', {})
        manquants = [j for j in ouvres if j not in h]
        etat = 'complet' if not manquants else 'manque ' + ', '.join(manquants)
        print(f" {ticker:<14} {etat}")

if __name__ == '__main__':
    main()
