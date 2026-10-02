"""Daily Market Brief engine.

Builds a detailed, evidence-based brief from connected headlines, cross-asset
prices, FRED observations and Treasury rates. Everything here is rules-based and
transparent; the optional analyst note (ai_note) is only used when an Anthropic
API key is configured.
"""
import datetime, json, os, re, time

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo('America/New_York')
except Exception:  # pragma: no cover - tzdata missing
    NY = datetime.timezone(datetime.timedelta(hours=-4))

# --------------------------------------------------------------------------- text helpers

STOPWORDS = set('a an the and or of to in on for at by with from as is are was were be been it its this that these those after before over under says say said amid ahead into up down new more than vs via will could may might has have had not no us u.s. inc corp'.split())
NOISE = ('my wife', 'my husband', 'my friend', 'my father', 'my mother', 'i invested', 'i have ', "i'm ", 'social security', 'retire early', 'tour ', 'celebrity',
         'recipe', 'football', 'soccer', 'concert', 'movie', 'appoints', 'names new', 'share consolidation', 'private placement', 'dear ', 'should i ')
MARKET_CONTEXT = ('stock', 'market', 'shares', 'index', 'yield', 'bond', 'treasur', 'dollar', 'currency', 'oil', 'crude', 'gold', 'tariff', 'econom', 'inflation',
                  'fed', 'central bank', 'rate', 'investor', 'trade', 'sanction', 'export', 'import', 'supply', 'price', 'gdp', 'growth', 'recession', 'energy',
                  'chip', 'semiconductor', 'defense', 'bank', 'deficit', 'debt', 'budget', 'commodit', 'shipping', 'earnings')

def text_of(article):
    return ' ' + (article.get('title', '') + ' ' + article.get('summary', '')).lower() + ' '

def _pattern(term):
    term = term.lower()
    if len(term) <= 4 or term.isalpha() and len(term) <= 6:
        return re.compile(r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])')
    return re.compile(re.escape(term))

_pattern_cache = {}
def has_any(text, terms):
    for term in terms:
        compiled = _pattern_cache.get(term)
        if compiled is None: compiled = _pattern_cache[term] = _pattern(term)
        if compiled.search(text): return True
    return False

def count_terms(text, terms):
    return sum(1 for term in terms if has_any(text, (term,)))

def is_noise(article):
    return has_any(text_of(article), NOISE)

def tokens(title):
    words = re.findall(r"[a-z0-9$%.']+", title.lower())
    return {w.strip(".'") for w in words if w.strip(".'") and w.strip(".'") not in STOPWORDS and len(w) > 2}

def cluster(articles, threshold=0.45):
    """Group near-duplicate headlines across publishers; each cluster keeps its best story."""
    clusters = []
    for article in sorted(articles, key=lambda a: (a.get('relevance', 0), a.get('published', 0)), reverse=True):
        words = tokens(article.get('title', ''))
        for group in clusters:
            union = words | group['words']
            if union and len(words & group['words']) / len(union) >= threshold:
                group['items'].append(article); group['sources'].add(article.get('source')); break
        else:
            clusters.append({'words': words, 'items': [article], 'sources': {article.get('source')}})
    return clusters

def story(group_or_article):
    if isinstance(group_or_article, dict) and 'items' in group_or_article:
        items, sources = group_or_article['items'], group_or_article['sources']
    else:
        items, sources = [group_or_article], {group_or_article.get('source')}
    lead = items[0]
    summary = next((a.get('summary') for a in items if len(a.get('summary') or '') > 40), '') or ''
    if len(summary) > 280: summary = summary[:277].rsplit(' ', 1)[0] + '…'
    return {'id': lead.get('id'), 'title': lead.get('title'), 'source': lead.get('source'), 'published': lead.get('published'),
            'relevance': lead.get('relevance'), 'summary': summary, 'sources': len(sources), 'url': lead.get('url')}

# --------------------------------------------------------------------------- number formatting

def pct(value, digits=1):
    if value is None: return 'n/a'
    return f"{'+' if value > 0 else '−' if value < 0 else ''}{abs(value):.{digits}f}%"

def bp(value):
    if value is None: return 'n/a'
    return f"{'+' if value > 0 else '−' if value < 0 else ''}{abs(value):.0f} bp"

def num(value, digits=2):
    if value is None: return 'n/a'
    return f"{value:,.{digits}f}"

def direction(value, small=0.15):
    if value is None: return 'flat'
    return 'up' if value > small else 'down' if value < -small else 'flat'

# --------------------------------------------------------------------------- market session

def session_state(now_ny, market):
    """US cash-session phase, and whether the latest daily bar is actually today's."""
    minutes = now_ny.hour * 60 + now_ny.minute
    weekday = now_ny.weekday() < 5
    if not weekday: phase = 'weekend'
    elif minutes < 4 * 60: phase = 'overnight'
    elif minutes < 9 * 60 + 30: phase = 'pre-market'
    elif minutes < 16 * 60: phase = 'open'
    elif minutes < 20 * 60: phase = 'after-hours'
    else: phase = 'closed'
    spx = market.get('SPX')
    bar_today = bool(spx and datetime.datetime.fromtimestamp(spx.get('bar_time') or 0, NY).date() == now_ny.date())
    live = phase == 'open' and bar_today
    label = {'weekend': 'WEEKEND', 'overnight': 'OVERNIGHT', 'pre-market': 'PRE-MARKET', 'open': 'US MARKET OPEN',
             'after-hours': 'AFTER HOURS', 'closed': 'US MARKET CLOSED'}[phase]
    return {'phase': phase, 'label': label, 'bar_today': bar_today, 'live': live,
            'move_word': 'today' if bar_today else 'in the prior session',
            'move_label': 'TODAY' if bar_today else 'PRIOR',
            'note': ('Cash prices are from the prior session; futures show the overnight tone.' if not bar_today else
                     'Prices update through the session.' if live else 'Prices are final for the session.')}

# --------------------------------------------------------------------------- market tape

TAPE = ['SPX', 'NDX', 'RUT', 'DJI', 'VIX', 'US10Y', 'DXY', 'WTI', 'GOLD', 'BTC']
SECTORS = {'XLK': 'Technology', 'SMH': 'Semiconductors', 'XLF': 'Financials', 'XLE': 'Energy', 'XLV': 'Health Care', 'XLI': 'Industrials',
           'XLY': 'Consumer Discretionary', 'XLP': 'Consumer Staples', 'XLU': 'Utilities', 'XLC': 'Communication', 'XLB': 'Materials',
           'XLRE': 'Real Estate', 'ITA': 'Aerospace & Defense'}
CYCLICALS, DEFENSIVES = ('XLY', 'XLF', 'XLI', 'XLK', 'XLB'), ('XLP', 'XLU', 'XLV')
YIELD_CODES = ('US10Y', 'US5Y', 'US3M', 'US30Y')

def yield_change_bp(item, base_key='previous'):
    if not item or item.get(base_key) is None: return None
    return (item['value'] - item[base_key]) * 100

def tape_tiles(market, session=None):
    tiles = []
    codes = list(TAPE)
    if session and not session['bar_today'] and market.get('ES'): codes = ['ES', 'NQ'] + [c for c in codes if c not in ('ES', 'NQ')]
    for code in codes:
        m = market.get(code)
        if not m: continue
        if code in YIELD_CODES:
            move = yield_change_bp(m)
            tile = {'code': code, 'name': m['name'], 'value': f"{m['value']:.2f}%", 'one_day': bp(move), 'five_day': bp(yield_change_bp(m, 'five_ago')),
                    'dir': direction(move, 0.5)}
        else:
            digits = 0 if m['value'] >= 1000 else 2
            tile = {'code': code, 'name': m['name'], 'value': num(m['value'], digits), 'one_day': pct(m.get('one_day'), 2), 'five_day': pct(m.get('five_day'), 1),
                    'dir': direction(m.get('one_day'), 0.05)}
        # VIX rising is risk-off: flip the colour so green always means "good for risk assets"
        if code == 'VIX' and tile['dir'] != 'flat': tile['dir'] = 'down' if tile['dir'] == 'up' else 'up'
        tile['spark'] = m.get('spark') or []
        tile['group'] = m.get('group')
        tiles.append(tile)
    return tiles

def relative(market, code, base='SPX', field='five_day'):
    a, b = market.get(code), market.get(base)
    if not a or not b or a.get(field) is None or b.get(field) is None: return None
    return a[field] - b[field]

def trend_state(m):
    if not m or not m.get('sma20') or not m.get('sma50'): return None
    above20, above50 = m['value'] > m['sma20'], m['value'] > m['sma50']
    if above20 and above50: return 'uptrend'
    if not above20 and not above50: return 'downtrend'
    return 'pullback' if above50 else 'repair'

def equity_clause(market):
    parts = [f"{market[c]['name']} {pct(market[c]['one_day'], 2)}" for c in ('SPX', 'NDX', 'RUT') if market.get(c) and market[c].get('one_day') is not None]
    return ', '.join(parts)

def breadth(market):
    """Participation: equal weight vs cap weight, small vs large, sectors above their 20-day."""
    rows = []
    equal = relative(market, 'RSP', 'SPY')
    if equal is not None:
        rows.append({'label': 'Equal weight vs cap weight (5d)', 'value': pct(equal),
                     'read': 'the average stock is outpacing the index — broad participation.' if equal > 0.5 else 'a handful of large caps are carrying the index.' if equal < -0.5 else 'in line; no clear concentration signal.',
                     'tone': 'positive' if equal > 0.5 else 'negative' if equal < -0.5 else 'neutral'})
    small = relative(market, 'RUT')
    if small is not None:
        rows.append({'label': 'Small caps vs S&P 500 (5d)', 'value': pct(small),
                     'read': 'risk appetite is spreading down the cap scale.' if small > 0.5 else 'small caps are lagging, typical of defensive or rate-pressured tape.' if small < -0.5 else 'tracking the index.',
                     'tone': 'positive' if small > 0.5 else 'negative' if small < -0.5 else 'neutral'})
    above = [code for code in SECTORS if market.get(code) and market[code].get('sma20') and market[code]['value'] > market[code]['sma20']]
    have = [code for code in SECTORS if market.get(code) and market[code].get('sma20')]
    if have:
        share = len(above) / len(have) * 100
        rows.append({'label': 'Sectors above their 20-day', 'value': f"{len(above)}/{len(have)}",
                     'read': 'most groups are trending up.' if share >= 60 else 'most groups are below trend — the damage is broad.' if share <= 35 else 'a split tape; leadership is rotating.',
                     'tone': 'positive' if share >= 60 else 'negative' if share <= 35 else 'neutral'})
    return rows

def market_conditions(market, rates, indicators, session=None):
    word = session['move_word'] if session else 'today'
    lines = []
    spx, ndx, rut, dji = (market.get(c) for c in ('SPX', 'NDX', 'RUT', 'DJI'))
    if spx:
        clause = equity_clause(market)
        small_vs_large = relative(market, 'RUT')
        text = f"Equities ({word}): {clause}{', Dow ' + pct(dji['one_day'], 2) if dji else ''}. Over 5 days the S&P 500 is {pct(spx['five_day'])} and {pct(spx['one_month'])} over a month."
        if small_vs_large is not None:
            text += f" Small caps are {'outperforming' if small_vs_large > 0.5 else 'lagging' if small_vs_large < -0.5 else 'tracking'} large caps by {abs(small_vs_large):.1f} pts over 5 days" + (' — broad participation.' if small_vs_large > 0.5 else ' — leadership is narrow.' if small_vs_large < -0.5 else '.')
        lines.append({'label': 'EQUITIES', 'text': text, 'tone': 'positive' if spx['five_day'] and spx['five_day'] > 0 else 'negative'})
        state = trend_state(spx)
        if state:
            from_high = (spx['value'] / spx['high_1m'] - 1) * 100 if spx.get('high_1m') else None
            lines.append({'label': 'TREND', 'text': f"S&P 500 is in a {state}: last {num(spx['value'])} vs 20-day {num(spx['sma20'])} and 50-day {num(spx['sma50'])}; {abs(from_high):.1f}% below its 1-month high of {num(spx['high_1m'])}, 1-month low {num(spx['low_1m'])}." if from_high is not None else f"S&P 500 is in a {state}.",
                          'tone': 'positive' if state == 'uptrend' else 'negative' if state == 'downtrend' else 'neutral'})
    if session and not session['bar_today'] and market.get('ES'):
        futures = '; '.join(f"{market[c]['name']} {pct(market[c]['one_day'], 2)}" for c in ('ES', 'NQ') if market.get(c))
        lines.insert(0, {'label': 'OVERNIGHT', 'text': f"{session['label'].title()}: {futures}. Futures set the tone for the open; the cash levels below are the prior session's close.",
                         'tone': 'neutral'})
    ten = market.get('US10Y'); rate_map = {r['series']: r for r in rates}
    if ten or rate_map.get('DGS10'):
        tv = ten['value'] if ten else rate_map['DGS10']['value']
        d1 = yield_change_bp(ten) if ten else None; d5 = yield_change_bp(ten, 'five_ago') if ten else None
        text = f"Rates: 10-year Treasury {tv:.2f}% ({bp(d1)} {word}, {bp(d5)} over 5 days)"
        if ten and ten.get('high_3m') and tv >= ten['high_3m'] - 0.02: text += ', at the top of its 3-month range'
        text += '.'
        curve = indicators.get('T10Y2Y')
        if curve: text += f" The 2s10s curve is {bp(curve['value'] * 100)}{' (inverted)' if curve['value'] < 0 else ''}."
        ff, two = rate_map.get('DFF'), rate_map.get('DGS2')
        if ff and two:
            gap = (two['value'] - ff['value']) * 100
            text += f" Fed funds effective {ff['value']:.2f}%; the 2-year at {two['value']:.2f}% sits {bp(abs(gap))[1:] if gap else '0 bp'} {'above' if gap > 0 else 'below'} it, " + (
                'so the bond market is pricing further tightening.' if gap > 25 else 'so the bond market is pricing rate cuts.' if gap < -25 else 'consistent with policy on hold.')
        lines.append({'label': 'RATES', 'text': text, 'tone': 'negative' if (d5 or 0) > 10 else 'positive' if (d5 or 0) < -10 else 'neutral'})
    fx = [f"Dollar index {num(market['DXY']['value'])} ({pct(market['DXY']['one_day'], 2)}, 5d {pct(market['DXY']['five_day'])})"] if market.get('DXY') else []
    if market.get('USDJPY'): fx.append(f"USD/JPY {num(market['USDJPY']['value'])} ({pct(market['USDJPY']['one_day'], 2)})")
    if market.get('EURUSD'): fx.append(f"EUR/USD {num(market['EURUSD']['value'], 4)} ({pct(market['EURUSD']['one_day'], 2)})")
    if fx:
        dxy5 = market.get('DXY', {}).get('five_day') or 0
        lines.append({'label': 'DOLLAR', 'text': '; '.join(fx) + ('. A firming dollar tightens global financial conditions and weighs on multinationals and commodities.' if dxy5 > 1 else '. A softer dollar is supportive for commodities, EM and US multinationals.' if dxy5 < -1 else '.'),
                      'tone': 'negative' if dxy5 > 1 else 'positive' if dxy5 < -1 else 'neutral'})
    com = [f"{market[c]['name']} {'$' + num(market[c]['value'])} ({pct(market[c]['one_day'], 2)}, 5d {pct(market[c]['five_day'])})" for c in ('WTI', 'BRENT', 'GOLD', 'COPPER', 'NATGAS') if market.get(c)]
    if com:
        text = 'Commodities: ' + '; '.join(com) + '.'
        wti5, gold5, cop5 = (market.get(c, {}).get('five_day') for c in ('WTI', 'GOLD', 'COPPER'))
        if wti5 is not None and wti5 > 5: text += ' The oil spike is an inflation and consumer-spending headwind.'
        if cop5 is not None and gold5 is not None:
            text += ' Copper outpacing gold signals improving growth expectations.' if cop5 - gold5 > 2 else ' Gold outpacing copper signals defensive, growth-scare positioning.' if gold5 - cop5 > 2 else ''
        lines.append({'label': 'COMMODITIES', 'text': text, 'tone': 'negative' if (wti5 or 0) > 5 else 'neutral'})
    vix, vix3m = market.get('VIX'), market.get('VIX3M'); hy = indicators.get('BAMLH0A0HYM2')
    if vix:
        text = f"Volatility & credit: VIX {vix['value']:.2f} ({pct(vix['one_day'], 1)})"
        stressed = vix['value'] >= 20
        if vix3m:
            ratio = vix['value'] / vix3m['value']
            text += f", VIX/VIX3M {ratio:.2f} — " + ('term structure inverted, hedging demand is acute' if ratio >= 1 else 'term structure flat, some near-term event hedging' if ratio > 0.92 else 'normal contango, no acute stress')
            stressed = stressed or ratio >= 1
        if hy:
            hist = hy.get('history') or []
            month = hist[-22]['value'] if len(hist) >= 22 else None
            text += f". High-yield spread {hy['value']:.2f}%" + (f" ({bp((hy['value'] - month) * 100)} over a month)" if month is not None else '')
        if market.get('HYG'): text += f", HYG 5d {pct(market['HYG']['five_day'])}"
        lines.append({'label': 'VOL & CREDIT', 'text': text + '.', 'tone': 'negative' if stressed else 'positive' if vix['value'] < 15 else 'neutral'})
    rows = breadth(market)
    if rows:
        lines.append({'label': 'BREADTH', 'text': 'Participation: ' + '; '.join(f"{r['label']} {r['value']} — {r['read']}" for r in rows),
                      'tone': 'negative' if sum(1 for r in rows if r['tone'] == 'negative') > len(rows) / 2 else 'positive' if sum(1 for r in rows if r['tone'] == 'positive') > len(rows) / 2 else 'neutral'})
    sectors = sector_table(market)
    if sectors:
        leaders = ', '.join(f"{s['name']} {pct(s['one_day'], 1)}" for s in sorted(sectors, key=lambda s: s['one_day'] or 0, reverse=True)[:3])
        laggards = ', '.join(f"{s['name']} {pct(s['one_day'], 1)}" for s in sorted(sectors, key=lambda s: s['one_day'] or 0)[:3])
        cyc = [market[c]['five_day'] for c in CYCLICALS if market.get(c) and market[c].get('five_day') is not None]
        dfn = [market[c]['five_day'] for c in DEFENSIVES if market.get(c) and market[c].get('five_day') is not None]
        text = f"Rotation ({word}): leaders {leaders}; laggards {laggards}."
        if cyc and dfn:
            spread = sum(cyc) / len(cyc) - sum(dfn) / len(dfn)
            text += f" Cyclicals vs defensives over 5 days: {pct(spread)} — " + ('risk appetite is expanding.' if spread > 1 else 'investors are hiding in defensives.' if spread < -1 else 'no clear rotation.')
        lines.append({'label': 'ROTATION', 'text': text, 'tone': 'neutral'})
    glob = [f"{market[c]['name']} {pct(market[c]['one_day'], 2)}" for c in ('NKY', 'HSI', 'DAX', 'UKX') if market.get(c)]
    if market.get('BTC'): glob.append(f"Bitcoin {pct(market['BTC']['one_day'], 1)} (5d {pct(market['BTC']['five_day'])})")
    if glob: lines.append({'label': 'GLOBAL', 'text': 'Overseas & crypto: ' + '; '.join(glob) + '.', 'tone': 'neutral'})
    return lines

def sector_table(market):
    rows = []
    for code, name in SECTORS.items():
        m = market.get(code)
        if not m: continue
        rows.append({'code': code, 'name': name, 'one_day': m.get('one_day'), 'five_day': m.get('five_day'), 'one_month': m.get('one_month'),
                     'rs_5d': relative(market, code), 'trend': trend_state(m)})
    return sorted(rows, key=lambda r: r['rs_5d'] if r['rs_5d'] is not None else -99, reverse=True)

def _pressure(value, low, high):
    """For a neutral reading inside (low, high): 0 at the band's middle, 1 at either edge, plus which edge is nearer."""
    mid, half = (low + high) / 2, (high - low) / 2
    return min(1.0, abs(value - mid) / half), ('high' if value >= mid else 'low')

def regime(market, indicators):
    components = []
    def add(label, score, detail, near=None):
        # near = (0..1 closeness to the next threshold, what crossing it would do) — only for neutral inputs
        components.append({'label': label, 'score': score, 'detail': detail, 'near': near if score == 0 else None})
    spx = market.get('SPX')
    state = trend_state(spx)
    if state:
        near = None
        if state in ('pullback', 'repair'):
            line, name = (spx['sma20'], '20-day') if state == 'pullback' else (spx['sma50'], '50-day')
            gap = abs(spx['value'] / line - 1) * 100
            near = (max(0.0, 1 - gap / 2), f"S&P 500 is {gap:.1f}% from its {name} ({num(line, 0)}); {'reclaiming it' if state == 'pullback' else 'a close above it'} turns Trend +2")
        add('Trend', {'uptrend': 2, 'pullback': 0, 'repair': 0, 'downtrend': -2}[state], f"S&P 500 {state}", near)
    if spx and spx.get('five_day') is not None:
        v = spx['five_day']; p, side = _pressure(v, -1, 1)
        add('Momentum', 1 if v > 1 else -1 if v < -1 else 0, f"S&P 500 5d {pct(v)}",
            (p, f"S&P 500 5d {pct(v)}; {'above +1% adds' if side == 'high' else 'below −1% subtracts'} a point"))
    vix, vix3m = market.get('VIX'), market.get('VIX3M')
    if vix:
        score = 1 if vix['value'] < 15 else 0 if vix['value'] < 20 else -1 if vix['value'] < 28 else -2
        if vix3m and vix['value'] >= vix3m['value']: score = min(score, -2)
        p, side = _pressure(vix['value'], 15, 20)
        add('Volatility', score, f"VIX {vix['value']:.1f}",
            (p, f"VIX {vix['value']:.1f}; {'under 15 adds' if side == 'low' else '20 or higher subtracts'} a point"))
    hy = indicators.get('BAMLH0A0HYM2')
    if hy and len(hy.get('history') or []) >= 22:
        widen = (hy['value'] - hy['history'][-22]['value']) * 100
        p, side = _pressure(widen, -15, 25)
        add('Credit', -2 if hy['value'] >= 5 or widen > 60 else -1 if widen > 25 else 1 if widen < -15 else 0, f"HY spread {hy['value']:.2f}% ({bp(widen)} 1m)",
            (p, f"HY spread {bp(widen)} over a month; {'widening past 25 bp subtracts' if side == 'high' else 'tightening past 15 bp adds'} a point"))
    small = relative(market, 'RUT')
    if small is not None:
        p, side = _pressure(small, -1, 1)
        add('Breadth', 1 if small > 1 else -1 if small < -1 else 0, f"Small vs large 5d {pct(small)}",
            (p, f"small caps {pct(small)} vs S&P 500 (5d); {'leading by 1 pt adds' if side == 'high' else 'lagging by 1 pt subtracts'} a point"))
    equal = relative(market, 'RSP', 'SPY')
    if equal is not None:
        p, side = _pressure(equal, -0.5, 0.5)
        add('Participation', 1 if equal > 0.5 else -1 if equal < -0.5 else 0, f"Equal vs cap weight 5d {pct(equal)}",
            (p, f"equal weight {pct(equal)} vs cap weight (5d); {'leading by 0.5 pt adds' if side == 'high' else 'lagging by 0.5 pt subtracts'} a point"))
    cyc = [market[c]['five_day'] for c in CYCLICALS if market.get(c) and market[c].get('five_day') is not None]
    dfn = [market[c]['five_day'] for c in DEFENSIVES if market.get(c) and market[c].get('five_day') is not None]
    if cyc and dfn:
        spread = sum(cyc) / len(cyc) - sum(dfn) / len(dfn)
        p, side = _pressure(spread, -1, 1)
        add('Rotation', 1 if spread > 1 else -1 if spread < -1 else 0, f"Cyclicals vs defensives {pct(spread)}",
            (p, f"cyclicals {pct(spread)} vs defensives (5d); {'leading by 1 pt adds' if side == 'high' else 'lagging by 1 pt subtracts'} a point"))
    ten = market.get('US10Y')
    if ten and ten.get('five_ago') is not None:
        move = yield_change_bp(ten, 'five_ago')
        add('Rates shock', -1 if abs(move) > 20 else 0, f"10Y {bp(move)} 5d",
            (min(1.0, abs(move) / 20), f"10-year {bp(move)} in 5 days; a 20 bp move either way subtracts a point"))
    dxy = market.get('DXY')
    if dxy and dxy.get('five_day') is not None:
        v = dxy['five_day']
        add('Dollar', -1 if v > 1.5 else 0, f"DXY 5d {pct(v)}",
            (max(0.0, min(1.0, v / 1.5)), f"dollar index {pct(v)} in 5 days; above +1.5% subtracts a point"))
    total = sum(c['score'] for c in components)
    label = 'RISK-ON' if total >= 4 else 'CONSTRUCTIVE' if total >= 1 else 'MIXED' if total >= -1 else 'DEFENSIVE' if total >= -4 else 'RISK-OFF'
    return {'label': label, 'score': total, 'components': components}

# --------------------------------------------------------------------------- economic data

def _hist(indicator, back):
    hist = indicator.get('history') or []
    return hist[-1 - back]['value'] if len(hist) > back else None

def economy_rows(indicators, rates):
    rows = []
    def row(series, value, prior, read, tone, trend):
        ind = indicators[series]
        rows.append({'series': series, 'name': ind['name'], 'group': ind['group'], 'value': value, 'prior': prior, 'date': ind['date'], 'read': read, 'tone': tone, 'trend': trend, 'url': ind.get('url')})
    cpi = indicators.get('CPIAUCSL')
    if cpi:
        three = _hist(cpi, 3); trend = direction(cpi['value'] - cpi['previous'], 0.05)
        read = f"{'Accelerating' if trend == 'up' else 'Cooling' if trend == 'down' else 'Steady'} vs {cpi['previous']:.1f}% prior" + (f" ({'up' if cpi['value'] > three else 'down'} from {three:.1f}% three months ago)" if three is not None else '') + f"; {cpi['value'] - 2:.1f} pts above the Fed's 2% target."
        row('CPIAUCSL', f"{cpi['value']:.1f}%", f"{cpi['previous']:.1f}%", read, 'negative' if trend == 'up' or cpi['value'] > 3 else 'neutral', trend)
    pce = indicators.get('PCEPILFE')
    if pce:
        trend = direction(pce['value'] - pce['previous'], 0.05); ff = next((r for r in rates if r['series'] == 'DFF'), None)
        read = f"The Fed's preferred gauge is {'rising' if trend == 'up' else 'easing' if trend == 'down' else 'flat'}."
        if ff: read += f" Real policy rate ≈ {ff['value'] - pce['value']:+.1f} pts ({'restrictive' if ff['value'] - pce['value'] > 1 else 'only mildly restrictive' if ff['value'] - pce['value'] > 0 else 'accommodative'})."
        row('PCEPILFE', f"{pce['value']:.1f}%", f"{pce['previous']:.1f}%", read, 'negative' if pce['value'] > 3 else 'neutral', trend)
    un = indicators.get('UNRATE')
    if un:
        hist = [h['value'] for h in (un.get('history') or [])]
        sahm = (sum(hist[-3:]) / 3 - min(hist[-15:-3])) if len(hist) >= 15 else None
        read = f"{'Rising' if un['value'] > un['previous'] else 'Falling' if un['value'] < un['previous'] else 'Unchanged'} from {un['previous']:.1f}%."
        if sahm is not None: read += f" Sahm-rule gap {sahm:+.2f} pts" + (' — recession signal triggered.' if sahm >= 0.5 else ' — labor market softening.' if sahm >= 0.3 else ' — no recession signal.')
        row('UNRATE', f"{un['value']:.1f}%", f"{un['previous']:.1f}%", read, 'negative' if sahm is not None and sahm >= 0.3 else 'neutral', direction(un['value'] - un['previous'], 0.05))
    pay = indicators.get('PAYEMS')
    if pay:
        hist = [h['value'] for h in (pay.get('history') or [])][-3:]
        avg = sum(hist) / len(hist) if hist else None
        read = f"{pay['value']:+.0f}K jobs vs {pay['previous']:+.0f}K prior" + (f"; 3-month average {avg:+.0f}K" if avg is not None else '') + ('. Hiring is re-accelerating.' if pay['value'] > pay['previous'] + 50 else '. Hiring momentum is fading.' if pay['value'] < pay['previous'] - 50 else '.')
        row('PAYEMS', f"{pay['value']:+.0f}K", f"{pay['previous']:+.0f}K", read, 'positive' if (avg or 0) > 100 else 'negative' if (avg or 0) < 50 else 'neutral', direction(pay['value'] - pay['previous'], 20))
    icsa = indicators.get('ICSA')
    if icsa:
        hist = [h['value'] for h in (icsa.get('history') or [])]
        four = sum(hist[-4:]) / 4 if len(hist) >= 4 else None; prior_q = sum(hist[-17:-13]) / 4 if len(hist) >= 17 else None
        read = (f"4-week average {four:.0f}K" if four else '') + (f" vs {prior_q:.0f}K a quarter ago" if prior_q else '') + ('; layoffs are low.' if icsa['value'] < 230 else '; claims are elevated.' if icsa['value'] > 260 else '.')
        row('ICSA', f"{icsa['value']:.0f}K", f"{icsa['previous']:.0f}K", read, 'positive' if icsa['value'] < 230 else 'negative' if icsa['value'] > 260 else 'neutral', direction(icsa['value'] - icsa['previous'], 3))
    gdp = indicators.get('GDPC1')
    if gdp:
        read = f"Real growth {'slowed' if gdp['value'] < gdp['previous'] else 'improved'} from {gdp['previous']:.1f}% annualized" + ('; below-trend expansion.' if gdp['value'] < 1.8 else '; at or above trend.')
        row('GDPC1', f"{gdp['value']:.1f}%", f"{gdp['previous']:.1f}%", read, 'negative' if gdp['value'] < 1 else 'neutral' if gdp['value'] < 1.8 else 'positive', direction(gdp['value'] - gdp['previous'], 0.2))
    for series, label in (('RSAFS', 'Consumer spending'), ('INDPRO', 'Factory output')):
        ind = indicators.get(series)
        if ind:
            trend = direction(ind['value'] - ind['previous'], 0.2)
            read = f"{label} growth {'accelerating' if trend == 'up' else 'slowing' if trend == 'down' else 'steady'} vs {ind['previous']:.1f}% prior (nominal YoY)."
            row(series, f"{ind['value']:.1f}%", f"{ind['previous']:.1f}%", read, 'negative' if ind['value'] < 0 or trend == 'down' else 'positive' if trend == 'up' else 'neutral', trend)
    sent = indicators.get('UMCSENT')
    if sent:
        hist = [h['value'] for h in (sent.get('history') or [])][-12:]
        avg = sum(hist) / len(hist) if hist else None
        read = f"{'Up' if sent['value'] > sent['previous'] else 'Down'} from {sent['previous']:.1f}" + (f"; 12-month average {avg:.1f}" if avg else '') + ('. Households remain gloomy.' if sent['value'] < 60 else '.')
        row('UMCSENT', f"{sent['value']:.1f}", f"{sent['previous']:.1f}", read, 'negative' if sent['value'] < 60 else 'neutral', direction(sent['value'] - sent['previous'], 1))
    curve = indicators.get('T10Y2Y')
    if curve:
        three = _hist(curve, 63)
        read = f"{'Inverted' if curve['value'] < 0 else 'Positively sloped'}" + (f"; {'steepened' if curve['value'] > three else 'flattened'} {abs(curve['value'] - three) * 100:.0f} bp over ~3 months" if three is not None else '') + '.'
        row('T10Y2Y', f"{curve['value'] * 100:+.0f} bp", f"{curve['previous'] * 100:+.0f} bp", read, 'negative' if curve['value'] < 0 else 'neutral', direction(curve['value'] - curve['previous'], 0.02))
    hy = indicators.get('BAMLH0A0HYM2')
    if hy:
        month = _hist(hy, 21)
        read = ('Tight — credit markets are relaxed.' if hy['value'] < 3.5 else 'Elevated — lenders demand more compensation.' if hy['value'] < 5 else 'Stressed.') + (f" {bp((hy['value'] - month) * 100)} over a month." if month is not None else '')
        row('BAMLH0A0HYM2', f"{hy['value']:.2f}%", f"{hy['previous']:.2f}%", read, 'positive' if hy['value'] < 3.5 else 'negative' if hy['value'] >= 5 else 'neutral', direction(hy['value'] - hy['previous'], 0.03))
    oil = indicators.get('DCOILWTICO')
    if oil:
        month = _hist(oil, 21)
        read = (f"{pct((oil['value'] / month - 1) * 100)} over a month. " if month else '') + ('Energy costs are an inflation risk.' if oil['value'] >= 85 else 'Energy prices are not a major inflation driver.')
        row('DCOILWTICO', f"${oil['value']:.2f}", f"${oil['previous']:.2f}", read, 'negative' if oil['value'] >= 85 else 'neutral', direction(oil['value'] - oil['previous'], 0.5))
    return rows

def macro_verdict(rows):
    tones = {r['series']: r for r in rows}
    inflation_hot = any(tones.get(s, {}).get('tone') == 'negative' for s in ('CPIAUCSL', 'PCEPILFE'))
    growth_soft = sum(1 for s in ('GDPC1', 'RSAFS', 'INDPRO', 'UNRATE', 'UMCSENT') if tones.get(s, {}).get('tone') == 'negative')
    labor_ok = tones.get('ICSA', {}).get('tone') == 'positive'
    if inflation_hot and growth_soft >= 2: return 'Stagflation-leaning mix: inflation is above target while growth indicators are softening — the hardest backdrop for both stocks and bonds.'
    if inflation_hot: return 'Inflation is the binding constraint: price data stays above target while the labor market holds up, keeping pressure on the Fed to stay tight.' if labor_ok else 'Inflation remains above target, limiting the Fed’s room to support growth.'
    if growth_soft >= 3: return 'Growth is cooling with inflation contained — a backdrop where bonds typically outperform and defensives lead.'
    return 'Macro data is broadly balanced: growth is positive and inflation is not forcing the Fed’s hand.'

# --------------------------------------------------------------------------- events, policy, geopolitics

WEEKDAYS = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
CATALYSTS = [
    ('FOMC rate decision', ('fomc', 'fed decision', 'rate decision', 'fed meeting', 'fed rate decision', 'federal reserve decision', 'fed policy decision'), '2:00 PM ET statement, 2:30 PM press conference', 'high', 14.5),
    ('CPI inflation', ('cpi', 'consumer price index', 'consumer prices'), '8:30 AM ET', 'high', 8.5),
    ('Jobs report', ('nonfarm payroll', 'jobs report', 'payrolls report', 'employment report'), '8:30 AM ET', 'high', 8.5),
    ('PCE inflation', ('pce', 'personal consumption expenditures'), '8:30 AM ET', 'high', 8.5),
    ('PPI', ('ppi', 'producer price'), '8:30 AM ET', 'medium', 8.5),
    ('Retail sales', ('retail sales',), '8:30 AM ET', 'medium', 8.5),
    ('GDP', ('gdp report', 'gdp estimate', 'gross domestic product'), '8:30 AM ET', 'medium', 8.5),
    ('ISM / PMI', ('ism manufacturing', 'ism services', 'pmi'), '9:45–10:00 AM ET', 'medium', 10.0),
    ('Oil inventories', ('oil inventories', 'crude inventories', 'eia inventory', 'crude stockpiles', 'api says'), 'EIA 10:30 AM ET Wednesday', 'low', 10.5),
    ('Treasury auction', ('treasury auction', 'bond auction', 'note auction', '10-year auction', '30-year auction', '20-year auction'), '1:00 PM ET', 'medium', 13.0),
    ('Housing data', ('housing starts', 'home sales', 'building permits'), '8:30–10:00 AM ET', 'low', 10.0),
    ('Central banks abroad', ('ecb decision', 'bank of japan', 'boj decision', 'bank of england', 'boe decision', 'pboc'), 'varies', 'medium', None),
    ('Big-tech earnings', ('nvidia earnings', 'apple earnings', 'microsoft earnings', 'alphabet earnings', 'amazon earnings', 'meta earnings', 'tesla earnings', 'oracle earnings', 'broadcom earnings'), 'after the close', 'high', 16.0),
]

# Confirmed dates from the Federal Reserve and BLS calendars. The two recurring
# weekly releases below are generated separately so the panel never depends on
# headlines alone. Keep only market-moving national releases here.
OFFICIAL_EVENTS = [
    (datetime.date(2026,9,29),10.0,'JOLTS','10:00 AM ET','medium','BLS'),
    (datetime.date(2026,10,2),8.5,'Jobs report','8:30 AM ET','high','BLS'),
    (datetime.date(2026,10,14),8.5,'CPI inflation','8:30 AM ET','high','BLS'),
    (datetime.date(2026,10,15),8.5,'PPI','8:30 AM ET','medium','BLS'),
    (datetime.date(2026,10,28),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2026,10,30),8.5,'Employment Cost Index','8:30 AM ET','medium','BLS'),
    (datetime.date(2026,11,3),10.0,'JOLTS','10:00 AM ET','medium','BLS'),
    (datetime.date(2026,11,6),8.5,'Jobs report','8:30 AM ET','high','BLS'),
    (datetime.date(2026,11,10),8.5,'CPI inflation','8:30 AM ET','high','BLS'),
    (datetime.date(2026,11,13),8.5,'PPI','8:30 AM ET','medium','BLS'),
    (datetime.date(2026,12,9),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,1,27),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,3,17),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,4,28),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,6,9),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,7,28),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,9,15),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,10,27),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
    (datetime.date(2027,12,8),14.0,'FOMC rate decision','2:00 PM ET statement, 2:30 PM press conference','high','Federal Reserve'),
]
EIA_EXCEPTIONS = {
    datetime.date(2026,10,14):(datetime.date(2026,10,15),12.0),
    datetime.date(2026,11,11):(datetime.date(2026,11,12),12.0),
}

def calendar_label(date_value, now_ny):
    offset=(date_value-now_ny.date()).days
    return 'Today' if offset==0 else 'Tomorrow' if offset==1 else date_value.strftime('%a %b %-d')

def next_weekday(date_value, weekday):
    return date_value+datetime.timedelta(days=(weekday-date_value.weekday())%7)

def official_calendar(now_ny, horizon=60):
    """Upcoming official releases used when news has no reliable future event."""
    output=[];end=now_ny.date()+datetime.timedelta(days=horizon)
    def add(date_value,hour,name,timing,importance,source):
        event_at=datetime.datetime.combine(date_value,datetime.time(int(hour),int(round(hour%1*60))),NY)
        if event_at<=now_ny or date_value>end: return
        output.append({'name':name,'when':calendar_label(date_value,now_ny),'date':date_value.isoformat(),
                       'offset':(date_value-now_ny.date()).days,'timing':timing,'importance':importance,
                       'mentions':0,'headline':None,'article_id':None,'calendar':True,'source':source})
    claims=next_weekday(now_ny.date(),3)
    if datetime.datetime.combine(claims,datetime.time(8,30),NY)<=now_ny: claims+=datetime.timedelta(days=7)
    add(claims,8.5,'Jobless claims','8:30 AM ET (weekly)','low','US Labor Department')
    eia_date=next_weekday(now_ny.date(),2);release_date,release_hour=EIA_EXCEPTIONS.get(eia_date,(eia_date,10.5))
    if datetime.datetime.combine(release_date,datetime.time(int(release_hour),int(round(release_hour%1*60))),NY)<=now_ny:
        eia_date+=datetime.timedelta(days=7);release_date,release_hour=EIA_EXCEPTIONS.get(eia_date,(eia_date,10.5))
    add(release_date,release_hour,'Oil inventories',('12:00 PM ET' if release_hour==12 else '10:30 AM ET')+' (weekly)','low','EIA')
    for row in OFFICIAL_EVENTS: add(*row)
    rank={'high':0,'medium':1,'low':2}
    return sorted(output,key=lambda event:(event['offset'],rank[event['importance']]))

def catalyst_context(article, terms):
    """Return only text near an event term so an unrelated weekday cannot date it."""
    text = text_of(article).lower();windows=[]
    for term in terms:
        start=0
        while True:
            index=text.find(term,start)
            if index<0: break
            windows.append(text[max(0,index-100):min(len(text),index+len(term)+130)])
            start=index+len(term)
    return ' '.join(windows)

def completed_event(text):
    """Identify coverage reacting to a release instead of previewing it."""
    patterns=(r'\b(?:reacts?|reaction) to\b',r'\b(?:after|following|in the wake of) (?:the )?(?:fed|fomc|decision|release|report)',
              r'\b(?:left|leaving|kept) (?:interest )?rates? (?:unchanged|steady)',r'\bdecision and what comes next\b',
              r'\brate hike reassured\b',r'\b(?:data|report|decision) (?:showed|came in|was released)\b')
    return any(re.search(pattern,text) for pattern in patterns)

def when_mentioned(text, now_ny):
    today = WEEKDAYS[now_ny.weekday()]; tomorrow = WEEKDAYS[(now_ny.weekday() + 1) % 7]
    if has_any(text, ('yesterday',)): return 'Past', -1
    if has_any(text, ('today',)): return 'Today', 0
    if has_any(text, ('tomorrow',)): return 'Tomorrow', 1
    if has_any(text, ('next '+today,)): return today.title(), 7
    if has_any(text, (today,)): return 'Today', 0
    if has_any(text, ('next '+tomorrow,)): return tomorrow.title(), 8
    if has_any(text, (tomorrow,)): return 'Tomorrow', 1
    for offset in range(2, 7):
        day = WEEKDAYS[(now_ny.weekday() + offset) % 7]
        if has_any(text, ('next '+day,)): return day.title(), offset+7
        if has_any(text, (day,)): return day.title(), offset
    if has_any(text, ('this week', 'week ahead', 'next week')): return 'This week', 3
    return None, 9

def catalysts(recent, now_ny):
    events = []
    current_hour=now_ny.hour+now_ny.minute/60
    for name, terms, timing, weight, cutoff_hour in CATALYSTS:
        hits = [a for a in recent if has_any(text_of(a), terms)]
        if not hits: continue
        votes = {}
        for a in sorted(hits, key=lambda a: a.get('published') or 0, reverse=True):
            published = datetime.datetime.fromtimestamp(a.get('published') or time.time(), NY)
            context=catalyst_context(a,terms)
            if completed_event(context): continue
            when, offset = when_mentioned(context, published)
            if not when: continue
            offset = 3 if when == 'This week' else offset - (now_ny.date() - published.date()).days
            if offset < 0: continue
            key = 'This week' if when == 'This week' else offset
            votes.setdefault(key, []).append(a)
        best_when, best_offset, source = None, 9, hits[0]
        dated = {k: v for k, v in votes.items() if k != 'This week'}
        if dated:
            best_offset = max(dated, key=lambda k: (len(dated[k]), -k)); source = dated[best_offset][0]
            best_when = 'Today' if best_offset == 0 else 'Tomorrow' if best_offset == 1 else WEEKDAYS[(now_ny.weekday() + best_offset) % 7].title()
        elif votes:
            best_when, best_offset, source = 'This week', 3, votes['This week'][0]
        if name == 'Oil inventories' and best_when is None and now_ny.weekday() <= 2: best_when, best_offset = ('Today' if now_ny.weekday() == 2 else 'Wednesday'), 2 - now_ny.weekday()
        if best_when is None: continue
        if best_offset==0 and cutoff_hour is not None and current_hour>=cutoff_hour: continue
        events.append({'name': name, 'when': best_when, 'offset': best_offset, 'timing': timing, 'importance': weight, 'mentions': len(hits),
                       'headline': source.get('title'), 'article_id': source.get('id')})
    if now_ny.weekday() <= 3 and not any(e['name'] == 'Jobless claims' for e in events):
        offset = 3 - now_ny.weekday()
        if offset>0 or current_hour<8.5:
            events.append({'name': 'Jobless claims', 'when': 'Today' if offset == 0 else 'Tomorrow' if offset == 1 else 'Thursday', 'offset': offset, 'timing': '8:30 AM ET (weekly)', 'importance': 'low', 'mentions': 0, 'headline': None, 'article_id': None})
    # Fill gaps from official schedules. A news-detected event with a nearer valid
    # date wins; otherwise the calendar entry supplies the next known occurrence.
    for scheduled in official_calendar(now_ny):
        if not any(event['name']==scheduled['name'] for event in events): events.append(scheduled)
    rank = {'high': 0, 'medium': 1, 'low': 2}
    return sorted(events, key=lambda e: (e['offset'], rank[e['importance']], -e['mentions']))[:8]

def fed_expectations(recent, rates):
    fed = [a for a in recent if has_any(text_of(a), ('fed', 'fomc', 'federal reserve', 'powell', 'warsh', 'rate hike', 'rate cut'))]
    hikes = sum(1 for a in fed if has_any(text_of(a), ('hike', 'raise rates', 'tighten', 'lift the benchmark', 'increase rates')))
    cuts = sum(1 for a in fed if has_any(text_of(a), ('rate cut', 'cut rates', 'ease', 'lower rates', 'easing')))
    odds = []
    for a in fed:
        for match in re.finditer(r'(\d{1,3}(?:\.\d)?)\s?%\s+(?:chance|probability|odds|likelihood)', text_of(a)):
            value = float(match.group(1))
            if 0 < value <= 100: odds.append((value, a))
    bps = []
    for a in fed:
        for match in re.finditer(r'(\d{1,3})\s?(?:basis points|bps|bp)\s+of\s+(tightening|easing|hikes|cuts)', text_of(a)):
            bps.append((int(match.group(1)), match.group(2), a))
    lines = []
    if fed:
        bias = 'hike' if hikes > cuts * 1.5 else 'cut' if cuts > hikes * 1.5 else 'hold/mixed'
        lines.append(f"{len(fed)} Fed-related stories in 24h; hike language in {hikes}, easing language in {cuts} → headline bias: {bias.upper()}.")
    if odds:
        value, a = max(odds, key=lambda x: x[1].get('published') or 0)
        lines.append(f"Market pricing cited: {value:.0f}% probability ({a.get('source')}: “{a.get('title')}”).")
    if bps:
        value, kind, a = bps[0]
        lines.append(f"About {value} bp of {kind} priced for the next decision ({a.get('source')}).")
    rate_map = {r['series']: r for r in rates}
    if rate_map.get('DGS2') and rate_map.get('DFF'):
        gap = (rate_map['DGS2']['value'] - rate_map['DFF']['value']) * 100
        lines.append(f"2-year yield minus fed funds = {gap:+.0f} bp: " + ('Treasuries price additional hikes over the next two years.' if gap > 25 else 'Treasuries price cuts ahead.' if gap < -25 else 'Treasuries price a steady policy path.'))
    return {'lines': lines, 'stories': [story(g) for g in cluster(fed)[:3]], 'bias': ('hike' if hikes > cuts * 1.5 else 'cut' if cuts > hikes * 1.5 else 'mixed') if fed else None}

REGIONS = [
    ('Middle East & Iran', ('iran', 'israel', 'gaza', 'hezbollah', 'houthi', 'red sea', 'hormuz', 'tehran', 'yemen', 'lebanon', 'syria', 'saudi'),
     'Oil supply and shipping risk; safe-haven demand; defense contractors.', (('WTI', 1.5), ('BRENT', 1.5), ('GOLD', 1.0), ('ITA', 1.2))),
    ('Russia–Ukraine', ('russia', 'ukraine', 'kremlin', 'putin', 'zelensky', 'moscow', 'kyiv', 'nato'),
     'European natural gas and grain prices; European equities; defense spending.', (('NATGAS', 3.0), ('DAX', 1.2), ('ITA', 1.2))),
    ('US–China & Taiwan', ('china', 'beijing', 'xi jinping', ' xi ', 'taiwan', 'export control', 'chip ban', 'rare earth', 'hong kong'),
     'Semiconductor supply chains, export controls and China-exposed revenue; Hong Kong stocks; industrial metals.', (('SMH', 1.5), ('HSI', 1.5), ('COPPER', 1.5))),
    ('Trade & tariffs', ('tariff', 'trade war', 'trade deal', 'trade talks', 'import duties', 'customs', 'usmca', 'trade tensions'),
     'Input costs and margins for importers, retailers and autos; dollar and inflation expectations.', (('DXY', 0.5), ('XLI', 1.2), ('XLY', 1.2))),
    ('US fiscal & politics', ('shutdown', 'debt ceiling', 'deficit', 'congress', 'white house', 'senate', 'supreme court', 'executive order', 'fiscal', 'spending bill', 'election'),
     'Treasury supply and term premium; regulatory risk for targeted sectors.', (('US10Y', 5), ('DXY', 0.5), ('GOLD', 1.0))),
    ('Japan & yen', ('bank of japan', 'boj', 'yen', 'japan', 'tokyo'),
     'Yen carry-trade unwinds can hit global risk assets; JGB yields feed into Treasury yields.', (('USDJPY', 0.7), ('NKY', 1.5))),
    ('Europe & UK', ('ecb', 'eurozone', 'european union', 'france', 'germany', 'bank of england', 'britain', 'lagarde', 'brussels'),
     'Euro and sterling moves; European banks and exporters; sovereign spreads.', (('EURUSD', 0.5), ('DAX', 1.2), ('UKX', 1.0))),
    ('Emerging markets', ('venezuela', 'mexico', 'brazil', 'argentina', 'india', 'turkey', 'south africa', 'emerging market'),
     'EM currencies and debt; commodity exporters; dollar funding stress.', (('DXY', 0.5), ('COPPER', 1.5))),
]

def geopolitics(recent, market):
    now = time.time(); output = []
    for name, terms, channel, proxies in REGIONS:
        hits = [a for a in recent if has_any(text_of(a), terms) and has_any(text_of(a), MARKET_CONTEXT) and not is_noise(a)]
        if len(hits) < 2: continue
        fresh = sum(1 for a in hits if (a.get('published') or 0) >= now - 6 * 3600)
        momentum = 'escalating' if fresh >= max(3, len(hits) * 0.45) else 'fading' if fresh <= len(hits) * 0.1 else 'steady'
        checks, priced = [], False
        for code, threshold in proxies:
            m = market.get(code)
            if not m: continue
            if code in YIELD_CODES:
                move = yield_change_bp(m); checks.append(f"{m['name']} {bp(move)}")
            else:
                move = m.get('one_day'); checks.append(f"{m['name']} {pct(move, 2)}")
            if move is not None and abs(move) >= threshold and proxies.index((code, threshold)) < 2: priced = True
        severity = min(5, 1 + len(hits) // 5 + (1 if momentum == 'escalating' else 0) + (1 if priced else 0))
        verdict = ('Markets are reacting: proxy assets moved beyond normal daily ranges.' if priced else 'Headline risk only so far: proxy assets are within normal daily ranges.') if checks else ''
        output.append({'region': name, 'stories': len(hits), 'fresh': fresh, 'momentum': momentum, 'severity': severity, 'channel': channel,
                       'market_check': '; '.join(checks), 'priced': priced, 'verdict': verdict, 'top': [story(g) for g in cluster(hits)[:3]]})
    return sorted(output, key=lambda x: (x['severity'], x['stories']), reverse=True)[:6]

THEMES = {
    'Fed & rates': ('fed', 'fomc', 'rate hike', 'rate cut', 'treasury yield', 'bond yield', 'yields', 'treasuries'),
    'Inflation': ('inflation', 'cpi', 'pce', 'prices rose', 'price pressures'),
    'AI & semiconductors': ('artificial intelligence', ' ai ', 'chip', 'semiconductor', 'nvidia', 'data center', 'openai', 'anthropic'),
    'Energy & oil': ('oil', 'crude', 'opec', 'natural gas', 'gasoline', 'brent'),
    'Earnings & guidance': ('earnings', 'guidance', 'beats', 'misses', 'quarterly results', 'revenue of', 'eps'),
    'Credit & private markets': ('credit', 'spread', 'private credit', 'bond sale', 'notes offering', 'default', 'leveraged'),
    'Deals & IPOs': ('merger', 'acquisition', 'acquire', 'takeover', 'ipo', 'buyout', 'bids'),
    'Consumer': ('consumer', 'retail', 'spending', 'shoppers', 'sentiment'),
    'Crypto': ('bitcoin', 'crypto', 'ethereum', 'stablecoin'),
    'Regulation & antitrust': ('antitrust', 'regulator', 'regulation', 'sec ', 'ftc', 'lawsuit', 'probe'),
}

def themes(recent):
    now = time.time(); output = []
    for name, terms in THEMES.items():
        hits = [a for a in recent if has_any(text_of(a), terms) and not is_noise(a)]
        if len(hits) < 3: continue
        fresh = sum(1 for a in hits if (a.get('published') or 0) >= now - 6 * 3600)
        groups = cluster(sorted(hits, key=lambda a: has_any(' ' + a.get('title', '').lower() + ' ', terms), reverse=True))
        groups.sort(key=lambda g: has_any(' ' + g['items'][0].get('title', '').lower() + ' ', terms), reverse=True)
        output.append({'name': name, 'count': len(hits), 'fresh': fresh, 'momentum': 'accelerating' if fresh > len(hits) * 0.4 else 'cooling' if fresh < len(hits) * 0.12 else 'steady',
                       'sources': len({a.get('source') for a in hits}), 'stories': [story(g) for g in groups[:3]]})
    return sorted(output, key=lambda t: t['count'] + t['fresh'], reverse=True)[:6]

# --------------------------------------------------------------------------- trading playbook

def key_levels(market):
    rows = []
    for code in ('SPX', 'NDX', 'RUT'):
        m = market.get(code)
        if not m: continue
        rows.append({'code': code, 'name': m['name'], 'last': m['value'], 'prev_close': m.get('previous'), 'sma20': m.get('sma20'), 'sma50': m.get('sma50'),
                     'high_1m': m.get('high_1m'), 'low_1m': m.get('low_1m'), 'trend': trend_state(m), 'spark': m.get('spark') or []})
    return rows

def playbook(market, reg, events, sectors, indicators, watch, fed):
    plays = []
    def add(tag, stance, text): plays.append({'tag': tag, 'stance': stance, 'text': text})
    vix, vix3m, spx = market.get('VIX'), market.get('VIX3M'), market.get('SPX')
    if vix:
        inverted = vix3m and vix['value'] >= vix3m['value']
        if vix['value'] >= 28 or inverted: add('SIZING', 'caution', f"VIX {vix['value']:.1f}{' with inverted term structure' if inverted else ''}: trade half size or less, widen stops to at least 1× ATR, prefer defined-risk options structures and take partial profits quickly.")
        elif vix['value'] >= 20: add('SIZING', 'caution', f"VIX {vix['value']:.1f}: reduce position size ~25–50% and expect wider intraday ranges; avoid chasing opening gaps.")
        elif vix['value'] < 15: add('SIZING', 'bullish', f"VIX {vix['value']:.1f}: calm tape supports normal sizing and holding trend trades; cheap volatility makes protective puts inexpensive.")
        else: add('SIZING', 'neutral', f"VIX {vix['value']:.1f}: normal sizing; volatility is not a constraint.")
    state = trend_state(spx)
    if state == 'uptrend': add('TREND', 'bullish', f"S&P 500 above rising 20/50-day averages: favor buying pullbacks toward the 20-day ({num(spx['sma20'], 0)}); don't fight strength with index shorts.")
    elif state == 'downtrend': add('TREND', 'bearish', f"S&P 500 below its 20- and 50-day averages: rallies into the 20-day ({num(spx['sma20'], 0)}) are sell/hedge zones; keep longs to names showing relative strength.")
    elif state == 'pullback': add('TREND', 'neutral', f"S&P 500 is under its 20-day ({num(spx['sma20'], 0)}) but above the 50-day ({num(spx['sma50'], 0)}): a pullback inside an uptrend — the 50-day is the line bulls need to hold.")
    elif state == 'repair': add('TREND', 'neutral', f"S&P 500 reclaimed its 20-day ({num(spx['sma20'], 0)}) but is still under the 50-day ({num(spx['sma50'], 0)}): wait for a close above the 50-day before adding aggressive long exposure.")
    near = [e for e in events if e['offset'] <= 1 and e['importance'] == 'high']
    for event in near[:2]:
        if event['name'] == 'FOMC rate decision':
            text = f"FOMC decision {event['when'].lower()} ({event['timing']}): avoid opening large new positions into the statement; the first move after 2:00 PM often reverses during the press conference. Implied volatility typically collapses after — long options into the event are expensive."
            if fed.get('bias') == 'hike': text += ' Headlines expect a hike, so the guidance/dot plot matters more than the decision itself.'
            add('EVENT RISK', 'caution', text)
        else:
            add('EVENT RISK', 'caution', f"{event['name']} {event['when'].lower()} ({event['timing']}): reduce exposure in rate-sensitive names beforehand and let the first 15–30 minutes after the release settle before acting.")
    if sectors:
        leaders = [s for s in sectors if s['rs_5d'] is not None][:3]; laggards = [s for s in sectors if s['rs_5d'] is not None][-3:]
        add('LEADERSHIP', 'bullish', 'Relative strength vs S&P 500 (5d): ' + ', '.join(f"{s['name']} ({s['code']}) {pct(s['rs_5d'])}" for s in leaders) + ' — look for long setups in these groups on intraday pullbacks.')
        add('WEAKNESS', 'bearish', 'Relative weakness (5d): ' + ', '.join(f"{s['name']} ({s['code']}) {pct(s['rs_5d'])}" for s in reversed(laggards)) + ' — avoid bottom-fishing; better short candidates on failed bounces.')
    ten = market.get('US10Y')
    if ten and ten.get('five_ago') is not None:
        move = yield_change_bp(ten, 'five_ago')
        if move >= 10: add('RATES', 'bearish', f"10-year yield {bp(move)} in 5 days to {ten['value']:.2f}%: headwind for long-duration growth, homebuilders, utilities, REITs and small caps; banks and value typically hold up better.")
        elif move <= -10: add('RATES', 'bullish', f"10-year yield {bp(move)} in 5 days: tailwind for growth/tech, homebuilders, REITs and small caps.")
    wti = market.get('WTI')
    if wti and wti.get('five_day') is not None and abs(wti['five_day']) >= 5:
        add('ENERGY', 'bullish' if wti['five_day'] > 0 else 'neutral', f"WTI {pct(wti['five_day'])} over 5 days: " + ('energy producers have earnings tailwind; airlines, cruise lines, transports and consumer discretionary face margin pressure.' if wti['five_day'] > 0 else 'relief for airlines, transports and the consumer; energy producers under pressure.'))
    dxy = market.get('DXY')
    if dxy and dxy.get('five_day') is not None and abs(dxy['five_day']) >= 1:
        add('DOLLAR', 'caution' if dxy['five_day'] > 0 else 'bullish', f"Dollar index {pct(dxy['five_day'])} over 5 days: " + ('headwind for multinationals, gold, commodities and EM assets.' if dxy['five_day'] > 0 else 'supports multinationals, gold, commodities and EM.'))
    hy = indicators.get('BAMLH0A0HYM2')
    if hy and len(hy.get('history') or []) >= 22 and (hy['value'] - hy['history'][-22]['value']) * 100 >= 25:
        add('CREDIT', 'caution', f"High-yield spreads widened {bp((hy['value'] - hy['history'][-22]['value']) * 100)} in a month: equity rallies without credit confirmation tend to fade.")
    for w in watch:
        if w.get('rs_5d') is None: continue
        stance = 'bullish' if w['rs_5d'] > 1 and w.get('trend') == 'uptrend' else 'bearish' if w['rs_5d'] < -1 and w.get('trend') == 'downtrend' else 'neutral'
        add('WATCHLIST', stance, f"{w['symbol']}: {pct(w['one_day'], 2)} today, {pct(w['rs_5d'])} vs S&P 500 over 5 days, {w.get('trend') or 'no trend read'}" +
            (' — relative strength leader; buy-the-dip candidate.' if stance == 'bullish' else ' — relative weakness; avoid longs until it reclaims its 20-day.' if stance == 'bearish' else '.'))
    return plays

# --------------------------------------------------------------------------- change since yesterday, alert signals

def since_yesterday(snapshot, history):
    prior = next((row for row in reversed(history) if row.get('date') != snapshot['date']), None)
    if not prior: return {'lines': [], 'prior_date': None, 'regime_shift': None}
    lines = []
    shift = snapshot['regime'] - prior['regime']
    lines.append(f"Regime score {prior['regime']:+d} → {snapshot['regime']:+d} ({'improving' if shift > 0 else 'deteriorating' if shift < 0 else 'unchanged'}) since {prior['date']}.")
    if prior.get('posture') and prior['posture'] != snapshot['posture']: lines.append(f"Posture changed from {prior['posture']} to {snapshot['posture']}.")
    for key, label, unit in (('spx', 'S&P 500', 'pct'), ('vix', 'VIX', 'level'), ('ten', '10-year yield', 'bp'), ('hy', 'High-yield spread', 'bp')):
        now_value, old_value = snapshot.get(key), prior.get(key)
        if now_value is None or old_value is None: continue
        if unit == 'pct' and old_value:
            move = (now_value / old_value - 1) * 100
            if abs(move) >= 0.25: lines.append(f"{label} {pct(move)} since the last brief.")
        elif unit == 'bp':
            move = (now_value - old_value) * 100
            if abs(move) >= 3: lines.append(f"{label} {bp(move)} since the last brief.")
        elif abs(now_value - old_value) >= 0.75:
            lines.append(f"{label} {old_value:.1f} → {now_value:.1f}.")
    return {'lines': lines[:5], 'prior_date': prior['date'], 'regime_shift': shift}

SIGNALS = [
    ('regime_risk_off', 'Regime turns defensive or risk-off'),
    ('regime_improves', 'Regime turns constructive or risk-on'),
    ('vix_20', 'VIX closes above 20'),
    ('vix_term_inverted', 'VIX term structure inverts'),
    ('catalyst_today', 'A high-impact catalyst lands today'),
    ('geo_priced', 'A geopolitical risk starts moving markets'),
    ('credit_widening', 'High-yield spreads widen sharply'),
    ('yield_shock', '10-year yield moves 20 bp in 5 days'),
    ('trend_break', 'S&P 500 loses or reclaims its 50-day average'),
]
SIGNAL_IDS = {sid for sid, _ in SIGNALS}

def alert_signals(brief, market, indicators, reg, events, geo):
    out = []
    def add(sid, text, detail, fingerprint):
        out.append({'id': sid, 'text': text, 'detail': detail, 'fingerprint': fingerprint})
    day = datetime.datetime.fromtimestamp(brief['generated_at'], NY).strftime('%Y-%m-%d')
    if reg['label'] in ('DEFENSIVE', 'RISK-OFF'): add('regime_risk_off', f"Market regime is {reg['label']} (score {reg['score']:+d}).", brief['headline'], day + reg['label'])
    if reg['label'] in ('CONSTRUCTIVE', 'RISK-ON'): add('regime_improves', f"Market regime is {reg['label']} (score {reg['score']:+d}).", brief['headline'], day + reg['label'])
    vix, vix3m = market.get('VIX'), market.get('VIX3M')
    if vix and vix['value'] >= 20: add('vix_20', f"VIX at {vix['value']:.1f}, above 20.", 'Reduce position size and widen stops.', day)
    if vix and vix3m and vix['value'] >= vix3m['value']: add('vix_term_inverted', 'VIX term structure is inverted.', 'Near-term hedging demand exceeds three-month demand.', day)
    today_event = next((e for e in events if e['offset'] == 0 and e['importance'] == 'high'), None)
    if today_event: add('catalyst_today', f"{today_event['name']} today — {today_event['timing']}.", today_event.get('headline') or '', day + today_event['name'])
    for g in geo:
        if g['priced']: add('geo_priced', f"{g['region']} is moving markets: {g['market_check']}.", g['channel'], day + g['region']); break
    hy = indicators.get('BAMLH0A0HYM2')
    if hy and len(hy.get('history') or []) >= 22 and (hy['value'] - hy['history'][-22]['value']) * 100 >= 30:
        add('credit_widening', f"High-yield spreads widened {bp((hy['value'] - hy['history'][-22]['value']) * 100)} in a month to {hy['value']:.2f}%.", 'Credit is not confirming equity strength.', day)
    ten = market.get('US10Y')
    if ten and ten.get('five_ago') is not None and abs(yield_change_bp(ten, 'five_ago')) >= 20:
        add('yield_shock', f"10-year yield {bp(yield_change_bp(ten, 'five_ago'))} over 5 days to {ten['value']:.2f}%.", 'Duration-sensitive equities are most exposed.', day)
    spx = market.get('SPX')
    if spx and spx.get('sma50'):
        side = 'above' if spx['value'] > spx['sma50'] else 'below'
        add('trend_break', f"S&P 500 is {side} its 50-day average ({num(spx['sma50'], 0)}).", f"Last {num(spx['value'], 0)}.", day + side)
    return out

# --------------------------------------------------------------------------- headline + assembly

def headline(market, reg, events, geo, fed):
    parts = []
    spx, ndx = market.get('SPX'), market.get('NDX')
    if spx and ndx: parts.append(f"S&P 500 {pct(spx['one_day'], 2)}, Nasdaq 100 {pct(ndx['one_day'], 2)}")
    ten = market.get('US10Y')
    if ten: parts.append(f"10-year yield {ten['value']:.2f}% ({bp(yield_change_bp(ten))})")
    wti = market.get('WTI')
    if wti and wti.get('one_day') is not None and abs(wti['one_day']) >= 1: parts.append(f"oil {pct(wti['one_day'], 1)} to ${wti['value']:.2f}")
    vix = market.get('VIX')
    if vix and vix['value'] >= 20: parts.append(f"VIX {vix['value']:.1f}")
    first = '; '.join(parts) + '.' if parts else ''
    drivers = []
    near = next((e for e in events if e['importance'] == 'high' and e['offset'] <= 2), None)
    if near: drivers.append(f"{near['name']} ({near['when'].lower()}) is the dominant catalyst" + (' with a hike expected' if near['name'].startswith('FOMC') and fed.get('bias') == 'hike' else ''))
    if geo: drivers.append(f"{geo[0]['region']} is the lead geopolitical risk ({geo[0]['momentum']}{', priced in markets' if geo[0]['priced'] else ''})")
    second = '; '.join(drivers) + '.' if drivers else ''
    second = second[:1].upper() + second[1:]
    return (first + ' ' + second).strip()

def summary_text(reg, conditions, macro_read):
    positives = [c['detail'] for c in reg['components'] if c['score'] > 0]
    negatives = [c['detail'] for c in reg['components'] if c['score'] < 0]
    text = f"Regime reads {reg['label']} (score {reg['score']:+d})."
    if positives: text += ' Supportive: ' + ', '.join(positives) + '.'
    if negatives: text += ' Headwinds: ' + ', '.join(negatives) + '.'
    return text + ' ' + macro_read

def analytics_lead(reg, breadth_rows):
    """ANLY's own lead: what the regime inputs say and which are closest to moving, instead of repeating the brief."""
    comps = reg['components']
    pos = [c for c in comps if c['score'] > 0]; neg = [c for c in comps if c['score'] < 0]
    neutral = len(comps) - len(pos) - len(neg)
    head = f"Regime {reg['label']} ({reg['score']:+d}) across {len(comps)} inputs: {len(pos)} supportive, {len(neg)} headwinds, {neutral} neutral."
    parts = []
    if pos: parts.append('Supportive: ' + ', '.join(f"{c['label']} ({c['score']:+d})" for c in pos) + '.')
    if neg: parts.append('Headwinds: ' + ', '.join(f"{c['label']} ({c['score']:+d})" for c in neg) + '.')
    near = sorted((c for c in comps if c.get('near') and c['near'][0] >= 0.5), key=lambda c: c['near'][0], reverse=True)[:3]
    if near: parts.append('Closest to moving the score: ' + '; '.join(f"{c['label']} — {c['near'][1]}" for c in near) + '.')
    elif neutral: parts.append('No neutral input is near its threshold; the score is unlikely to move on small changes.')
    if breadth_rows:
        tones = [r['tone'] for r in breadth_rows]
        parts.append('Breadth reads ' + ('broad' if tones.count('positive') > len(tones) / 2 else 'narrow' if tones.count('negative') > len(tones) / 2 else 'split')
                     + ': ' + '; '.join(f"{r['label'][:1].lower() + r['label'][1:]} {r['value']}" for r in breadth_rows) + '.')
    return {'headline': head, 'summary': ' '.join(parts)}

def build(articles, economy, rates, sources, watchlist, market, now=None, history=None):
    now = now or time.time(); now_ny = datetime.datetime.fromtimestamp(now, NY)
    coverage_prices = market.pop('_coverage', None) or {}
    session = session_state(now_ny, market)
    recent = [a for a in articles if (a.get('published') or 0) >= now - 86400]
    signal = [a for a in recent if not is_noise(a)]
    indicators = {x['series']: x for x in economy}
    reg = regime(market, indicators)
    conditions = market_conditions(market, rates, indicators, session)
    econ = economy_rows(indicators, rates)
    macro_read = macro_verdict(econ)
    events = catalysts(signal, now_ny)
    fed = fed_expectations(signal, rates)
    geo = geopolitics(signal, market)
    sectors = sector_table(market)
    watch = []
    for item in watchlist:
        code = item['symbol'].split(':')[-1]
        terms = [code] + [x.strip() for x in item.get('aliases', '').split(',') if x.strip()]
        hits = [a for a in sorted(signal, key=lambda a: (a.get('relevance', 0), a.get('published', 0)), reverse=True) if has_any(text_of(a), [t.lower() for t in terms])]
        m = market.get('W:' + code)
        watch.append({'symbol': code, 'name': item['name'], 'count': len(hits), 'headline': hits[0]['title'] if hits else None, 'article_id': hits[0]['id'] if hits else None,
                      'price': m['value'] if m else None, 'one_day': m.get('one_day') if m else None, 'five_day': m.get('five_day') if m else None,
                      'rs_5d': (m['five_day'] - market['SPX']['five_day']) if m and market.get('SPX') and m.get('five_day') is not None else None, 'trend': trend_state(m)})
    plays = playbook(market, reg, events, sectors, indicators, watch, fed)
    ranked = sorted(signal, key=lambda a: (a.get('relevance', 0), a.get('published', 0)), reverse=True)
    macro_terms = ('fed', 'fomc', 'yield', 'treasury', 'inflation', 'cpi', 'pce', 'gdp', 'jobs', 'payroll', 'unemployment', 'oil', 'opec', 'tariff', 'sanction', 'war', 'dollar', 'central bank')
    micro_terms = ('earnings', 'guidance', 'revenue', 'profit', 'merger', 'acquisition', 'upgrade', 'downgrade', 'price target', 'buyback', 'dividend', 'ipo', 'bankruptcy', 'beats', 'misses')
    macro = [story(g) for g in cluster([a for a in ranked if has_any(text_of(a), macro_terms)])[:6]]
    micro = [story(g) for g in cluster([a for a in ranked if has_any(text_of(a), micro_terms) and not has_any(text_of(a), macro_terms)])[:6]]
    high_priority = sum(1 for a in recent if a.get('relevance', 0) >= 70)
    event_flag = any(e['importance'] == 'high' and e['offset'] <= 1 for e in events)
    posture = reg['label'] + (' · EVENT RISK' if event_flag else '')
    risks = []
    for e in events:
        if e['importance'] == 'high' and e['offset'] <= 2: risks.append(f"{e['name']} {e['when'].lower()} — {e['timing']}.")
    for g in geo[:3]:
        if g['severity'] >= 4 or g['priced']: risks.append(f"{g['region']}: {g['stories']} stories, {g['momentum']}; {g['channel']}")
    for r in econ:
        if r['tone'] == 'negative' and r['series'] in ('CPIAUCSL', 'BAMLH0A0HYM2', 'UNRATE', 'T10Y2Y'): risks.append(f"{r['name']} {r['value']}: {r['read']}")
    result = {
        'generated_at': now, 'engine': 'rules', 'posture': posture, 'regime': reg, 'session': session,
        'headline': headline(market, reg, events, geo, fed), 'summary': summary_text(reg, conditions, macro_read),
        'analytics': analytics_lead(reg, breadth(market)),
        'tape': tape_tiles(market, session), 'breadth': breadth(market), 'market_as_of': max((m.get('market_time') or 0 for m in market.values()), default=None),
        'conditions': conditions, 'economy': econ, 'macro_read': macro_read, 'fed': fed, 'catalysts': events,
        'geopolitics': geo, 'sectors': sectors, 'levels': key_levels(market), 'playbook': plays, 'risks': risks[:6],
        'themes': themes(signal), 'macro': macro, 'micro': micro, 'watchlist': sorted(watch, key=lambda x: x['count'], reverse=True)[:8],
        'coverage': {'stories': len(recent), 'filtered': len(recent) - len(signal), 'high_priority': high_priority,
                     'sources_online': sum(1 for s in sources if s.get('ok')), 'sources_total': len(sources),
                     'prices_available': coverage_prices.get('available', len(market)), 'prices_expected': coverage_prices.get('expected', len(market))},
        'signal_catalog': [{'id': sid, 'label': label} for sid, label in SIGNALS],
        'disclaimer': 'Rules-based analysis of public data for research. Not investment advice; confirm prices and levels before trading.',
    }
    hy = indicators.get('BAMLH0A0HYM2'); ten = market.get('US10Y'); vix = market.get('VIX'); spx = market.get('SPX')
    result['snapshot'] = {'date': now_ny.strftime('%Y-%m-%d'), 'at': now, 'regime': reg['score'], 'posture': posture,
                          'spx': spx['value'] if spx else None, 'vix': vix['value'] if vix else None,
                          'ten': ten['value'] if ten else None, 'hy': hy['value'] if hy else None}
    result['change'] = since_yesterday(result['snapshot'], history or [])
    result['signals'] = alert_signals(result, market, indicators, reg, events, geo)
    return result

# --------------------------------------------------------------------------- optional analyst note (Claude)

AI_MODEL = os.getenv('BRIEF_MODEL', 'claude-opus-5')
AI_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['headline', 'market_conditions', 'economic_data', 'geopolitics', 'trading_plan', 'risks_to_view'],
    'properties': {
        'headline': {'type': 'string'},
        'market_conditions': {'type': 'string'},
        'economic_data': {'type': 'string'},
        'geopolitics': {'type': 'string'},
        'trading_plan': {'type': 'array', 'items': {'type': 'string'}},
        'risks_to_view': {'type': 'array', 'items': {'type': 'string'}},
    },
}
AI_SYSTEM = """You are the morning strategist for a single active trader's personal market terminal.
You receive a JSON brief computed from live cross-asset prices, FRED economic data, Treasury rates and the last 24 hours of headlines, plus the headlines themselves.
Write a specific, dense, trader-grade note. Every claim must be grounded in the supplied numbers or headlines — cite levels, percentage moves, basis points and dates. Never invent data, events, prices or quotes; if something is not in the input, do not mention it.
- headline: one sentence capturing what matters most today.
- market_conditions: 3-5 sentences on the tape, trend, rates, dollar, commodities, volatility/credit and sector rotation, explaining how they connect.
- economic_data: 3-5 sentences interpreting the latest releases and what they imply for the Fed path; note upcoming releases.
- geopolitics: 2-4 sentences on the active geopolitical situations, the transmission channel to markets, and whether prices are confirming the headlines.
- trading_plan: 4-7 concrete, conditional bullets (sizing, event handling, sectors/tickers with relative strength or weakness, key index levels, what would change the view).
- risks_to_view: 2-4 bullets naming what would invalidate the plan.
This is research for the user's own decisions, not personalized investment advice; do not add disclaimers."""

def ai_available():
    return bool(os.getenv('ANTHROPIC_API_KEY'))

def ai_note(brief, articles):
    import anthropic
    now = time.time()
    headlines = [{'source': a.get('source'), 'time_utc': datetime.datetime.fromtimestamp(a.get('published') or now, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M'),
                  'title': a.get('title'), 'summary': (a.get('summary') or '')[:300]}
                 for a in sorted([a for a in articles if (a.get('published') or 0) >= now - 86400 and not is_noise(a)], key=lambda a: (a.get('relevance', 0), a.get('published', 0)), reverse=True)[:80]]
    skip = ('macro', 'micro', 'themes', 'ai', 'coverage', 'disclaimer', 'signal_catalog', 'snapshot')
    def trim(value):
        if isinstance(value, dict): return {k: trim(v) for k, v in value.items() if k != 'spark'}
        if isinstance(value, list): return [trim(v) for v in value]
        return value
    payload = {k: trim(v) for k, v in brief.items() if k not in skip}
    now_ny = datetime.datetime.fromtimestamp(now, NY).strftime('%A %Y-%m-%d %H:%M ET')
    client = anthropic.Anthropic(max_retries=2, timeout=180)
    response = client.beta.messages.create(
        model=AI_MODEL, max_tokens=16000, system=AI_SYSTEM,
        betas=['server-side-fallback-2026-07-01'], extra_body={'fallbacks': 'default'},
        output_config={'format': {'type': 'json_schema', 'schema': AI_SCHEMA}},
        messages=[{'role': 'user', 'content': f"Current time: {now_ny}\n\nComputed brief:\n{json.dumps(payload, default=str)}\n\nHeadlines (last 24h, ranked):\n{json.dumps(headlines)}"}],
    )
    if response.stop_reason == 'refusal': raise RuntimeError('The model declined to write this brief.')
    if response.stop_reason == 'max_tokens': raise RuntimeError('The analyst note was cut off; try refreshing.')
    text = next(b.text for b in response.content if b.type == 'text')
    return json.loads(text) | {'model': response.model, 'generated_at': time.time()}
