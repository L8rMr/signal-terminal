"""Personal, loopback-only news terminal. Provider credentials never leave the server."""
import concurrent.futures, copy, csv, datetime, email.utils, hashlib, html, io, ipaddress, json, os, re, secrets, socket, threading, time
import urllib.request, urllib.parse, urllib.error, xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from html.parser import HTMLParser
from pathlib import Path
import brief as brief_engine
import schwab
import market_functions as mf

ROOT = Path(__file__).parent
INTERVAL = max(15, int(os.getenv('POLL_SECONDS', '30')))
DATA = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
# Hostnames allowed to read the terminal from the internet (e.g. "signal.alenlvovich.com").
# Visitors on these hosts get a read-only view: every GET works, but settings,
# watchlist, alert-rule and brief-refresh writes stay limited to localhost.
PUBLIC_HOSTS = {h.strip().lower() for h in os.getenv('PUBLIC_HOSTS', '').split(',') if h.strip()}
SEC_USER_AGENT = os.getenv('SEC_USER_AGENT', 'Signal Personal Terminal contact@example.com').strip()
CATEGORIES = ['Markets', 'Business', 'World', 'Policy', 'Technology']
DEFAULT_FEEDS = [
    ('CNBC', 'Markets', 'https://www.cnbc.com/id/100003114/device/rss/rss.html'),
    ('BBC Business', 'Business', 'https://feeds.bbci.co.uk/news/business/rss.xml'),
    ('BBC World', 'World', 'https://feeds.bbci.co.uk/news/world/rss.xml'),
    ('Federal Reserve', 'Policy', 'https://www.federalreserve.gov/feeds/press_all.xml'),
    ('CNBC Tech', 'Technology', 'https://www.cnbc.com/id/19854910/device/rss/rss.html'),
    ('Yahoo Finance', 'Markets', 'https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US'),
    ('Reuters Headlines', 'Markets', 'https://news.google.com/rss/search?q=site%3Areuters.com+business+markets&hl=en-US&gl=US&ceid=US%3Aen'),
    ('Investing.com Stocks', 'Markets', 'https://www.investing.com/rss/news_25.rss'),
    ('WSJ Markets', 'Markets', 'https://feeds.content.dowjones.io/public/rss/RSSMarketsMain'),
    ('MarketWatch', 'Markets', 'https://feeds.content.dowjones.io/public/rss/mw_topstories'),
    ('Bloomberg Markets', 'Markets', 'https://feeds.bloomberg.com/markets/news.rss'),
    ('Seeking Alpha News', 'Markets', 'https://seekingalpha.com/market_currents.xml'),
]
config = {'schema': 3, 'revision': 0, 'feeds': [dict(id=str(i), name=n, category=c, url=u, enabled=True) for i,(n,c,u) in enumerate(DEFAULT_FEEDS)],
          'watchlist': [dict(symbol=s,name=n,aliases=a) for s,n,a in [('AMEX:SPY','S&P 500','S&P 500,SPY'),('NASDAQ:QQQ','Nasdaq 100','Nasdaq,QQQ'),('NASDAQ:AAPL','Apple','Apple,iPhone'),('NASDAQ:NVDA','Nvidia','Nvidia')]],
          'rules': [], 'benzinga_key': ''}
condition = threading.Condition(threading.RLock())
wake = threading.Event()
stream_wake = threading.Event()
csrf = secrets.token_urlsafe(32)
changes = []
state = {'articles': [], 'sources': [], 'updated': None, 'version': 0, 'interval': INTERVAL,
         'alerts': [], 'rates': [], 'rates_updated': None, 'economy': [], 'economy_updated': None,
         'stream': {'status': 'Optional — no subscription', 'connected': False, 'last_received': None, 'gap_since': None}}
seen = {}  # persistent IDs prevent replay/restart alert floods
baseline_sources = set()
sec_cache = {}
sec_request_lock = threading.Lock()
sec_last_request = 0.0
reader_cache = {}
company_news_cache = {}
research_articles = {}
market_cache = {'expires': 0, 'value': None}
quote_cache = {}

MARKET_SYMBOLS = [
    ('Equities', '^GSPC', 'SPX', 'S&P 500'),
    ('Equities', '^NDX', 'NDX', 'Nasdaq 100'),
    ('Equities', '^DJI', 'DJI', 'Dow 30'),
    ('Equities', '^N225', 'NKY', 'Nikkei 225'),
    ('Equities', '^FTSE', 'UKX', 'FTSE 100'),
    ('Rates & FX', '^TNX', 'US10Y', 'US 10Y yield'),
    ('Rates & FX', 'EURUSD=X', 'EURUSD', 'Euro / US dollar'),
    ('Rates & FX', 'JPY=X', 'USDJPY', 'US dollar / yen'),
    ('Rates & FX', 'DX-Y.NYB', 'DXY', 'US Dollar Index'),
    ('Commodities & Vol', 'GC=F', 'GOLD', 'Gold futures'),
    ('Commodities & Vol', 'CL=F', 'WTI', 'WTI crude futures'),
    ('Commodities & Vol', '^VIX', 'VIX', 'Cboe Volatility Index'),
    ('Commodities & Vol', 'BTC-USD', 'BTC', 'Bitcoin'),
]

def clean(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub('<[^>]+>', '', str(value or '')))).strip()

def date_value(value):
    if not value: return None
    try: return email.utils.parsedate_to_datetime(value).timestamp()
    except (ValueError, TypeError, AttributeError):
        try: return datetime.datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
        except (ValueError, TypeError): return None

def article_id(url): return hashlib.sha256(url.encode()).hexdigest()[:20]

def safe_link(url):
    p = urllib.parse.urlparse(str(url))
    return p.scheme in ('http','https') and bool(p.hostname) and not p.username

# SSRF guard for user-added feeds: HTTPS on the default port only, no credentials,
# and every address the host resolves to must be public.
def public_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('Use a public HTTPS feed URL on port 443, without login credentials.')
    try: addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror: raise ValueError('Feed hostname could not be resolved.')
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError('Feed URL must point to a public internet address.')
    return url

def market_symbol(item):
    group, symbol, code, name = item
    return dict(quote(symbol), group=group, code=code, name=name)

def quote(symbol, ttl=300):
    """One Yahoo request per symbol, shared by every view so prices never disagree."""
    now = time.time()
    with condition:
        hit = quote_cache.get(symbol)
        if hit and hit['expires'] > now: return copy.deepcopy(hit['value'])
    value = fetch_symbol(symbol)
    with condition:
        quote_cache[symbol] = {'expires': now + ttl, 'value': copy.deepcopy(value)}
        if len(quote_cache) > 200:
            for key in sorted(quote_cache, key=lambda k: quote_cache[k]['expires'])[:50]: del quote_cache[key]
    return value

def fetch_symbol(symbol):
    url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(symbol, safe='') + '?range=3mo&interval=1d'
    request = urllib.request.Request(public_url(url), headers={
        'User-Agent': 'Mozilla/5.0 (Signal Personal Terminal)',
        'Accept': 'application/json',
    })
    with urllib.request.build_opener(PublicRedirect()).open(request, timeout=12) as response:
        payload = json.load(response)
    result = payload['chart']['result'][0]
    meta = result['meta']
    quote = result['indicators']['quote'][0]
    history = [(timestamp, close) for timestamp, close in zip(result.get('timestamp', []), quote.get('close', [])) if close is not None]
    if len(history) < 2: raise ValueError('Insufficient market history')
    current = float(meta.get('regularMarketPrice') or history[-1][1])
    # chartPreviousClose is the close before the requested three-month range,
    # not the prior session. The penultimate daily bar is the comparable close.
    previous = float(history[-2][1])
    def change(base): return round((current / float(base) - 1) * 100, 2) if base else None
    five_base = history[-6][1] if len(history) >= 6 else history[0][1]
    month_base = history[-23][1] if len(history) >= 23 else history[0][1]
    closes = [float(close) for _, close in history]
    def average(n): return round(sum(closes[-n:]) / n, 4) if len(closes) >= n else None
    return {
        'symbol': symbol,
        'value': current, 'currency': meta.get('currency') or '',
        'exchange': meta.get('fullExchangeName') or meta.get('exchangeName') or '',
        'one_day': change(previous), 'five_day': change(five_base), 'one_month': change(month_base),
        'previous': previous, 'five_ago': float(five_base), 'sma20': average(20), 'sma50': average(50),
        'high_1m': max(closes[-22:]), 'low_1m': min(closes[-22:]), 'high_3m': max(closes), 'low_3m': min(closes),
        'spark': [round(c, 4) for c in closes[-30:]],
        'bar_time': int(history[-1][0]),
        'market_time': int(meta.get('regularMarketTime') or history[-1][0]),
    }

def market_snapshot(force=False):
    now = time.time()
    with condition:
        if not force and market_cache['value'] and market_cache['expires'] > now:
            return copy.deepcopy(market_cache['value'])
    items, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = {pool.submit(market_symbol, item): item for item in MARKET_SYMBOLS}
        for future, item in futures.items():
            try: items.append(future.result())
            except Exception: errors.append(item[2])
    order = {item[2]: index for index, item in enumerate(MARKET_SYMBOLS)}
    items.sort(key=lambda item: order[item['code']])
    if not items:
        with condition:
            if market_cache['value']: return copy.deepcopy(market_cache['value']) | {'stale': True}
        raise LookupError('Cross-asset market data is temporarily unavailable.')
    value = {'as_of': max(item['market_time'] for item in items), 'items': items, 'unavailable': errors,
             'source': 'Yahoo Finance', 'stale': False}
    with condition:
        market_cache.update(expires=now + 300, value=copy.deepcopy(value))
    return value

BRIEF_SYMBOLS = [
    ('Index', '^GSPC', 'SPX', 'S&P 500'), ('Index', '^NDX', 'NDX', 'Nasdaq 100'), ('Index', '^DJI', 'DJI', 'Dow 30'), ('Index', '^RUT', 'RUT', 'Russell 2000'), ('Index', 'RSP', 'RSP', 'S&P 500 equal weight'), ('Index', 'SPY', 'SPY', 'S&P 500 ETF'),
    ('Futures', 'ES=F', 'ES', 'S&P 500 futures'), ('Futures', 'NQ=F', 'NQ', 'Nasdaq 100 futures'),
    ('Volatility', '^VIX', 'VIX', 'VIX'),
    ('Rates', '^TNX', 'US10Y', 'US 10Y yield'), ('Rates', '^FVX', 'US5Y', 'US 5Y yield'), ('Rates', '^TYX', 'US30Y', 'US 30Y yield'), ('Credit', 'HYG', 'HYG', 'High-yield bond ETF'),
    ('FX', 'DX-Y.NYB', 'DXY', 'Dollar index'), ('FX', 'JPY=X', 'USDJPY', 'USD/JPY'), ('FX', 'EURUSD=X', 'EURUSD', 'EUR/USD'),
    ('Commodities', 'CL=F', 'WTI', 'WTI crude'), ('Commodities', 'BZ=F', 'BRENT', 'Brent crude'), ('Commodities', 'GC=F', 'GOLD', 'Gold'),
    ('Commodities', 'HG=F', 'COPPER', 'Copper'), ('Commodities', 'NG=F', 'NATGAS', 'Natural gas'), ('Crypto', 'BTC-USD', 'BTC', 'Bitcoin'),
    ('Global', '^N225', 'NKY', 'Nikkei 225'), ('Global', '^HSI', 'HSI', 'Hang Seng'), ('Global', '^GDAXI', 'DAX', 'DAX'), ('Global', '^FTSE', 'UKX', 'FTSE 100'),
] + [('Sector', code, code, name) for code, name in brief_engine.SECTORS.items()]
brief_market_cache = {'expires': 0, 'key': None, 'value': {}}
brief_ai = {'note': None, 'error': None, 'running': False, 'started': 0}
brief_refresh = {}

def brief_market(watchlist, force=False):
    """Cross-asset, sector and watchlist prices for the brief; cached for five minutes."""
    watch_items = [('Watchlist', w['symbol'].split(':')[-1].replace('.', '-'), 'W:' + w['symbol'].split(':')[-1], w['name']) for w in watchlist]
    key = tuple(item[1] for item in watch_items); now = time.time()
    with condition:
        if not force and brief_market_cache['key'] == key and brief_market_cache['expires'] > now:
            return copy.deepcopy(brief_market_cache['value'])
    wanted = BRIEF_SYMBOLS + watch_items
    result = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(market_symbol, item): item for item in wanted}
        for future, item in futures.items():
            try: result[item[2]] = future.result()
            except Exception: pass
    result['_coverage'] = {'available': len(result), 'expected': len(wanted)}
    with condition:
        if len(result) <= 1 and brief_market_cache['value']: return copy.deepcopy(brief_market_cache['value'])
        brief_market_cache.update(expires=now + 300, key=key, value=copy.deepcopy(result))
    return result

# Re-applies the public_url check on every redirect hop, so a public feed URL
# cannot bounce the server to a private address.
class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def parse_feed(raw, feed):
    root = ET.fromstring(raw)
    articles = []
    atom = '{http://www.w3.org/2005/Atom}'
    items = root.findall('.//item') or root.findall(f'{atom}entry')
    for item in items[:100]:
        is_atom = item.tag == atom+'entry'
        if is_atom:
            title = clean(item.findtext(atom+'title'))
            links = item.findall(atom+'link')
            url = next((x.get('href','') for x in links if x.get('rel','alternate')=='alternate'),'')
            published = date_value(item.findtext(atom+'published') or item.findtext(atom+'updated'))
            summary = item.findtext(atom+'summary') or item.findtext(atom+'content')
        else:
            title, url = clean(item.findtext('title')), (item.findtext('link') or '').strip()
            published = date_value(item.findtext('pubDate'))
            summary = item.findtext('description')
        if not title or not safe_link(url): continue
        articles.append({'id':article_id(url),'title':title,'url':url,'source':feed['name'],'source_id':feed['id'],
                         'category':feed['category'],'published':published,'summary':clean(summary)[:700],'tickers':[], 'delivery':'RSS'})
    if not articles: raise ValueError('No readable RSS or Atom headlines were found.')
    return articles

def fetch(feed):
    status = dict(name=feed['name'],id=feed['id'],category=feed['category'],checked=time.time(),count=0)
    try:
        request = urllib.request.Request(public_url(feed['url']), headers={'User-Agent':'Mozilla/5.0 (Signal Personal News Reader)','Accept':'application/rss+xml, application/atom+xml, application/xml'})
        with urllib.request.build_opener(PublicRedirect()).open(request, timeout=12) as response:
            raw = response.read(3_000_001)
        if len(raw)>3_000_000: raise ValueError('Feed exceeds the 3 MB size limit.')
        articles = parse_feed(raw, feed)
        return articles, dict(status,ok=True,count=len(articles))
    except urllib.error.HTTPError as exc: return [], dict(status,ok=False,error=f'Publisher returned HTTP {exc.code}; retrying automatically.')
    except Exception as exc: return [], dict(status,ok=False,error=str(exc)[:160] if isinstance(exc,ValueError) else 'Feed unavailable or invalid; retrying automatically.')

# Write to a temp file and rename, so a crash mid-write cannot corrupt the cache.
def atomic(name, obj):
    DATA.mkdir(parents=True,exist_ok=True)
    temp = DATA / (name+'.tmp')
    fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600)
    with os.fdopen(fd,'w') as f: json.dump(obj,f)
    temp.replace(DATA/name)

def persist_news():
    try: atomic('news.json',{'articles':state['articles'],'alerts':state['alerts'],'seen':seen})
    except OSError: print('Could not persist headline cache',flush=True)

def broadcast(added=(), removed=()):
    state['version'] += 1
    changes.append({'version': state['version'], 'added': [copy.deepcopy(a) for a in added], 'removed': list(removed)})
    del changes[:-60]
    condition.notify_all()

def light_state():
    """Everything except the article list and FRED history, which move on their own schedules."""
    economy = [{k: v for k, v in item.items() if k != 'history'} for item in state['economy']]
    return {k: state[k] for k in ('sources', 'updated', 'version', 'interval', 'alerts', 'rates', 'rates_updated', 'economy_updated', 'stream')} | {'economy_error': state.get('economy_error')} | {'economy': economy}

def event_payload(since):
    """Full snapshot for a new client, otherwise only the articles that changed."""
    base = light_state()
    window = [c for c in changes if c['version'] > since]
    if since < 0 or not window or window[0]['version'] != since + 1:
        return base | {'type': 'full', 'articles': state['articles']}
    added, removed = [], []
    for change in window:
        added += change['added']; removed += change['removed']
    return base | {'type': 'delta', 'added': added, 'removed': removed}

def matches(article, terms):
    text = article['title']+' '+article.get('summary','')
    return any(re.search(r'(?<!\w)'+re.escape(t.strip())+r'(?!\w)',text,re.I) or t.strip().upper() in article.get('tickers',[]) for t in terms if t.strip())

def rule_matches(article, rule):
    if rule['kind']=='brief': return False  # brief rules fire from market conditions, not headlines
    if rule['kind']=='keyword': return matches(article,rule['terms'].split(','))
    return any(matches(article,[w['symbol'].split(':')[-1]]+w['aliases'].split(',')) for w in config['watchlist'])

def ingest(articles, notify=True):
    """Caller holds condition. First source snapshot never generates historical alerts."""
    now=time.time(); merged={a['id']:a for a in state['articles']}; touched=set()
    for a in articles:
        new=a['id'] not in seen
        seen[a['id']]=now
        old=merged.get(a['id'],{})
        a['received']=old.get('received',now)
        a['relevance']=relevance(a)
        if old!=a: touched.add(a['id'])
        merged[a['id']]=a
        if new and notify:
            for rule in config['rules']:
                if not rule['enabled'] or not a.get('published') or a['published']<rule['created']: continue
                if rule_matches(a,rule):
                    state['alerts'].insert(0,{'id':rule['id']+':'+a['id'],'rule':rule['name'],'at':now,'article':copy.deepcopy(a)})
    before={a['id'] for a in state['articles']}
    state['articles']=sorted(merged.values(),key=lambda a:a['published'] or 0,reverse=True)[:600]
    after={a['id']:a for a in state['articles']}
    changed=([a for a in state['articles'] if a['id'] not in before or a['id'] in touched],[i for i in before if i not in after])
    state['alerts']=state['alerts'][:100]
    if len(seen)>10000:
        keep=sorted(seen,key=seen.get,reverse=True)[:8000]
        for key in set(seen)-set(keep): del seen[key]
    persist_news()
    return changed

def relevance(article):
    """Transparent market relevance score. This is a heuristic, never presented as fact."""
    text=(article.get('title','')+' '+article.get('summary','')).lower()
    score=35
    weights={
        22:('fed','fomc','interest rate','inflation','cpi','jobs report','treasury','yield'),
        18:('earnings','guidance','revenue','profit','merger','acquisition','bankruptcy','sec '),
        15:('tariff','sanction','regulator','antitrust','war','oil','opec'),
        12:('upgrade','downgrade','price target','dividend','buyback','ipo'),
        8:('ai','semiconductor','crypto','bitcoin','currency','dollar')}
    for points,terms in weights.items():
        if any(term in text for term in terms): score+=points
    if article.get('tickers'): score+=10
    if article.get('delivery')=='STREAM': score+=5
    age=max(0,time.time()-(article.get('published') or 0))
    if age<3600: score+=10
    elif age<86400: score+=5
    return min(score,99)

RATE_SERIES=[('DFF','Fed Funds','%'),('SOFR','SOFR','%'),('DGS2','US 2Y','%'),('DGS10','US 10Y','%'),('DGS30','US 30Y','%')]
FRED_INDICATORS=[
    ('CPIAUCSL','CPI inflation','Inflation','% YoY','yoy',12),
    ('PCEPILFE','Core PCE inflation','Inflation','% YoY','yoy',12),
    ('UNRATE','Unemployment rate','Labor','%','level',1),
    ('PAYEMS','Payroll growth','Labor','K jobs MoM','change',1),
    ('ICSA','Initial jobless claims','Labor','K claims','thousands',1),
    ('GDPC1','Real GDP growth','Growth','% annualized QoQ','qoq_ann',1),
    ('INDPRO','Industrial production','Growth','% YoY','yoy',12),
    ('RSAFS','Retail sales','Demand','% YoY','yoy',12),
    ('UMCSENT','Consumer sentiment','Demand','Index','level',1),
    ('T10Y2Y','10Y–2Y curve','Financial conditions','%','level',1),
    ('BAMLH0A0HYM2','High-yield spread','Financial conditions','%','level',1),
    ('DCOILWTICO','WTI crude','Commodities','$/bbl','level',1),
]

def fred_key():
    """FRED API key from the environment, or from DATA_DIR/fred_api_key (saved on the owner page)."""
    key=os.getenv('FRED_API_KEY','').strip()
    if key: return key
    try: return (DATA/'fred_api_key').read_text().strip()
    except OSError: return ''

economy_wake=threading.Event()

def http_text(url,timeout=15,limit=4_000_000,accept='*/*'):
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Signal Personal Terminal)','Accept':accept})
    with urllib.request.urlopen(request,timeout=timeout) as response: return response.read(limit).decode('utf-8','replace')

def nyfed_rate(kind,path,name):
    data=json.loads(http_text(f'https://markets.newyorkfed.org/api/rates/{path}/last/2.json',accept='application/json'))
    rows=data.get('refRates') or []
    if not rows: raise ValueError('No New York Fed observations')
    last=rows[0];previous=rows[1] if len(rows)>1 else last
    return {'series':kind,'name':name,'value':float(last['percentRate']),'previous':float(previous['percentRate']),'date':last['effectiveDate'],'unit':'%','source':'New York Fed'}

def treasury_yields():
    """Daily par yield curve from Treasury.gov (this month, plus last month early in a month)."""
    ns={'a':'http://www.w3.org/2005/Atom','m':'http://schemas.microsoft.com/ado/2007/08/dataservices/metadata','d':'http://schemas.microsoft.com/ado/2007/08/dataservices'}
    today=datetime.date.today();months=[today.strftime('%Y%m'),(today.replace(day=1)-datetime.timedelta(days=1)).strftime('%Y%m')]
    rows=[]
    for month in months:
        raw=http_text('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month='+month,accept='application/xml')
        for entry in ET.fromstring(raw).findall('a:entry',ns):
            props=entry.find('a:content/m:properties',ns)
            if props is None: continue
            row={child.tag.split('}')[1]:(child.text or '') for child in props}
            if row.get('NEW_DATE'): rows.append(row)
        if len(rows)>=2: break
    rows.sort(key=lambda r:r['NEW_DATE'])
    if len(rows)<1: raise ValueError('No Treasury yield observations')
    result=[]
    for series,field,name in (('DGS2','BC_2YEAR','US 2Y'),('DGS10','BC_10YEAR','US 10Y'),('DGS30','BC_30YEAR','US 30Y')):
        valid=[r for r in rows if r.get(field)]
        if not valid: continue
        last=valid[-1];previous=valid[-2] if len(valid)>1 else last
        result.append({'series':series,'name':name,'value':float(last[field]),'previous':float(previous[field]),'date':last['NEW_DATE'][:10],'unit':'%','source':'US Treasury'})
    return result

def keyless_rates():
    rates=[]
    for args in (('DFF','unsecured/effr','Fed Funds'),('SOFR','secured/sofr','SOFR')):
        try: rates.append(nyfed_rate(*args))
        except Exception as exc: print('Rates: New York Fed',args[0],'failed:',type(exc).__name__,str(exc)[:120],flush=True)
    try: rates+=treasury_yields()
    except Exception as exc: print('Rates: Treasury yields failed:',type(exc).__name__,str(exc)[:120],flush=True)
    return rates

def fred_observations(series,limit=None):
    """[(date, value)] from the FRED API when FRED_API_KEY is set, otherwise the public CSV download."""
    key=fred_key()
    if key:
        params={'series_id':series,'api_key':key,'file_type':'json'}
        if limit: params.update(sort_order='desc',limit=limit)
        data=json.loads(http_text('https://api.stlouisfed.org/fred/series/observations?'+urllib.parse.urlencode(params),accept='application/json'))
        values=[(o['date'],float(o['value'])) for o in data.get('observations',[]) if o.get('value') not in (None,'.','')]
        return sorted(values)
    content=http_text('https://fred.stlouisfed.org/graph/fredgraph.csv?'+urllib.parse.urlencode({'id':series}))
    values=[]
    for row in csv.DictReader(io.StringIO(content)):
        raw=row.get(series)
        if raw in (None,'.',''): continue
        try: values.append((row.get('observation_date') or row.get('DATE'),float(raw)))
        except ValueError: continue
    return values

def fetch_rate(item):
    series,name,unit=item
    values=fred_observations(series,limit=5)
    if not values: raise ValueError('No rate observations')
    last=values[-1];previous=values[-2] if len(values)>1 else last
    return {'series':series,'name':name,'value':last[1],'previous':previous[1],'date':last[0],'unit':unit,'source':'FRED'}

def transformed_history(values, mode, lag):
    output=[]
    for i,(date,value) in enumerate(values):
        prior=values[i-lag][1] if i>=lag else None
        if mode=='yoy': result=(value/prior-1)*100 if prior not in (None,0) else None
        elif mode=='qoq_ann': result=((value/prior)**4-1)*100 if prior not in (None,0) else None
        elif mode=='change': result=value-prior if prior is not None else None
        elif mode=='thousands': result=value/1000
        else: result=value
        if result is not None: output.append({'date':date,'value':round(result,4)})
    return output

def fetch_indicator(item):
    series,name,group,unit,mode,lag=item
    values=fred_observations(series)
    history=transformed_history(values,mode,lag)[-120:]
    if not history: raise ValueError('No FRED observations')
    latest=history[-1];previous=history[-2] if len(history)>1 else latest
    return {'series':series,'name':name,'group':group,'unit':unit,'value':latest['value'],'previous':previous['value'],
            'date':latest['date'],'history':history,'url':f'https://fred.stlouisfed.org/series/{series}'}

fred_errors={}
def fetch_indicator_safe(item):
    try:
        result=fetch_indicator(item);fred_errors.pop(item[0],None);return result
    except Exception as exc:
        fred_errors[item[0]]=f'{type(exc).__name__}: {str(exc)[:120]}';return None

def sec_json(url, ttl=900):
    """Fetch an official SEC JSON resource with a shared cache and conservative rate limit."""
    global sec_last_request
    cached=sec_cache.get(url)
    if cached and cached[0]>time.time(): return copy.deepcopy(cached[1])
    with sec_request_lock:
        cached=sec_cache.get(url)
        if cached and cached[0]>time.time(): return copy.deepcopy(cached[1])
        delay=.25-(time.monotonic()-sec_last_request)
        if delay>0: time.sleep(delay)
        request=urllib.request.Request(url,headers={'User-Agent':SEC_USER_AGENT,'Accept':'application/json','Accept-Encoding':'identity'})
        try:
            with urllib.request.urlopen(request,timeout=18) as response: raw=response.read(15_000_001)
        finally: sec_last_request=time.monotonic()
        if len(raw)>15_000_000: raise ValueError('SEC response exceeded the 15 MB safety limit.')
        value=json.loads(raw)
        sec_cache[url]=(time.time()+ttl,value)
        return copy.deepcopy(value)

def resolve_sec_company(query):
    query=query.strip().upper()
    if re.fullmatch(r'\d{1,10}',query): return query.zfill(10), None, None
    if not re.fullmatch(r'[A-Z0-9.-]{1,10}',query): raise ValueError('Enter a US ticker or a CIK with up to 10 digits.')
    companies=sec_json('https://www.sec.gov/files/company_tickers.json',86400)
    match=next((x for x in companies.values() if str(x.get('ticker','')).upper()==query),None)
    if not match: raise LookupError('Ticker was not found in the SEC company list. Try the company CIK.')
    return str(match['cik_str']).zfill(10),match.get('ticker'),match.get('title')

SEC_METRICS=[
    ('Revenue',['RevenueFromContractWithCustomerExcludingAssessedTax','Revenues','SalesRevenueNet'],'USD',False),
    ('Gross profit',['GrossProfit'],'USD',False),
    ('Net income',['NetIncomeLoss'],'USD',False),
    ('Operating income',['OperatingIncomeLoss'],'USD',False),
    ('Assets',['Assets'],'USD',True),
    ('Current assets',['AssetsCurrent'],'USD',True),
    ('Liabilities',['Liabilities'],'USD',True),
    ('Current liabilities',['LiabilitiesCurrent'],'USD',True),
    ('Stockholders equity',['StockholdersEquity'],'USD',True),
    ('Cash & equivalents',['CashAndCashEquivalentsAtCarryingValue','CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents'],'USD',True),
    ('Long-term debt',['LongTermDebtAndFinanceLeaseObligationsNoncurrent','LongTermDebtNoncurrent','LongTermDebt'],'USD',True),
    ('Operating cash flow',['NetCashProvidedByUsedInOperatingActivities'],'USD',False),
    ('Capital expenditures',['PaymentsToAcquirePropertyPlantAndEquipment'],'USD',False),
    ('Research & development',['ResearchAndDevelopmentExpense'],'USD',False),
    ('Diluted EPS',['EarningsPerShareDiluted'],'USD/shares',False),
    ('Shares outstanding',['EntityCommonStockSharesOutstanding'],'shares',True),
]

class ReaderParser(HTMLParser):
    BLOCKED={'script','style','noscript','svg','nav','footer','header','form','aside'}
    def __init__(self): super().__init__(convert_charrefs=True);self.blocked=0;self.in_p=0;self.parts=[];self.current=[]
    def handle_starttag(self,tag,attrs):
        tag=tag.lower()
        if tag in self.BLOCKED: self.blocked+=1
        elif tag=='p' and not self.blocked: self.in_p+=1;self.current=[]
    def handle_endtag(self,tag):
        tag=tag.lower()
        if tag in self.BLOCKED and self.blocked: self.blocked-=1
        elif tag=='p' and self.in_p:
            self.in_p-=1;text=clean(' '.join(self.current))
            if len(text)>=55 and text not in self.parts: self.parts.append(text)
            self.current=[]
    def handle_data(self,value):
        if self.in_p and not self.blocked: self.current.append(value)

def article_reader(article):
    cached=reader_cache.get(article['id'])
    if cached and cached[0]>time.time(): return copy.deepcopy(cached[1])
    url=public_url(article['url'])
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Signal Personal Article Reader)','Accept':'text/html,application/xhtml+xml'})
    with urllib.request.build_opener(PublicRedirect()).open(request,timeout=15) as response:
        content_type=response.headers.get_content_type()
        raw=response.read(3_000_001)
        charset=response.headers.get_content_charset() or 'utf-8'
    if content_type not in ('text/html','application/xhtml+xml'): raise ValueError('This publisher did not return a readable web article.')
    if len(raw)>3_000_000: raise ValueError('This article page is too large for the dashboard reader.')
    parser=ReaderParser();parser.feed(raw.decode(charset,'replace'))
    paragraphs=[];total=0
    for paragraph in parser.parts:
        if total+len(paragraph)>16000: break
        paragraphs.append(paragraph);total+=len(paragraph)
        if len(paragraphs)>=28: break
    if not paragraphs and article.get('summary'): paragraphs=[article['summary']]
    if not paragraphs: raise ValueError('The publisher page could not be extracted. Open the original article instead.')
    value={'id':article['id'],'title':article['title'],'source':article['source'],'published':article.get('published'),
           'url':article['url'],'paragraphs':paragraphs,'truncated':len(parser.parts)>len(paragraphs)}
    reader_cache[article['id']]=(time.time()+900,value)
    return copy.deepcopy(value)

def fact_observations(concept, unit, instant, forms):
    units=concept.get('units',{})
    rows=units.get(unit) or (next(iter(units.values()),[]) if units else [])
    chosen={}
    for row in rows:
        form=str(row.get('form',''))
        if form not in forms or not isinstance(row.get('val'),(int,float)) or not row.get('end'): continue
        if not instant and form.startswith('10-Q') and row.get('start'):
            try:
                days=(datetime.date.fromisoformat(row['end'])-datetime.date.fromisoformat(row['start'])).days
                if days>140: continue
            except ValueError: pass
        key=(row['end'],form)
        if key not in chosen or str(row.get('filed',''))>str(chosen[key].get('filed','')): chosen[key]=row
    result=[]
    for row in sorted(chosen.values(),key=lambda x:(x.get('end',''),x.get('filed','')),reverse=True)[:6]:
        result.append({k:row.get(k) for k in ('val','start','end','filed','form','fy','fp','frame')}|{'unit':unit})
    return result

def sec_metric_facts(companyfacts):
    taxonomy=companyfacts.get('facts',{}).get('us-gaap',{})
    result=[]
    for label,candidates,unit,instant in SEC_METRICS:
        tag=next((tag for tag in candidates if tag in taxonomy),None)
        if not tag: continue
        concept=taxonomy[tag]
        annual=fact_observations(concept,unit,instant,('10-K','10-K/A','20-F','20-F/A','40-F','40-F/A'))
        quarterly=fact_observations(concept,unit,instant,('10-Q','10-Q/A'))
        if annual or quarterly:
            result.append({'label':label,'tag':tag,'description':clean(concept.get('description',''))[:400],
                           'annual':annual,'quarterly':quarterly})
    return result

def recent_filings(submissions):
    recent=submissions.get('filings',{}).get('recent',{})
    accessions=recent.get('accessionNumber',[]);result=[]
    for i,accession in enumerate(accessions[:120]):
        def field(name):
            values=recent.get(name,[])
            return values[i] if i<len(values) else ''
        form=field('form');document=field('primaryDocument')
        if not re.fullmatch(r'[0-9-]+',str(accession)) or not document: continue
        accession_path=str(accession).replace('-','')
        url=f"https://www.sec.gov/Archives/edgar/data/{int(submissions['cik'])}/{accession_path}/{urllib.parse.quote(str(document),safe='._-/')}"
        result.append({'form':form,'filed':field('filingDate'),'report_date':field('reportDate'),
                       'description':field('primaryDocDescription') or form,'accession':accession,'url':url})
        if len(result)>=60: break
    return result

def company_news_fallback(ticker, company_name):
    key=(ticker or company_name).upper();cached=company_news_cache.get(key)
    if cached and cached[0]>time.time(): return copy.deepcopy(cached[1])
    base=re.sub(r'[,\s]+(?:INCORPORATED|INC\.?|CORPORATION|CORP\.?|COMPANY|CO\.?|PLC|LTD\.?)$','',company_name or '',flags=re.I).strip()
    query=' OR '.join(f'"{x}"' for x in (ticker,base) if x)
    url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':query+' stock','hl':'en-US','gl':'US','ceid':'US:en'})
    request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Signal Personal News Reader)','Accept':'application/rss+xml, application/xml'})
    with urllib.request.urlopen(request,timeout=15) as response: raw=response.read(3_000_001)
    if len(raw)>3_000_000: raise ValueError('Company news search response was too large.')
    root=ET.fromstring(raw);articles=[]
    for item in root.findall('.//item')[:40]:
        title=clean(item.findtext('title'));link=(item.findtext('link') or '').strip();publisher=clean(item.findtext('source')) or 'Google News'
        if not title or not safe_link(link): continue
        suffix=' - '+publisher
        if title.endswith(suffix): title=title[:-len(suffix)]
        article={'id':'search:'+article_id(link),'title':title,'url':link,'source':publisher,'source_id':'company-search',
                 'category':'Markets','published':date_value(item.findtext('pubDate')),'summary':f'Company news search result from {publisher}.',
                 'tickers':[ticker] if ticker else [],'delivery':'SEARCH'}
        article['relevance']=relevance(article);articles.append(article);research_articles[article['id']]=copy.deepcopy(article)
    company_news_cache[key]=(time.time()+900,articles)
    if len(research_articles)>1000:
        for article_key in list(research_articles)[:-700]: research_articles.pop(article_key,None)
    return copy.deepcopy(articles)

def company_research(query):
    cik,ticker,title=resolve_sec_company(query)
    submissions=sec_json(f'https://data.sec.gov/submissions/CIK{cik}.json',900)
    facts=sec_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json',900)
    tickers=submissions.get('tickers') or ([ticker] if ticker else [])
    exchanges=submissions.get('exchanges') or []
    address=(submissions.get('addresses') or {}).get('business') or {}
    address_text=', '.join(str(x) for x in (address.get('city'),address.get('stateOrCountry'),address.get('zipCode')) if x)
    result={
        'company':{'name':submissions.get('name') or facts.get('entityName') or title,'cik':cik,'tickers':tickers,
                   'exchanges':exchanges,'sic':submissions.get('sic'),'industry':submissions.get('sicDescription'),
                   'fiscal_year_end':submissions.get('fiscalYearEnd'),'state':submissions.get('stateOfIncorporation'),
                   'category':submissions.get('category'),'phone':submissions.get('phone'),'website':submissions.get('website'),
                   'investor_website':submissions.get('investorWebsite'),'address':address_text},
        'metrics':sec_metric_facts(facts),'filings':recent_filings(submissions),'updated':time.time(),
        'sec_url':f'https://www.sec.gov/edgar/browse/?CIK={cik}&owner=exclude'
    }
    company_name=result['company']['name'] or '';ticker_value=tickers[0] if tickers else ticker
    terms=[x for x in (ticker_value,re.sub(r'[,\s]+(?:INCORPORATED|INC\.?|CORPORATION|CORP\.?|COMPANY|CO\.?|PLC|LTD\.?)$','',company_name,flags=re.I).strip()) if x]
    with condition:
        has_fresh=any((a.get('published') or 0)>=time.time()-86400 and matches(a,terms) for a in state['articles'])
    result['archive_news']=[]
    if not has_fresh:
        try: result['archive_news']=company_news_fallback(ticker_value,company_name)
        except Exception: pass
    return result

SYMBOL_RE=re.compile(r'[A-Z0-9][A-Z0-9.\-]{0,11}')

def clean_symbol(value):
    symbol=str(value or '').strip().upper().replace('/','-')
    if not SYMBOL_RE.fullmatch(symbol): raise ValueError('Enter a ticker such as AAPL.')
    return symbol

def fa_report(symbol,periods):
    cik,_,_=resolve_sec_company(symbol)
    facts=sec_json(f'https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json',900)
    report=mf.financials(facts,'quarter' if periods=='quarter' else 'annual')
    report.update(symbol=symbol,name=facts.get('entityName') or symbol,cik=cik)
    return report

ns_cache={}
def news_search(query):
    """NS: every cached headline that contains all the words, plus a Google News search."""
    terms=[t for t in re.split(r'\s+',query.strip()) if t][:8]
    if not terms: raise ValueError('Type words to search for, e.g. NS fed rate cut.')
    q=' '.join(terms);key=q.upper();now=time.time()
    words=[t.upper() for t in terms]
    with condition:
        local=[copy.deepcopy(a) for a in state['articles'] if all(w in f"{a.get('title','')} {a.get('summary','')}".upper() for w in words)]
    remote=[]
    cached=ns_cache.get(key)
    if cached and cached[0]>now: remote=copy.deepcopy(cached[1])
    else:
        try:
            url='https://news.google.com/rss/search?'+urllib.parse.urlencode({'q':q+' when:7d','hl':'en-US','gl':'US','ceid':'US:en'})
            request=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (Signal Personal News Reader)','Accept':'application/rss+xml, application/xml'})
            with urllib.request.urlopen(request,timeout=15) as response: raw=response.read(3_000_001)
            if len(raw)<=3_000_000:
                for item in ET.fromstring(raw).findall('.//item')[:60]:
                    title=clean(item.findtext('title'));link=(item.findtext('link') or '').strip();publisher=clean(item.findtext('source')) or 'Google News'
                    if not title or not safe_link(link): continue
                    if title.endswith(' - '+publisher): title=title[:-len(publisher)-3]
                    article={'id':'search:'+article_id(link),'title':title,'url':link,'source':publisher,'source_id':'news-search','category':'Markets',
                             'published':date_value(item.findtext('pubDate')),'summary':f'News search result from {publisher}.','tickers':[],'delivery':'SEARCH'}
                    article['relevance']=relevance(article);remote.append(article);research_articles[article['id']]=copy.deepcopy(article)
            ns_cache[key]=(now+300,copy.deepcopy(remote))
            if len(ns_cache)>200:
                for k in sorted(ns_cache,key=lambda k:ns_cache[k][0])[:80]: ns_cache.pop(k,None)
        except Exception: pass
    seen=set();results=[]
    for a in sorted(local+remote,key=lambda a:a.get('published') or 0,reverse=True):
        k=re.sub(r'\W+','',a['title'].lower())[:80]
        if k in seen: continue
        seen.add(k);results.append({x:a.get(x) for x in ('id','title','url','source','published','delivery','tickers')})
    return {'query':q,'results':results[:100],'local':len(local),'at':int(now)}

def brief_history(snapshot=None):
    """One small row per day so the brief can say what changed since yesterday."""
    try: rows=json.loads((DATA/'brief_history.json').read_text())
    except (OSError,ValueError): rows=[]
    if not isinstance(rows,list): rows=[]
    if snapshot:
        rows=[r for r in rows if r.get('date')!=snapshot['date']]+[snapshot]
        rows=sorted(rows,key=lambda r:r.get('date') or '')[-40:]
        try: atomic('brief_history.json',rows)
        except OSError: pass
    return rows

def brief_alerts(brief):
    """Fire enabled 'brief' rules when a market condition flips, not on a headline."""
    with condition: rules=[r for r in config['rules'] if r['kind']=='brief' and r['enabled']]
    if not rules: return
    signals={s['id']:s for s in brief.get('signals',[])}
    fired=[]
    with condition:
        for rule in rules:
            signal=signals.get(rule['terms'])
            if not signal: continue
            key=rule['id']+':'+signal['fingerprint']
            if any(a['id']==key for a in state['alerts']): continue
            fired.append({'id':key,'rule':rule['name'],'at':time.time(),
                          'article':{'id':'brief','title':signal['text'],'source':'Daily Market Brief','category':'Markets',
                                     'published':brief['generated_at'],'summary':signal.get('detail',''),'url':'','relevance':90,'delivery':'BRIEF'}})
        if fired:
            state['alerts']=(fired+state['alerts'])[:100];broadcast()

def run_ai_note(brief, articles):
    try:
        note=brief_engine.ai_note(brief,articles)
        with condition: brief_ai.update(note=note,error=None)
    except Exception as exc:
        print('Brief analyst note failed',type(exc).__name__,str(exc)[:200],flush=True)
        with condition: brief_ai.update(error=f'Analyst note unavailable ({type(exc).__name__}).')
    finally:
        with condition: brief_ai['running']=False

def build_briefing(force=False):
    with condition:
        articles=copy.deepcopy(state['articles']);economy=copy.deepcopy(state['economy']);rates=copy.deepcopy(state['rates']);sources=copy.deepcopy(state['sources']);watchlist=copy.deepcopy(config['watchlist'])
    market=brief_market(watchlist,force=force)
    brief=brief_engine.build(articles,economy,rates,sources,watchlist,market,history=brief_history())
    brief_history(brief.pop('snapshot'))
    brief_alerts(brief)
    with condition:
        if brief_refresh: brief['refresh']=copy.deepcopy(brief_refresh)
    if not brief_engine.ai_available():
        brief['ai']={'status':'off'};return brief
    start_note=False
    with condition:
        note=brief_ai['note'];stale=not note or time.time()-note['generated_at']>1800
        if (force or stale) and not brief_ai['running'] and time.time()-brief_ai['started']>60:
            brief_ai.update(running=True,started=time.time());start_note=True
        running=brief_ai['running'];error=brief_ai['error']
    if start_note: threading.Thread(target=run_ai_note,args=(copy.deepcopy(brief),articles),daemon=True).start()
    brief['ai']={'status':'generating' if running else 'error' if error and not note else 'ready' if note else 'idle','note':note,'error':error}
    return brief

def rates_loop():
    """Fed funds and SOFR from the New York Fed, Treasury yields from Treasury.gov (no key needed).
    FRED fills any series those sources miss, if it is reachable."""
    while True:
        rates=keyless_rates();have={r['series'] for r in rates}
        for item in RATE_SERIES:
            if item[0] in have: continue
            try: rates.append(fetch_rate(item))
            except Exception as exc: print('Rates: FRED',item[0],'failed:',type(exc).__name__,flush=True)
        order={s[0]:i for i,s in enumerate(RATE_SERIES)};rates.sort(key=lambda r:order.get(r['series'],99))
        if rates:
            with condition: state['rates']=rates;state['rates_updated']=time.time();broadcast()
        time.sleep(900 if rates else 120)

def economy_loop():
    while True:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                results=[x for x in pool.map(fetch_indicator_safe,FRED_INDICATORS) if x]
            error=None
            if not results:
                error=('FRED is not answering from this server. Add a free FRED API key on the owner setup page to use the official API.'
                       if not fred_key() else 'FRED API request failed: '+next(iter(fred_errors.values()),'unknown error'))
                print('FRED indicators unavailable:',next(iter(fred_errors.values()),''),flush=True)
            with condition:
                if results: state['economy']=results;state['economy_updated']=time.time()
                state['economy_error']=error;broadcast()
        except Exception as exc: print('FRED indicator refresh incomplete',type(exc).__name__,flush=True)
        economy_wake.wait(900 if state['economy'] else 300);economy_wake.clear()

def public_settings():
    return {k:copy.deepcopy(config[k]) for k in ('revision','feeds','watchlist','rules')} | {'benzinga_configured':bool(config['benzinga_key'])}

# ---------- abuse protection ----------
# Endpoints that make the server call Yahoo, SEC, Cboe, Google News or a publisher.
HEAVY_PATHS={'/api/des','/api/fa','/api/most','/api/omon','/api/ns','/api/company','/api/reader','/api/briefing/refresh'}
RATE_CAPACITY,RATE_PER_SECOND=30,0.5      # burst of 30, then one request every 2 s per visitor
SSE_MAX_TOTAL,SSE_MAX_PER_IP=60,4
rate_lock=threading.Lock();rate_buckets={};sse_clients={}

def rate_allow(ip):
    """Token bucket per visitor. Returns 0 if allowed, else seconds to wait."""
    now=time.monotonic()
    with rate_lock:
        tokens,last=rate_buckets.get(ip,(RATE_CAPACITY,now))
        tokens=min(RATE_CAPACITY,tokens+(now-last)*RATE_PER_SECOND)
        if tokens<1:
            rate_buckets[ip]=(tokens,now);return max(1,int((1-tokens)/RATE_PER_SECOND)+1)
        rate_buckets[ip]=(tokens-1,now)
        if len(rate_buckets)>5000:
            for key in sorted(rate_buckets,key=lambda k:rate_buckets[k][1])[:2500]: rate_buckets.pop(key,None)
        return 0

def sse_acquire(ip):
    with rate_lock:
        if sum(sse_clients.values())>=SSE_MAX_TOTAL or sse_clients.get(ip,0)>=SSE_MAX_PER_IP: return False
        sse_clients[ip]=sse_clients.get(ip,0)+1;return True

def sse_release(ip):
    with rate_lock:
        n=sse_clients.get(ip,0)-1
        if n>0: sse_clients[ip]=n
        else: sse_clients.pop(ip,None)

CSP=("default-src 'self'; script-src 'self' https://s3.tradingview.com; style-src 'self' 'unsafe-inline'; "
     "img-src 'self' data: https:; font-src 'self' data:; connect-src 'self'; "
     "frame-src https://*.tradingview.com https://*.tradingview-widget.com; frame-ancestors 'none'; "
     "base-uri 'self'; form-action 'self'; object-src 'none'")

# ---------- asset versions ----------
# index.html references /x.js?v=… ; the server replaces each ?v= with a hash of the file,
# so a deploy can never serve a stale script and nobody has to bump numbers by hand.
asset_versions={}
def asset_version(name):
    path=ROOT/'static'/name
    try: stat=path.stat()
    except OSError: return '0'
    hit=asset_versions.get(name)
    if hit and hit[0]==stat.st_mtime_ns: return hit[1]
    digest=hashlib.sha1(path.read_bytes()).hexdigest()[:10]
    asset_versions[name]=(stat.st_mtime_ns,digest);return digest

def versioned_index():
    html=(ROOT/'static'/'index.html').read_text()
    return re.sub(r'/([A-Za-z0-9_.-]+\.(?:js|css))\?v=[A-Za-z0-9]+',lambda m:f'/{m.group(1)}?v={asset_version(m.group(1))}',html)

STARTED=time.time()
def health():
    with condition:
        sources=state['sources'];ok_sources=[s for s in sources if s.get('ok')]
        failing=[s.get('name') for s in sources if not s.get('ok')]
        rates=len(state['rates']);economy=len(state['economy'])
        rates_age=round((time.time()-state['rates_updated'])/60) if state.get('rates_updated') else None
        news_age=round(time.time()-state['updated']) if state.get('updated') else None
    problems=[]
    if sources and len(failing)>1: problems.append(f'{len(failing)} news sources down')
    if news_age is not None and news_age>INTERVAL*6: problems.append('news polling stalled')
    if not rates: problems.append('rates unavailable')
    if not economy: problems.append('economic data unavailable')
    return {'ok':True,'status':'degraded' if problems else 'ok','problems':problems,'version':state['version'],
            'uptime_minutes':round((time.time()-STARTED)/60),
            'sources':{'ok':len(ok_sources),'total':len(sources),'failing':failing,'last_poll_seconds':news_age},
            'rates':{'series':rates,'age_minutes':rates_age},'economy':{'series':economy,'error':state.get('economy_error')}}

OWNER_COOKIE='signal_owner'
owner_key=''

RETIRED_YAHOO_FEED='https://finance.yahoo.com/news/rssindex'
YAHOO_FEED='https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US'

def load_owner_key():
    """Random owner key kept in the data volume. Whoever holds it gets the owner view
    (live Schwab quotes, editing) on a public hostname; localhost is always owner."""
    global owner_key
    path=DATA/'owner_key'
    try: owner_key=path.read_text().strip()
    except OSError: owner_key=''
    if len(owner_key)<32:
        owner_key=secrets.token_urlsafe(32);path.write_text(owner_key);os.chmod(path,0o600)

def load():
    DATA.mkdir(parents=True,exist_ok=True)
    load_owner_key()
    migrated=False
    for filename in ('settings.json','news.json'):
        try:
            value=json.loads((DATA/filename).read_text())
            if filename=='settings.json':
                prior_schema=value.get('schema',1)
                config.update(value)
                if prior_schema<2 and not any(f.get('name')=='Yahoo Finance' for f in config['feeds']):
                    config['feeds'].append(dict(id='5',name='Yahoo Finance',category='Markets',url='https://feeds.finance.yahoo.com/rss/2.0/headline?s=%5EGSPC,%5EDJI,%5EIXIC&region=US&lang=en-US',enabled=True))
                    config['schema']=2;migrated=True
                if prior_schema<3:
                    existing_names={f.get('name') for f in config['feeds']};existing_urls={f.get('url') for f in config['feeds']}
                    for index,(name,category,url) in enumerate(DEFAULT_FEEDS):
                        if name not in existing_names and url not in existing_urls:
                            config['feeds'].append(dict(id=str(index),name=name,category=category,url=url,enabled=True))
                            existing_names.add(name);existing_urls.add(url)
                    config['schema']=3;migrated=True
                if config.get('schema',1)<4:
                    # Yahoo retired /news/rssindex (HTTP 404) in 2026; its headline feed still works.
                    for feed in config['feeds']:
                        if feed.get('url')==RETIRED_YAHOO_FEED: feed['url']=YAHOO_FEED
                    config['schema']=4;migrated=True
            else:
                state['articles']=value.get('articles',[]);state['alerts']=value.get('alerts',[])
                seen.update(value.get('seen',{}));seen.update({a['id']:a.get('received',time.time()) for a in state['articles']})
        except (OSError,ValueError,TypeError): pass
    if migrated:
        try: atomic('settings.json',config)
        except OSError: pass

def poll():
    while True:
        wake.clear()
        with condition: feeds=copy.deepcopy([f for f in config['feeds'] if f['enabled']])
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool: results=list(pool.map(fetch,feeds))
        with condition:
            active={f['id'] for f in config['feeds'] if f['enabled']}
            statuses=[];changed_added=[];changed_removed=[]
            for feed,(articles,status) in zip(feeds,results):
                if feed['id'] not in active: continue
                added,removed=ingest(articles,notify=feed['id'] in baseline_sources)
                changed_added+=added;changed_removed+=removed
                if status['ok']: baseline_sources.add(feed['id'])
                statuses.append(status)
            state.update(sources=statuses,updated=time.time());broadcast(changed_added,changed_removed)
        wake.wait(INTERVAL)

def handle_benzinga(message):
    if message in ('pong','ping'): return
    event=json.loads(message)
    if not isinstance(event,dict) or not isinstance(event.get('data'),dict): return
    d=event['data'];c=d.get('content') or {};action=str(d.get('action','')).lower()
    record_id=str(d.get('id') or c.get('id') or '')
    if action not in ('created','updated','deleted') or not record_id: return
    key='bz:'+record_id
    with condition:
        if action=='deleted':
            removed=[a['id'] for a in state['articles'] if a['id']==key]
            state['articles']=[a for a in state['articles'] if a['id']!=key]
            state['alerts']=[a for a in state['alerts'] if a['article']['id']!=key]
            persist_news()
            state['stream']['last_received']=time.time();broadcast((),removed);return
        else:
            if not c.get('title') or not safe_link(c.get('url','')): return
            a=dict(id=key,title=clean(c['title']),url=c['url'],source='Benzinga',source_id='benzinga',category='Markets',
                   published=date_value(c.get('created') or d.get('timestamp')),summary=clean(c.get('teaser') or c.get('body'))[:700],
                   tickers=[s['name'].upper() for s in c.get('stocks',[]) if isinstance(s,dict) and isinstance(s.get('name'),str)],delivery='STREAM')
            added,removed=ingest([a])
            state['stream']['last_received']=time.time();broadcast(added,removed);return
        state['stream']['last_received']=time.time();broadcast()

def stream_status(status, connected=False, gap=False):
    with condition:
        state['stream'].update(status=status,connected=connected)
        if gap: state['stream']['gap_since']=time.time()
        broadcast()

# Optional paid Benzinga news stream. Idle unless a key is saved in settings.
def benzinga_loop():
    import websocket
    backoff=2
    while True:
        stream_wake.clear()
        with condition: key=config['benzinga_key']
        if not key:
            stream_status('Optional — no subscription');stream_wake.wait();continue
        ws=None
        try:
            stream_status('Connecting')
            ws=websocket.create_connection('wss://api.benzinga.com/api/v1/news/stream?'+urllib.parse.urlencode({'token':key}),timeout=12,redirect_limit=0)
            ws.settimeout(2)
            stream_status('Connected',True)
            ws.send('replay');backoff=2;last_ping=time.monotonic()
            while not stream_wake.is_set():
                if time.monotonic()-last_ping>=30: ws.send('ping');last_ping=time.monotonic()
                try: message=ws.recv()
                except websocket.WebSocketTimeoutException: continue
                if not message: raise ConnectionError('Closed')
                if message=='ping': ws.send('pong');continue
                try: handle_benzinga(message)
                except (ValueError,TypeError,KeyError,AttributeError): continue
        except Exception as exc:
            code=getattr(exc,'status_code',None)
            msg='Authentication failed — check your stream API key' if code in (401,403) else 'Another connection is using this key — retrying' if code==429 else 'Connection interrupted — retrying'
            stream_status(msg,gap=True)  # Never expose exceptions containing the token URL.
        finally:
            if ws:
                try: ws.close()
                except Exception: pass
        stream_wake.wait(backoff);backoff=min(backoff*2,30)

def validated_settings(body):
    result=copy.deepcopy(config)
    if 'feeds' in body:
        feeds=body['feeds']
        if not isinstance(feeds,list) or len(feeds)>20: raise ValueError('Maximum 20 feeds.')
        normalized=[];old={f['id']:f for f in config['feeds']}
        for f in feeds:
            name=str(f.get('name','')).strip();url=str(f.get('url','')).strip();category=f.get('category')
            if not 1<=len(name)<=60 or len(url)>2000 or category not in CATEGORIES: raise ValueError('Provide a feed name, HTTPS URL, and category.')
            fid=str(f.get('id') or secrets.token_hex(8))
            if fid not in old or old[fid]['url']!=url: public_url(url)
            if name=='Benzinga' or any(x['name']==name or x['id']==fid or x['url']==url for x in normalized): raise ValueError('Feed names and URLs must be unique.')
            normalized.append(dict(id=fid,name=name,url=url,category=category,enabled=bool(f.get('enabled',True))))
        result['feeds']=normalized
    if 'watchlist' in body:
        rows=body['watchlist']
        if not isinstance(rows,list) or len(rows)>30: raise ValueError('Maximum 30 watchlist symbols.')
        normalized=[]
        for w in rows:
            symbol=str(w.get('symbol','')).strip().upper();name=str(w.get('name','')).strip();aliases=str(w.get('aliases','')).strip()
            if not re.fullmatch(r'[A-Z0-9_]{1,20}:[A-Z0-9._/-]{1,25}',symbol) or not 1<=len(name)<=60 or len(aliases)>250: raise ValueError('Use EXCHANGE:TICKER, a name, and up to 250 characters of aliases.')
            if any(x['symbol']==symbol for x in normalized): raise ValueError('That symbol is already on the watchlist.')
            normalized.append(dict(symbol=symbol,name=name,aliases=aliases))
        result['watchlist']=normalized
    if 'rules' in body:
        rows=body['rules']
        if not isinstance(rows,list) or len(rows)>20: raise ValueError('Maximum 20 alert rules.')
        normalized=[];old={r['id']:r for r in config['rules']}
        for r in rows:
            rid=str(r.get('id') or secrets.token_hex(8));name=str(r.get('name','')).strip();terms=str(r.get('terms','')).strip();kind=r.get('kind')
            if not 1<=len(name)<=60 or kind not in ('keyword','watchlist','brief') or len(terms)>250 or (kind=='keyword' and not any(t.strip() for t in terms.split(','))): raise ValueError('Enter an alert name and keywords, or select Watchlist.')
            if kind=='brief' and terms not in brief_engine.SIGNAL_IDS: raise ValueError('Choose a market condition for this brief alert.')
            if any(x['id']==rid for x in normalized): raise ValueError('Duplicate rule.')
            previous=old.get(rid,{});unchanged=all(previous.get(k)==v for k,v in [('terms',terms),('kind',kind)]) and previous.get('enabled')
            normalized.append(dict(id=rid,name=name,terms=terms,kind=kind,enabled=bool(r.get('enabled',True)),created=previous['created'] if unchanged else time.time()))
        result['rules']=normalized
    if 'benzinga_key' in body:
        key=body['benzinga_key']
        if not isinstance(key,str) or len(key)>500 or any(c.isspace() for c in key): raise ValueError('Invalid API key format.')
        result['benzinga_key']=key
    result['revision']+=1
    return result

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(ROOT/'static'),**kwargs)
    def request_host(self):
        try: return (urllib.parse.urlparse('http://'+self.headers.get('Host','')).hostname or '').lower()
        except ValueError: return ''
    def local_request(self):
        # Requests relayed by the Cloudflare Tunnel carry CF headers that clients cannot strip,
        # so they are never treated as local even if the Host header claims localhost.
        if self.headers.get('Cf-Ray') or self.headers.get('Cf-Connecting-Ip'): return False
        return self.request_host() in ('localhost','127.0.0.1','::1')
    def public_request(self): return self.request_host() in PUBLIC_HOSTS
    def owner_cookie(self):
        for part in self.headers.get('Cookie','').split(';'):
            name,_,value=part.strip().partition('=')
            if name==OWNER_COOKIE and owner_key and secrets.compare_digest(value.encode(),owner_key.encode()): return True
        return False
    def is_owner(self): return self.local_request() or (self.public_request() and self.owner_cookie())
    def json_body(self,limit=50000):
        length=int(self.headers.get('Content-Length','0'))
        if not 0<length<=limit: raise ValueError('Request too large or empty.')
        body=json.loads(self.rfile.read(length))
        if not isinstance(body,dict): raise ValueError('Expected a JSON object.')
        return body
    def private_reply(self,obj,status=200,cookie=None):
        payload=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','private, no-store')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def serve_page(self,name):
        try: payload=(ROOT/'static'/name).read_bytes()
        except OSError: return self.reply({'error':'Not found.'},404)
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',CSP);self.send_header('Permissions-Policy','camera=(), microphone=(), geolocation=()')
        if self.public_request(): self.send_header('Strict-Transport-Security','max-age=31536000')
        if getattr(self,'static_cache',None): self.send_header('Cache-Control',self.static_cache)
        super().end_headers()
    def client_ip(self): return (self.headers.get('Cf-Connecting-Ip') or self.client_address[0] or '').strip()
    def throttled(self):
        """429 for visitors who call the expensive endpoints too fast. The owner is never throttled."""
        if self.is_owner(): return False
        wait=rate_allow(self.client_ip())
        if not wait: return False
        payload=json.dumps({'error':'Too many requests. Wait a few seconds and try again.'}).encode()
        self.send_response(429);self.send_header('Retry-After',str(wait));self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        return True
    def reply(self,obj,status=200):
        payload=json.dumps(obj).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def do_POST(self):
        path=urllib.parse.urlparse(self.path).path
        if path=='/api/owner/login' and (self.public_request() or self.local_request()):
            try: key=str(self.json_body(500).get('key',''))
            except (ValueError,TypeError): key=''
            if owner_key and secrets.compare_digest(key.encode(),owner_key.encode()):
                return self.private_reply({'ok':True},cookie=f'{OWNER_COOKIE}={owner_key}; Path=/; Max-Age=31536000; HttpOnly; Secure; SameSite=Lax')
            time.sleep(1);return self.private_reply({'error':'That owner key is not valid.'},403)
        if path=='/api/owner/logout':
            return self.private_reply({'ok':True},cookie=f'{OWNER_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Lax')
        if self.public_request() and not self.owner_cookie(): return self.reply({'error':'This is a read-only public view. Run your own copy to change settings.'},403)
        if not self.is_owner() or not secrets.compare_digest(self.headers.get('X-Signal-Token',''),csrf): return self.reply({'error':'Reload Signal before saving settings.'},403)
        if self.headers.get('Content-Type','').split(';')[0]!='application/json': return self.reply({'error':'JSON required.'},415)
        if path.startswith('/api/owner/schwab/'):
            if not self.local_request(): return self.reply({'error':'Schwab setup is only available on the host PC.'},403)
            try:
                body=self.json_body()
                if path=='/api/owner/schwab/keys': schwab.set_keys(body.get('app_key'),body.get('app_secret'),body.get('callback'))
                elif path=='/api/owner/schwab/connect': schwab.connect(body.get('url'))
                elif path=='/api/owner/schwab/fred':
                    key=str(body.get('key','')).strip().lower()
                    if not re.fullmatch(r'[a-z0-9]{32}',key): raise ValueError('A FRED API key is 32 letters and digits.')
                    target=DATA/'fred_api_key';target.write_text(key);os.chmod(target,0o600);economy_wake.set()
                else: return self.reply({'error':'Not found.'},404)
                return self.private_reply({'schwab':schwab.status()})
            except (ValueError,TypeError) as exc: return self.private_reply({'error':str(exc)[:240]},400)
            except (urllib.error.URLError,TimeoutError,OSError): return self.private_reply({'error':'Could not reach Schwab. Try again.'},503)
        if self.path=='/api/briefing/refresh' and self.throttled(): return
        if self.path=='/api/briefing/refresh':
            with condition:
                old_updated=state.get('updated');old_ids={a['id'] for a in state['articles']}
                old_market=tuple(sorted((key,value.get('value'),value.get('market_time')) for key,value in brief_market_cache['value'].items() if key!='_coverage' and isinstance(value,dict)))
            wake.set()
            with condition: poll_completed=condition.wait_for(lambda:state.get('updated')!=old_updated,timeout=18)
            brief=build_briefing(force=True)
            with condition:
                new_ids={a['id'] for a in state['articles']}
                new_market=tuple(sorted((key,value.get('value'),value.get('market_time')) for key,value in brief_market_cache['value'].items() if key!='_coverage' and isinstance(value,dict)))
                sources_online=sum(1 for source in state['sources'] if source.get('ok'));sources_total=len(state['sources']);checked_at=state.get('updated')
                refresh={'checked_at':checked_at,'poll_completed':poll_completed,'new_stories':len(new_ids-old_ids),'expired_stories':len(old_ids-new_ids),'market_changed':new_market!=old_market,'sources_online':sources_online,'sources_total':sources_total}
                brief_refresh.clear();brief_refresh.update(refresh)
            brief['refresh']=refresh
            return self.reply(brief)
        if self.path!='/api/settings': return self.reply({'error':'Not found.'},404)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=50000: return self.reply({'error':'Request too large or empty.'},413)
            body=json.loads(self.rfile.read(length))
            if not isinstance(body,dict): raise ValueError('Expected settings object.')
            with condition:
                if body.get('revision')!=config['revision']: return self.reply({'error':'Settings changed in another tab. Reload settings and try again.'},409)
                updated=validated_settings(body)
                atomic('settings.json',updated)
                key_changed=updated['benzinga_key']!=config['benzinga_key']
                config.clear();config.update(updated)
                broadcast()
                response=public_settings()
            if 'feeds' in body: wake.set()
            if key_changed: stream_wake.set()
            self.reply(response)
        except (ValueError,TypeError,KeyError,AttributeError) as exc: self.reply({'error':str(exc)[:200]},400)
        except OSError: self.reply({'error':'Settings could not be saved to disk.'},500)
    def do_GET(self):
        public=not self.local_request() and self.public_request()
        if not self.local_request() and not public: return self.reply({'error':'Local access only.'},403)
        path=urllib.parse.urlparse(self.path).path
        owner=self.is_owner()
        if path in HEAVY_PATHS and self.throttled(): return
        if path in ('/','/index.html'):
            payload=versioned_index().encode();self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
            self.send_header('Cache-Control','no-cache');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
        if path=='/api/settings':
            with condition: self.reply(public_settings()|({'csrf':csrf,'owner':True} if owner else {'read_only':True}))
        elif path=='/owner':
            if not self.local_request(): return self.reply({'error':'Not found.'},404)
            self.serve_page('owner.html')
        elif path=='/login': self.serve_page('login.html')
        elif path=='/api/owner/status':
            if not owner: return self.private_reply({'owner':False})
            st=schwab.status();self.private_reply({'owner':True,'local':self.local_request(),'schwab':{k:st[k] for k in ('configured','connected','refresh_expires')}})
        elif path=='/api/owner/setup':
            if not self.local_request(): return self.reply({'error':'Not found.'},404)
            hosts=sorted(PUBLIC_HOSTS)
            self.private_reply({'owner_link':(f'https://{hosts[0]}/login#key={owner_key}' if hosts else None),'owner_key':owner_key,'schwab':schwab.status(),'csrf':csrf,
                                'fred':{'configured':bool(fred_key()),'from_env':bool(os.getenv('FRED_API_KEY','').strip()),'series':len(state['economy']),'error':state.get('economy_error')}})
        elif path=='/api/owner/schwab/authorize':
            if not self.local_request(): return self.reply({'error':'Not found.'},404)
            try: self.private_reply({'url':schwab.authorize_url()})
            except ValueError as exc: self.private_reply({'error':str(exc)},400)
        elif path in ('/api/des','/api/fa','/api/most','/api/omon','/api/ns'):
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query);arg=lambda k,d='':(query.get(k) or [d])[0]
            try:
                if path=='/api/des': self.reply(mf.describe(clean_symbol(arg('symbol'))))
                elif path=='/api/fa': self.reply(fa_report(clean_symbol(arg('symbol')),arg('periods','annual')))
                elif path=='/api/most': self.reply(mf.most(arg('kind','ACTIVE')))
                elif path=='/api/omon': self.reply(mf.option_chain(clean_symbol(arg('symbol')),arg('expiry') or None))
                else: self.reply(news_search(arg('q')[:200]))
            except ValueError as exc: self.reply({'error':str(exc)[:200]},400)
            except LookupError as exc: self.reply({'error':str(exc)[:200]},404)
            except urllib.error.HTTPError as exc: self.reply({'error':f'The data source returned HTTP {exc.code}. Try again shortly.'},502)
            except (urllib.error.URLError,TimeoutError,OSError,KeyError,TypeError,json.JSONDecodeError): self.reply({'error':'The data source could not be reached. Try again shortly.'},503)
        elif path=='/api/live/quotes':
            # Owner only: Schwab data is licensed for the account holder's personal use.
            if not owner: return self.private_reply({'error':'Live quotes are only available to the owner.'},403)
            codes=[c for c in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('codes',[''])[0].split(',') if c][:80]
            try: self.private_reply({'source':'Schwab','quotes':schwab.live_quotes(codes),'at':int(time.time())})
            except LookupError as exc: self.private_reply({'error':str(exc),'quotes':{}},503)
            except (urllib.error.URLError,TimeoutError,OSError,ValueError): self.private_reply({'error':'Schwab could not be reached.','quotes':{}},503)
        elif path=='/api/briefing': self.reply(build_briefing())
        elif path=='/api/markets':
            try:
                snap=market_snapshot()
                # Same session logic as the brief: weekday clock plus whether today's S&P bar exists (catches holidays).
                snap['session']=brief_engine.session_state(datetime.datetime.now(brief_engine.NY),{i['code']:i for i in snap['items']})
                self.reply(snap)
            except LookupError as exc: self.reply({'error':str(exc)},503)
        elif path=='/api/company':
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('symbol',[''])[0]
            try: self.reply(company_research(query))
            except LookupError as exc: self.reply({'error':str(exc)},404)
            except ValueError as exc: self.reply({'error':str(exc)[:200]},400)
            except urllib.error.HTTPError as exc:
                self.reply({'error':'SEC EDGAR is temporarily unavailable or rejected the request.'},503 if exc.code>=500 else 502)
            except (urllib.error.URLError,TimeoutError,json.JSONDecodeError,KeyError,TypeError):
                self.reply({'error':'SEC EDGAR could not be reached. Try again shortly.'},503)
        elif path=='/api/reader':
            article_id_value=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('id',[''])[0]
            with condition: article=next((copy.deepcopy(a) for a in state['articles'] if a['id']==article_id_value),None) or copy.deepcopy(research_articles.get(article_id_value))
            if not article: return self.reply({'error':'That headline is no longer in the local feed cache.'},404)
            try: self.reply(article_reader(article))
            except ValueError as exc: self.reply({'error':str(exc)[:220]},422)
            except urllib.error.HTTPError as exc: self.reply({'error':f'The publisher returned HTTP {exc.code}. Open the original article instead.'},422)
            except (urllib.error.URLError,TimeoutError,LookupError,UnicodeError): self.reply({'error':'The publisher page could not be loaded. Open the original article instead.'},503)
        elif path=='/api/economy':
            with condition: self.reply({'economy':copy.deepcopy(state['economy']),'updated':state['economy_updated']})
        elif path=='/health': self.reply(health())
        elif path=='/api/news':
            with condition: self.reply(state)
        elif path=='/api/events':
            ip=self.client_ip()
            if not owner and not sse_acquire(ip): return self.reply({'error':'Too many live connections. Close other Signal tabs and reload.'},429)
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Cache-Control','no-cache');self.end_headers()
            version=-1
            try:
                while True:
                    with condition:
                        if version==state['version']: condition.wait(timeout=15)
                        payload=json.dumps(event_payload(version)) if version!=state['version'] else None;version=state['version']
                    self.wfile.write(('data: '+payload+'\n\n' if payload else ': heartbeat\n\n').encode());self.wfile.flush()
            except (BrokenPipeError,ConnectionResetError,TimeoutError,OSError): pass
            finally:
                if not owner: sse_release(ip)
        elif path.startswith('/api/'): self.reply({'error':'Not found.'},404)
        else:
            # Versioned assets (?v=<hash>) never change, so browsers and Cloudflare may keep them.
            query=urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get('v',[''])[0]
            name=path.lstrip('/')
            self.static_cache='public, max-age=31536000, immutable' if query and '/' not in name and query==asset_version(name) else 'no-cache'
            super().do_GET()

if __name__=='__main__':
    load()
    threading.Thread(target=poll,daemon=True).start()
    threading.Thread(target=benzinga_loop,daemon=True).start()
    threading.Thread(target=rates_loop,daemon=True).start()
    threading.Thread(target=economy_loop,daemon=True).start()
    print('Signal terminal listening on http://localhost:8080',flush=True)
    ThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()
