"""Rate limiting, live-connection caps and request coalescing."""
import threading, time
from conftest import call, PUBLIC
import server
import market_functions as mf


def test_rate_limit_kicks_in(base, monkeypatch):
    monkeypatch.setattr(mf, 'most', lambda kind='ACTIVE': {'kind': kind, 'rows': [], 'at': 0, 'source': 'test'})
    server.rate_buckets.clear()
    codes = [call(base, '/api/most', PUBLIC, headers={'Cf-Connecting-Ip': '203.0.113.9'})[0] for _ in range(server.RATE_CAPACITY + 5)]
    assert codes.count(200) == server.RATE_CAPACITY and 429 in codes
    # A different visitor is unaffected.
    assert call(base, '/api/most', PUBLIC, headers={'Cf-Connecting-Ip': '203.0.113.10'})[0] == 200


def test_owner_is_never_throttled(base, monkeypatch):
    monkeypatch.setattr(mf, 'most', lambda kind='ACTIVE': {'kind': kind, 'rows': [], 'at': 0, 'source': 'test'})
    server.rate_buckets.clear()
    codes = {call(base, '/api/most', 'localhost')[0] for _ in range(server.RATE_CAPACITY + 5)}
    assert codes == {200}


def test_sse_cap_per_visitor():
    server.sse_clients.clear()
    ip = '198.51.100.7'
    assert all(server.sse_acquire(ip) for _ in range(server.SSE_MAX_PER_IP))
    assert not server.sse_acquire(ip)
    server.sse_release(ip)
    assert server.sse_acquire(ip)
    server.sse_clients.clear()


def test_requests_for_same_key_coalesce():
    calls = []
    def slow(): calls.append(1); time.sleep(0.2); return 1
    threads = [threading.Thread(target=lambda: mf._cached(('test', 'coalesce'), 60, slow)) for _ in range(15)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert len(calls) == 1


def test_failures_are_cached_briefly():
    calls = []
    def broken(): calls.append(1); raise LookupError('down')
    for _ in range(4):
        try: mf._cached(('test', 'fail'), 60, broken)
        except LookupError: pass
    assert len(calls) == 1
