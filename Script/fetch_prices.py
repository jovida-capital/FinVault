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
            print(f"  Retry {i+1}/{retries}: {e}")
            time.sleep(2)
    return None


def assainir_historique(history, ticker, seuil=0.12, reference_incoherente=None):
    """Corrige les points aberrants herites des runs precedents.

    Un cours provisoire injecte par erreur (quote d'une autre cotation, seance
    fantome) reste fige dans prices.json tant que Yahoo ne renvoie pas a nouveau
    cette date.

    Detection : on parcourt la serie dans l'ordre et on compare chaque point a
    la derniere valeur jugee saine. Un ecart superieur au seuil n'est retenu
    comme anomalie que si la serie REVIENT ensuite au niveau anterieur : un
    decrochage durable ou une tendance reguliere sont ainsi preserves, alors
    qu'un pic isole est identifie. Le dernier point, qui n'a pas de suite, est
    juge sur le seul ecart avec la valeur precedente.

    La valeur fautive est REMPLACEE par la derniere cloture saine, et non
    supprimee : supprimer laisserait un trou, et la lecture d'une date
    remonterait silencieusement a la seance precedente.
    """
    dates = sorted(history)
    if len(dates) < 3:
        return history, []

    # Seuil adapte a la volatilite propre de l'instrument. Un ETF varie de
    # moins de 1 % par seance, une crypto peut bouger de 10 % : appliquer le
    # meme seuil fixe aplatirait de vrais mouvements sur les actifs volatils.
    variations = []
    for i in range(1, len(dates)):
        a, b = history[dates[i-1]], history[dates[i]]
        if a > 0:
            variations.append(abs(b - a) / a)
    # Il faut assez de seances pour estimer une volatilite : sur une serie
    # courte, la mediane serait elle-meme tiree par l'anomalie a detecter.
    if len(variations) >= 20:
        variations.sort()
        mediane = variations[len(variations) // 2]
        seuil = max(seuil, min(6 * mediane, 0.35))   # plafonne pour rester utile

    corriges = []
    ref = None                       # derniere valeur consideree comme saine

    for pos, d in enumerate(dates):
        v = history[d]
        if not v > 0:
            continue
        if ref is None:
            ref = v
            continue

        if abs(v - ref) / ref <= seuil:
            ref = v                  # evolution plausible : devient la reference
            continue

        # Ecart important : anomalie isolee ou vrai mouvement de marche ?
        suivants = [history[x] for x in dates[pos+1:pos+4] if history.get(x, 0) > 0]
        if suivants:
            # La serie revient-elle vers l'ancien niveau, ou suit-elle le nouveau ?
            proche_ancien = sum(1 for x in suivants if abs(x - ref) / ref <= seuil)
            proche_nouveau = sum(1 for x in suivants if abs(x - v) / v <= seuil)
            anomalie = proche_ancien > proche_nouveau
            # Cas ambigu : plusieurs points consecutifs au meme niveau decale
            # peuvent etre un vrai decrochage OU une serie de cours provisoires
            # issus d'une autre cotation. Si ce niveau correspond au quote juge
            # incoherent pour ce ticker, on tranche pour l'anomalie.
            if not anomalie and reference_incoherente:
                if abs(v - reference_incoherente) / max(reference_incoherente, 1e-9) <= 0.02:
                    anomalie = True
        else:
            anomalie = True          # dernier point : pas de confirmation possible

        if anomalie:
            history[d] = ref
            corriges.append((d, v, ref))
        else:
            ref = v                  # mouvement reel : on suit le nouveau niveau

    if corriges:
        detail = ', '.join(f"{d} : {v} -> {r}" for d, v, r in corriges)
        print(f"\n    CORRECTION {ticker} : {len(corriges)} point(s) aberrant(s) — {detail}", end='')
    return history, corriges

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
        meta   = result['meta']
        price  = meta.get('regularMarketPrice') or meta.get('chartPreviousClose')
        if not price or price <= 0:
            return None

        gmtoffset = meta.get('gmtoffset', 0)

        # Devise. Le drapeau "cotation en pence" doit etre capture AVANT la
        # normalisation en GBP, sans quoi le test devient toujours faux et
        # l'historique reste en pence alors que le cours passe en livres.
        currency = (meta.get('currency') or 'EUR').upper()
        en_pence = (currency == 'GBX')      # a capturer AVANT la normalisation
        if currency == 'GBX':
            price = price / 100
            currency = 'GBP'
        # Les ETF europeens (.PA, .AS, .DE, .MI) sont libelles en EUR meme
        # lorsque Yahoo annonce USD dans ses metadonnees.
        if currency == 'USD' and any(ticker.upper().endswith(s)
                                     for s in ('.PA', '.AS', '.DE', '.MI')):
            currency = 'EUR'

        # ── Historique quotidien sur 2 ans : source unique et correctement datee
        history = _lire_series(data, gmtoffset, en_pence)
        source_hist = 'quotidien'

        if len(history) < 10:
            # Repli hebdomadaire, recalé sur le vendredi (dernier jour ouvre).
            print(f"\n    REPLI {ticker} : serie quotidienne trop courte "
                  f"({len(history)} pts) — bascule sur l'hebdomadaire", end='')
            for hote in ('query1', 'query2'):
                dw = fetch_json(f"https://{hote}.finance.yahoo.com/v8/finance/chart/"
                                f"{ticker}?interval=1wk&range=2y")
                if dw:
                    hebdo = _lire_series(dw, gmtoffset, en_pence,
                                         decalage_fin_semaine=4)
                    hebdo.update(history)      # le quotidien reste prioritaire
                    history = hebdo
                    source_hist = 'hebdomadaire recale'
                    break

        if not history:
            print(f"\n    ATTENTION {ticker} : aucune cloture historique recuperee", end='')

        # ── Prix retenu : la DERNIERE CLOTURE de la serie, jamais le quote ──
        #
        # regularMarketPrice donne le cours "courant". Or le script s'execute
        # avant l'ouverture des marches europeens : a cette heure, pour un
        # instrument cote sur plusieurs places, ce champ peut porter la valeur
        # d'une autre cotation. Constate sur WSRI.PA : quote 96.507 contre une
        # cloture de 115.175, soit exactement le rapport EUR/USD — deux
        # valorisations du meme fonds dans deux devises.
        #
        # La serie de clotures, elle, est le fixing officiel de la place : une
        # seule source, non ambigue. Elle fait donc foi. Le quote n'est conserve
        # qu'a titre indicatif, et n'entre plus jamais dans l'historique.
        quote_brut = price
        ecart_quote = None
        if history:
            derniere_cloture = history[max(history)]
            if derniere_cloture > 0:
                ecart_quote = (price - derniere_cloture) / derniere_cloture
                price = derniere_cloture          # la cloture fait foi
                if abs(ecart_quote) > 0.05:
                    print(f"\n    INFO {ticker} : quote {round(quote_brut, 4)} ecarte "
                          f"({ecart_quote * 100:+.1f} %) au profit de la cloture "
                          f"{derniere_cloture}", end='')
        else:
            print(f"\n    ATTENTION {ticker} : aucune cloture — repli sur le quote "
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
        print(f"  Parse error: {e}")
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
    for i, (ticker, nom) in enumerate(tickers.items()):
        print(f"  [{i+1}/{len(tickers)}] {ticker} ({nom[:40]})...", end=' ', flush=True)
        result = fetch_yahoo(ticker)
        if result:
            # Self-building history: merge with previous history if Yahoo gave none/little
            prev_hist = prev_prices.get(ticker, {}).get('history', {})
            if prev_hist:
                merged = dict(prev_hist)
                merged.update(result['history'])  # new data wins on overlapping dates
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
                        print(f"\n    PURGE {ticker} : {len(parasites)} date(s) de "
                              f"week-end heritees supprimees", end='')
                result['history'] = merged
            # Le cours instantané ne sert que de valeur d'attente :
            # si Yahoo publie déjà une clôture pour aujourd'hui, elle fait foi.
            # L'historique ne contient plus que des clotures officielles.
            # Aucun cours instantane n'y est injecte : c'etait la source des
            # seances fantomes et des valeurs provisoires corrigees le lendemain.
            # Nettoyage des valeurs parasites heritees des executions anterieures.
            result['history'], _ = assainir_historique(
                result['history'], ticker,
                reference_incoherente=result.get('quote_brut'))
            prices[ticker] = result
            print(f"✓ {result['price']} {result['currency']} ({len(result['history'])} pts)")
        else:
            # Yahoo failed entirely — keep previous data if we have it (stale but not lost).
            # L'historique conserve passe malgre tout par l'assainissement :
            # sans cela une valeur polluee y resterait figee indefiniment.
            if ticker in prev_prices:
                conserve = dict(prev_prices[ticker])
                if conserve.get('history'):
                    conserve['history'], _ = assainir_historique(
                        dict(conserve['history']), ticker,
                        reference_incoherente=conserve.get('quote_brut'))
                prices[ticker] = conserve
                print(f"✗ Yahoo failed, kept previous ({conserve['price']})")
            else:
                print("✗ no data")
        time.sleep(1.5)  # be polite

    # Write prices.json
    import os
    os.makedirs('data', exist_ok=True)
    with open('data/prices.json', 'w') as f:
        json.dump({
            'updated': _utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
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
        print(f"  {ticker:<14} {etat}")

if __name__ == '__main__':
    main()
