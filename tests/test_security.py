"""The public/owner boundary: visitors can read but never write, and never see Schwab data."""
import json
from conftest import call, PUBLIC
import server


def test_public_settings_are_read_only(base):
    status, _, raw = call(base, '/api/settings', PUBLIC)
    body = json.loads(raw)
    assert status == 200 and body['read_only'] is True and 'csrf' not in body


def test_public_cannot_write(base):
    status, _, raw = call(base, '/api/settings', PUBLIC, 'POST', {})
    assert status == 403


def test_public_never_gets_live_quotes(base):
    assert json.loads(call(base, '/api/owner/status', PUBLIC)[2]) == {'owner': False}
    assert call(base, '/api/live/quotes?codes=SPX', PUBLIC)[0] == 403


def test_owner_pages_hidden_from_public(base):
    for path in ('/owner', '/api/owner/setup', '/api/owner/schwab/authorize'):
        assert call(base, path, PUBLIC)[0] == 404


def test_cloudflare_relayed_request_is_never_local(base):
    # Claims to be localhost but carries a Cloudflare header: must not reach owner setup.
    status = call(base, '/api/owner/setup', 'localhost', headers={'Cf-Ray': 'abc123'})[0]
    assert status in (403, 404)


def test_localhost_is_owner(base):
    assert call(base, '/api/owner/setup', 'localhost')[0] == 200


def test_owner_cookie_unlocks_live_view(base):
    cookie = {'Cookie': f'{server.OWNER_COOKIE}={server.owner_key}'}
    status, _, raw = call(base, '/api/owner/status', PUBLIC, headers=cookie)
    assert status == 200 and json.loads(raw)['owner'] is True
    assert json.loads(call(base, '/api/settings', PUBLIC, headers=cookie)[2]).get('csrf')


def test_wrong_owner_key_rejected(base):
    status = call(base, '/api/owner/login', PUBLIC, 'POST', {'key': 'nope'})[0]
    assert status == 403


def test_unknown_host_refused(base):
    assert call(base, '/api/news', 'evil.example.net')[0] == 403


def test_security_headers(base):
    _, headers, _ = call(base, '/', PUBLIC)
    csp = headers.get('Content-Security-Policy', '')
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    assert headers.get('X-Frame-Options') == 'DENY'
    assert 'max-age' in headers.get('Strict-Transport-Security', '')


def test_bad_tickers_rejected(base):
    for path in ('/api/des?symbol=../../etc', '/api/fa?symbol=%3Cscript%3E', '/api/omon?symbol='):
        assert call(base, path, PUBLIC)[0] == 400
