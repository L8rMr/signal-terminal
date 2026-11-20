"""Owner-only live quotes from the Schwab Trader API (individual / personal use).

Schwab's account agreements license market data for the account holder's
personal use only, so nothing in this module is ever shown to public visitors:
server.py only calls live_quotes() for an owner request (localhost or a valid
owner cookie), keeps its own cache that no shared view reads, and marks every
response private / no-store.

Credentials and tokens live in DATA_DIR/schwab.json inside the container's
data volume, never in the repo. Schwab access tokens last 30 minutes and are
refreshed automatically; the refresh token lasts 7 days, after which the owner
reconnects from the setup page (http://localhost:<port>/owner on the host PC).
"""
import base64, json, os, threading, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

API = 'https://api.schwabapi.com'
REFRESH_LIFETIME = 7 * 24 * 3600
_lock = threading.Lock()
_cache = {}  # owner-only quote cache: schwab symbol -> (expires, quote)

# Terminal codes that map to a Schwab symbol. Anything not listed here is treated
# as a stock/ETF ticker. Yields, FX, crypto and foreign indices are not mapped and
# stay on the delayed feed.
SYMBOL_MAP = {
    'SPX': '$SPX', 'NDX': '$NDX', 'DJI': '$DJI', 'RUT': '$RUT', 'VIX': '$VIX', 'COMPX': '$COMPX',
    'ES': '/ES', 'NQ': '/NQ', 'WTI': '/CL', 'BRENT': '/BZ', 'GOLD': '/GC', 'COPPER': '/HG', 'NATGAS': '/NG',
}
UNMAPPED = {'US10Y', 'US5Y', 'US30Y', 'US2Y', 'DXY', 'USDJPY', 'EURUSD', 'BTC', 'NKY', 'HSI', 'DAX', 'UKX'}


def _path(): return Path(os.getenv('DATA_DIR', '/data')) / 'schwab.json'


def _load():
    try: return json.loads(_path().read_text())
    except (OSError, ValueError): return {}


def _save(cfg):
    path = _path(); tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(cfg)); os.chmod(tmp, 0o600); tmp.replace(path)


def status():
    cfg = _load(); now = time.time()
    refresh_expires = cfg.get('refresh_issued', 0) + REFRESH_LIFETIME if cfg.get('refresh_token') else None
    return {
        'configured': bool(cfg.get('app_key') and cfg.get('app_secret')),
        'connected': bool(refresh_expires and refresh_expires > now),
        'refresh_expires': refresh_expires,
        'callback': cfg.get('callback') or 'https://127.0.0.1',
        'error': cfg.get('last_error'),
    }


def set_keys(app_key, app_secret, callback):
    app_key, app_secret = str(app_key or '').strip(), str(app_secret or '').strip()
    callback = str(callback or 'https://127.0.0.1').strip()
    if not (8 <= len(app_key) <= 200 and 8 <= len(app_secret) <= 200): raise ValueError('Enter the App Key and Secret from developer.schwab.com.')
    if not callback.startswith('https://'): raise ValueError('The callback URL must start with https://')
    with _lock:
        cfg = _load()
        if cfg.get('app_key') != app_key: cfg = {}  # new app: old tokens are useless
        cfg.update(app_key=app_key, app_secret=app_secret, callback=callback); _save(cfg)


def authorize_url():
    cfg = _load()
    if not cfg.get('app_key'): raise ValueError('Save the App Key and Secret first.')
    return API + '/v1/oauth/authorize?' + urllib.parse.urlencode({'client_id': cfg['app_key'], 'redirect_uri': cfg.get('callback') or 'https://127.0.0.1'})


def _token_request(cfg, form):
    basic = base64.b64encode(f"{cfg['app_key']}:{cfg['app_secret']}".encode()).decode()
    req = urllib.request.Request(API + '/v1/oauth/token', data=urllib.parse.urlencode(form).encode(), method='POST', headers={
        'Authorization': 'Basic ' + basic, 'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=15) as response: return json.load(response)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Schwab rejected the request (HTTP {exc.code}). If the code expired, sign in again and paste the new address within 30 seconds.') from None


def connect(redirected_url):
    """Exchange the ?code= from the page Schwab redirected to for tokens."""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(str(redirected_url or '').strip()).query)
    code = (query.get('code') or [''])[0]
    if not code: raise ValueError('Paste the full address of the page Schwab sent you to (it contains ?code=).')
    with _lock:
        cfg = _load()
        if not cfg.get('app_key'): raise ValueError('Save the App Key and Secret first.')
        tokens = _token_request(cfg, {'grant_type': 'authorization_code', 'code': code, 'redirect_uri': cfg.get('callback') or 'https://127.0.0.1'})
        now = time.time()
        cfg.update(access_token=tokens['access_token'], access_expires=now + int(tokens.get('expires_in', 1800)) - 60,
                   refresh_token=tokens['refresh_token'], refresh_issued=now, last_error=None)
        _save(cfg)


def _access_token():
    with _lock:
        cfg = _load(); now = time.time()
        if not cfg.get('refresh_token'): raise LookupError('Schwab is not connected.')
        if cfg.get('access_token') and cfg.get('access_expires', 0) > now: return cfg['access_token']
        if cfg.get('refresh_issued', 0) + REFRESH_LIFETIME < now: raise LookupError('Schwab sign-in expired. Reconnect from the setup page.')
        try: tokens = _token_request(cfg, {'grant_type': 'refresh_token', 'refresh_token': cfg['refresh_token']})
        except ValueError as exc:
            cfg['last_error'] = str(exc); _save(cfg); raise LookupError('Schwab refresh failed. Reconnect from the setup page.') from None
        cfg.update(access_token=tokens['access_token'], access_expires=now + int(tokens.get('expires_in', 1800)) - 60, last_error=None)
        if tokens.get('refresh_token') and tokens['refresh_token'] != cfg['refresh_token']:
            cfg.update(refresh_token=tokens['refresh_token'], refresh_issued=now)
        _save(cfg)
        return cfg['access_token']


def to_schwab(code):
    code = str(code or '').upper().strip()
    if code in SYMBOL_MAP: return SYMBOL_MAP[code]
    if code in UNMAPPED or not code or len(code) > 12: return None
    if not all(c.isalnum() or c in '.-' for c in code): return None
    return code.replace('-', '.')  # BRK-B -> BRK.B


def live_quotes(codes, ttl=1.5):
    """{code: {price, change_pct, previous, time}} for codes Schwab can quote."""
    wanted = {}
    for code in list(dict.fromkeys(codes))[:80]:
        sym = to_schwab(code)
        if sym: wanted[code] = sym
    now = time.time(); missing = sorted({s for s in wanted.values() if _cache.get(s, (0,))[0] <= now})
    if missing:
        token = _access_token()
        for start in range(0, len(missing), 50):
            batch = missing[start:start + 50]
            url = API + '/marketdata/v1/quotes?' + urllib.parse.urlencode({'symbols': ','.join(batch), 'fields': 'quote', 'indicative': 'false'})
            req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
            try:
                with urllib.request.urlopen(req, timeout=10) as response: payload = json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    with _lock:
                        cfg = _load(); cfg['access_expires'] = 0; _save(cfg)
                raise LookupError(f'Schwab quotes unavailable (HTTP {exc.code}).') from None
            for sym in batch:
                q = (payload.get(sym) or {}).get('quote') or {}
                price = q.get('lastPrice') or q.get('mark')
                prev = q.get('closePrice')
                if price is None: _cache[sym] = (now + ttl, None); continue
                pct = q.get('netPercentChange')
                if pct is None and prev: pct = (price / prev - 1) * 100
                _cache[sym] = (now + ttl, {'price': float(price), 'change_pct': round(float(pct), 2) if pct is not None else None,
                                           'previous': prev, 'time': int((q.get('tradeTime') or q.get('quoteTime') or 0) / 1000)})
        if len(_cache) > 500:
            for key in sorted(_cache, key=lambda k: _cache[k][0])[:200]: _cache.pop(key, None)
    return {code: _cache[sym][1] for code, sym in wanted.items() if _cache.get(sym, (0, None))[1]}
