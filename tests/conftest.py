"""Spin up the real request handler on a random port with a throwaway data folder.
No feed polling or market loops run, and no test touches the network."""
import os, sys, tempfile, threading, json, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
import pytest

DATA = tempfile.mkdtemp(prefix='signal-test-')
os.environ['DATA_DIR'] = DATA
os.environ['PUBLIC_HOSTS'] = 'signal.example.com'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server  # noqa: E402

server.load()
PUBLIC = 'signal.example.com'


@pytest.fixture(scope='session')
def base():
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()


def call(base, path, host=None, method='GET', body=None, headers=None):
    h = dict(headers or {})
    if host: h['Host'] = host
    data = None
    if body is not None:
        data = json.dumps(body).encode(); h.setdefault('Content-Type', 'application/json')
    req = urllib.request.Request(base + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read(); return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
