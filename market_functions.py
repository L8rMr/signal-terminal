"""Data for the Godel-style terminal functions: DES, FA, MOST and OMON.

All sources are free and delayed or end-of-day, so every result is safe to show
to public visitors:
- Yahoo Finance (unofficial): company profile and key stats (DES) and the
  predefined most-active / gainer / loser screeners (MOST). Profile requests need
  a session cookie plus "crumb", which this module fetches and renews itself.
- Cboe delayed quotes (about 15 minutes): option chains with greeks (OMON).
- SEC EDGAR XBRL company facts: standardized financial statements (FA).
"""
import datetime, http.cookiejar, json, threading, time, urllib.error, urllib.parse, urllib.request

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36'
_lock = threading.Lock()
_cache = {}
_yahoo = {'opener': None, 'crumb': None, 'expires': 0}


def _cached(key, ttl, fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now: return hit[1]
    value = fn()
    _cache[key] = (now + ttl, value)
    if len(_cache) > 400:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:150]: _cache.pop(k, None)
    return value


def _get(opener, url, headers=None, limit=8_000_000):
    h = {'User-Agent': UA, 'Accept': '*/*', 'Accept-Language': 'en-US,en;q=0.9'}
    h.update(headers or {})
    with opener.open(urllib.request.Request(url, headers=h), timeout=15) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit: raise ValueError('Response too large.')
    return raw


# ---------- Yahoo session ----------
def _yahoo_session(force=False):
    with _lock:
        if not force and _yahoo['crumb'] and _yahoo['expires'] > time.time(): return _yahoo['opener'], _yahoo['crumb']
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try: _get(opener, 'https://fc.yahoo.com/')  # sets the A3 cookie even though it answers 404
        except urllib.error.HTTPError: pass
        crumb = _get(opener, 'https://query1.finance.yahoo.com/v1/test/getcrumb', limit=200).decode().strip()
        if not crumb or len(crumb) > 40 or '<' in crumb: raise LookupError('Yahoo session unavailable.')
        _yahoo.update(opener=opener, crumb=crumb, expires=time.time() + 3600)
        return opener, crumb


def _yahoo_json(url, crumb=True):
    for attempt in (0, 1):
        opener, token = _yahoo_session(force=attempt == 1)
        full = url + (('&' if '?' in url else '?') + 'crumb=' + urllib.parse.quote(token) if crumb else '')
        try: return json.loads(_get(opener, full, {'Accept': 'application/json'}))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403) and attempt == 0: continue
            raise


def _raw(v):
    if isinstance(v, dict): return v.get('raw')
    return v


# ---------- DES ----------
def describe(symbol):
    symbol = symbol.upper()
    def fetch():
        modules = 'assetProfile,summaryDetail,defaultKeyStatistics,financialData,price,calendarEvents'
        try: data = _yahoo_json(f'https://query2.finance.yahoo.com/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}?modules={modules}')
        except urllib.error.HTTPError as exc:
            if exc.code == 404: raise LookupError(f'No security found for {symbol}. Check the ticker.') from None
            raise
        result = ((data.get('quoteSummary') or {}).get('result') or [None])[0]
        if not result: raise LookupError(f'No profile found for {symbol}.')
        p, sd, ks, fd, pr = (result.get(k) or {} for k in ('assetProfile', 'summaryDetail', 'defaultKeyStatistics', 'financialData', 'price'))
        cal = (result.get('calendarEvents') or {}).get('earnings') or {}
        earnings = [_raw(x) for x in cal.get('earningsDate') or []]
        officers = [{'name': o.get('name'), 'title': o.get('title')} for o in (p.get('companyOfficers') or [])[:4]]
        return {
            'symbol': symbol, 'name': pr.get('longName') or pr.get('shortName') or symbol,
            'exchange': pr.get('exchangeName'), 'currency': pr.get('currency'), 'quote_type': pr.get('quoteType'),
            'price': _raw(pr.get('regularMarketPrice')), 'change_pct': (_raw(pr.get('regularMarketChangePercent')) or 0) * 100,
            'market_time': _raw(pr.get('regularMarketTime')), 'market_state': pr.get('marketState'),
            'sector': p.get('sector'), 'industry': p.get('industry'), 'employees': p.get('fullTimeEmployees'),
            'website': p.get('website'), 'city': p.get('city'), 'state': p.get('state'), 'country': p.get('country'),
            'summary': p.get('longBusinessSummary'), 'officers': officers,
            'stats': {
                'Market cap': _raw(pr.get('marketCap')) or _raw(sd.get('marketCap')),
                'Enterprise value': _raw(ks.get('enterpriseValue')),
                'P/E (trailing)': _raw(sd.get('trailingPE')), 'P/E (forward)': _raw(sd.get('forwardPE')),
                'EV/EBITDA': _raw(ks.get('enterpriseToEbitda')), 'Price/book': _raw(ks.get('priceToBook')),
                'Dividend yield': _raw(sd.get('dividendYield')), 'Beta': _raw(sd.get('beta')),
                '52-week high': _raw(sd.get('fiftyTwoWeekHigh')), '52-week low': _raw(sd.get('fiftyTwoWeekLow')),
                '50-day avg': _raw(sd.get('fiftyDayAverage')), '200-day avg': _raw(sd.get('twoHundredDayAverage')),
                'Avg volume (3m)': _raw(sd.get('averageVolume')), 'Shares out': _raw(ks.get('sharesOutstanding')),
                'Short % float': _raw(ks.get('shortPercentOfFloat')),
                'Revenue (ttm)': _raw(fd.get('totalRevenue')), 'Revenue growth': _raw(fd.get('revenueGrowth')),
                'Gross margin': _raw(fd.get('grossMargins')), 'Operating margin': _raw(fd.get('operatingMargins')),
                'Profit margin': _raw(fd.get('profitMargins')), 'Free cash flow': _raw(fd.get('freeCashflow')),
                'Total cash': _raw(fd.get('totalCash')), 'Total debt': _raw(fd.get('totalDebt')),
            },
            'analyst': {'target_mean': _raw(fd.get('targetMeanPrice')), 'target_high': _raw(fd.get('targetHighPrice')),
                        'target_low': _raw(fd.get('targetLowPrice')), 'recommendation': fd.get('recommendationKey'),
                        'analysts': _raw(fd.get('numberOfAnalystOpinions'))},
            'next_earnings': next((e for e in earnings if e), None),
            'source': 'Yahoo Finance (delayed)',
        }
    return _cached(('des', symbol), 600, fetch)


# ---------- MOST ----------
SCREENS = {'ACTIVE': 'most_actives', 'GAINERS': 'day_gainers', 'LOSERS': 'day_losers'}


def most(kind='ACTIVE', count=40):
    kind = kind.upper() if kind and kind.upper() in SCREENS else 'ACTIVE'
    def fetch():
        url = f'https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={SCREENS[kind]}&count={count}'
        data = _yahoo_json(url, crumb=False)
        quotes = (((data.get('finance') or {}).get('result') or [{}])[0]).get('quotes') or []
        rows = []
        for q in quotes:
            vol, avg = q.get('regularMarketVolume'), q.get('averageDailyVolume3Month')
            rows.append({'symbol': q.get('symbol'), 'name': q.get('shortName') or q.get('longName') or '',
                         'price': q.get('regularMarketPrice'), 'change_pct': q.get('regularMarketChangePercent'),
                         'volume': vol, 'rvol': round(vol / avg, 2) if vol and avg else None,
                         'market_cap': q.get('marketCap'), 'exchange': q.get('fullExchangeName') or q.get('exchange')})
        return {'kind': kind, 'rows': [r for r in rows if r['symbol']], 'at': int(time.time()), 'source': 'Yahoo Finance screener (delayed)'}
    return _cached(('most', kind, count), 60, fetch)


# ---------- OMON ----------
def _parse_occ(code):
    # AAPL261120C00100000 -> root, expiry, type, strike
    tail = code[-15:]
    return code[:-15], datetime.date(2000 + int(tail[:2]), int(tail[2:4]), int(tail[4:6])).isoformat(), tail[6], int(tail[7:]) / 1000


def option_chain(symbol, expiry=None):
    symbol = symbol.upper()
    def fetch():
        root = ('_' + symbol) if symbol in ('SPX', 'NDX', 'RUT', 'VIX', 'XSP', 'DJX') else symbol
        raw = _get(urllib.request.build_opener(), f'https://cdn.cboe.com/api/global/delayed_quotes/options/{urllib.parse.quote(root)}.json',
                   {'Referer': 'https://www.cboe.com/', 'Origin': 'https://www.cboe.com', 'Accept': 'application/json'}, limit=40_000_000)
        data = json.loads(raw)
        d = data.get('data') or {}
        chain = {}
        for o in d.get('options') or []:
            try: _, exp, kind, strike = _parse_occ(o['option'])
            except (KeyError, ValueError, IndexError): continue
            row = chain.setdefault(exp, {}).setdefault(strike, {'strike': strike})
            row['C' if kind == 'C' else 'P'] = {k: o.get(k) for k in ('bid', 'ask', 'last_trade_price', 'volume', 'open_interest', 'iv', 'delta', 'gamma', 'theta', 'vega')}
        return {'symbol': symbol, 'underlying': d.get('current_price') or d.get('close'), 'change_pct': d.get('percent_change'),
                'timestamp': data.get('timestamp'), 'chain': chain}
    full = _cached(('omon', symbol), 90, fetch)
    expiries = sorted(full['chain'])
    if not expiries: raise LookupError(f'No listed options found for {symbol}.')
    today = datetime.date.today().isoformat()
    pick = expiry if expiry in full['chain'] else next((e for e in expiries if e >= today), expiries[0])
    strikes = sorted(full['chain'][pick].values(), key=lambda r: r['strike'])
    spot = full['underlying']
    if spot and len(strikes) > 40:  # keep the 40 strikes nearest the money
        center = min(range(len(strikes)), key=lambda i: abs(strikes[i]['strike'] - spot))
        lo = max(0, min(center - 20, len(strikes) - 40)); strikes = strikes[lo:lo + 40]
    return {'symbol': symbol, 'underlying': spot, 'change_pct': full['change_pct'], 'expiries': expiries, 'expiry': pick,
            'rows': strikes, 'as_of': full['timestamp'], 'source': 'Cboe delayed quotes (about 15 minutes)'}


# ---------- FA ----------
FA_LINES = [
    # (section, label, candidate tags, kind) kind: flow | instant | eps | derived
    ('Income statement', 'Revenue', ['RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues', 'SalesRevenueNet', 'RevenueFromContractWithCustomerIncludingAssessedTax'], 'flow'),
    ('Income statement', 'Cost of revenue', ['CostOfGoodsAndServicesSold', 'CostOfRevenue', 'CostOfGoodsSold'], 'flow'),
    ('Income statement', 'Gross profit', ['GrossProfit'], 'flow'),
    ('Income statement', 'R&D', ['ResearchAndDevelopmentExpense'], 'flow'),
    ('Income statement', 'Operating income', ['OperatingIncomeLoss'], 'flow'),
    ('Income statement', 'Net income', ['NetIncomeLoss', 'ProfitLoss'], 'flow'),
    ('Income statement', 'EPS (diluted)', ['EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted'], 'eps'),
    ('Balance sheet', 'Cash & equivalents', ['CashAndCashEquivalentsAtCarryingValue', 'CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'], 'instant'),
    ('Balance sheet', 'Total assets', ['Assets'], 'instant'),
    ('Balance sheet', 'Total liabilities', ['Liabilities'], 'instant'),
    ('Balance sheet', 'Long-term debt', ['LongTermDebtNoncurrent', 'LongTermDebt'], 'instant'),
    ("Balance sheet", "Shareholders' equity", ['StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'], 'instant'),
    ('Cash flow', 'Operating cash flow', ['NetCashProvidedByUsedInOperatingActivities'], 'flow'),
    ('Cash flow', 'Capital expenditure', ['PaymentsToAcquirePropertyPlantAndEquipment', 'PaymentsToAcquireProductiveAssets'], 'flow'),
    ('Cash flow', 'Buybacks', ['PaymentsForRepurchaseOfCommonStock'], 'flow'),
    ('Cash flow', 'Dividends paid', ['PaymentsOfDividends', 'PaymentsOfDividendsCommonStock'], 'flow'),
]


def _days(row):
    try: return (datetime.date.fromisoformat(row['end']) - datetime.date.fromisoformat(row['start'])).days
    except (KeyError, ValueError, TypeError): return None


def _series(taxonomy, tags, kind):
    """{'annual': {end: value}, 'quarter': {end: value}} merged across candidate tags (first tag wins per period)."""
    annual, quarter = {}, {}
    for tag in tags:
        concept = taxonomy.get(tag)
        if not concept: continue
        units = concept.get('units') or {}
        rows = units.get('USD/shares' if kind == 'eps' else 'USD') or []
        best, spans = {}, {}
        for r in rows:
            if not isinstance(r.get('val'), (int, float)) or not r.get('end'): continue
            form = str(r.get('form', ''))
            if not form.startswith(('10-K', '10-Q', '20-F', '40-F')): continue
            if kind == 'instant':
                bucket = 'annual' if form.startswith(('10-K', '20-F', '40-F')) and r.get('fp') == 'FY' else 'quarter'
            else:
                d = _days(r)
                if d is None: continue
                bucket = 'annual' if 330 <= d <= 380 else 'quarter' if 80 <= d <= 100 else None
                if not bucket: continue
            key = (bucket, r['end'])
            if key not in best or str(r.get('filed', '')) > str(best[key].get('filed', '')): best[key] = r
        if kind == 'flow':  # every reported span, including 6- and 9-month year-to-date figures
            for r in rows:
                if not isinstance(r.get('val'), (int, float)) or not r.get('start') or not r.get('end'): continue
                if not str(r.get('form', '')).startswith(('10-K', '10-Q', '20-F', '40-F')): continue
                k = (r['start'], r['end'])
                if k not in spans or str(r.get('filed', '')) > str(spans[k].get('filed', '')): spans[k] = r
        for (bucket, end), r in best.items():
            target = annual if bucket == 'annual' else quarter
            target.setdefault(end, r['val'])
        if kind == 'instant':  # quarter-end balances include fiscal year ends too
            for end, v in annual.items(): quarter.setdefault(end, v)
        if kind == 'flow':
            # Cash-flow lines are filed year-to-date (3, 6, 9, 12 months from the same
            # fiscal-year start), so a quarter = this YTD figure minus the previous one.
            by_start = {}
            for (start, end), r in spans.items(): by_start.setdefault(start, []).append((end, r['val']))
            for start, items in by_start.items():
                items.sort()
                for (prev_end, prev_val), (end, val) in zip(items, items[1:]):
                    gap = (datetime.date.fromisoformat(end) - datetime.date.fromisoformat(prev_end)).days
                    if 80 <= gap <= 100: quarter.setdefault(end, val - prev_val)
    if kind == 'flow':  # derive fiscal Q4 = FY minus the three reported quarters
        for end, total in annual.items():
            fy_end = datetime.date.fromisoformat(end)
            qs = [v for e, v in quarter.items() if 0 < (fy_end - datetime.date.fromisoformat(e)).days < 300]
            if end not in quarter and len(qs) == 3: quarter[end] = total - sum(qs)
    return {'annual': annual, 'quarter': quarter}


def financials(companyfacts, periods='annual'):
    taxonomy = (companyfacts.get('facts') or {}).get('us-gaap') or {}
    series = {label: (section, kind, _series(taxonomy, tags, kind)[periods]) for section, label, tags, kind in FA_LINES}
    anchor = series['Revenue'][2] or series['Net income'][2]
    count = 5 if periods == 'annual' else 8
    columns = sorted(anchor, reverse=True)[:count][::-1]
    if not columns: raise LookupError('No standardized financial data found in SEC filings for this company.')
    def val(label, end): return series[label][2].get(end)
    lines = []
    for section, label, _, kind in FA_LINES:
        values = [val(label, c) for c in columns]
        if label == 'Gross profit' and not any(values):
            values = [(val('Revenue', c) - val('Cost of revenue', c)) if val('Revenue', c) is not None and val('Cost of revenue', c) is not None else None for c in columns]
        if any(v is not None for v in values): lines.append({'section': section, 'label': label, 'kind': kind, 'values': values})
    get = {l['label']: l['values'] for l in lines}
    def ratio(a, b): return [(x / y if x is not None and y else None) for x, y in zip(get.get(a, [None] * len(columns)), get.get(b, [None] * len(columns)))]
    derived = []
    if 'Operating cash flow' in get and 'Capital expenditure' in get:
        derived.append({'section': 'Cash flow', 'label': 'Free cash flow', 'kind': 'flow',
                        'values': [(o - c) if o is not None and c is not None else None for o, c in zip(get['Operating cash flow'], get['Capital expenditure'])]})
    for label, num in (('Gross margin', 'Gross profit'), ('Operating margin', 'Operating income'), ('Net margin', 'Net income')):
        if num in get and 'Revenue' in get: derived.append({'section': 'Margins', 'label': label, 'kind': 'pct', 'values': ratio(num, 'Revenue')})
    rev = get.get('Revenue')
    if rev:
        step = 1 if periods == 'annual' else 4
        growth = [None] * len(rev)
        for i in range(step, len(rev)):
            if rev[i] is not None and rev[i - step]: growth[i] = rev[i] / rev[i - step] - 1
        if any(g is not None for g in growth): derived.append({'section': 'Margins', 'label': 'Revenue growth (YoY)', 'kind': 'pct', 'values': growth})
    return {'periods': periods, 'columns': columns, 'lines': lines + derived, 'source': 'SEC EDGAR XBRL company facts'}
