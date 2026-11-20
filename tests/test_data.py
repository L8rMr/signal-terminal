"""Data transformations: financial statements, symbols, asset versions, health."""
import re
from conftest import call, PUBLIC
import market_functions as mf
import schwab
import server


def flow(start, end, val, form='10-Q', fp=None):
    return {'start': start, 'end': end, 'val': val, 'form': form, 'filed': end, 'fp': fp}


def test_quarters_derived_from_year_to_date_cash_flow():
    rows = [flow('2024-09-29', '2024-12-28', 30), flow('2024-09-29', '2025-03-29', 50),
            flow('2024-09-29', '2025-06-28', 80), flow('2024-09-29', '2025-09-27', 111, '10-K', 'FY')]
    s = mf._series({'X': {'units': {'USD': rows}}}, ['X'], 'flow')
    assert s['annual'] == {'2025-09-27': 111}
    assert s['quarter'] == {'2024-12-28': 30, '2025-03-29': 20, '2025-06-28': 30, '2025-09-27': 31}


def test_financials_margins_and_growth():
    def yr(y, rev, ni):
        return {'Revenues': flow(f'{y-1}-10-01', f'{y}-09-30', rev, '10-K', 'FY'), 'NetIncomeLoss': flow(f'{y-1}-10-01', f'{y}-09-30', ni, '10-K', 'FY')}
    rows = {'Revenues': [], 'NetIncomeLoss': []}
    for y, rev, ni in ((2023, 100, 10), (2024, 120, 18)):
        for k, v in yr(y, rev, ni).items(): rows[k].append(v)
    facts = {'facts': {'us-gaap': {k: {'units': {'USD': v}} for k, v in rows.items()}}}
    report = mf.financials(facts, 'annual')
    lines = {l['label']: l['values'] for l in report['lines']}
    assert lines['Revenue'] == [100, 120]
    assert lines['Net margin'] == [0.1, 0.15]
    assert round(lines['Revenue growth (YoY)'][1], 2) == 0.2


def test_symbol_validation():
    assert server.clean_symbol(' brk/b ') == 'BRK-B'
    for bad in ('', '../x', '<script>', 'A' * 20):
        try: server.clean_symbol(bad); assert False, bad
        except ValueError: pass


def test_schwab_symbol_map():
    assert schwab.to_schwab('SPX') == '$SPX' and schwab.to_schwab('WTI') == '/CL'
    assert schwab.to_schwab('BRK-B') == 'BRK.B' and schwab.to_schwab('US10Y') is None


def test_index_assets_are_content_hashed(base):
    html = call(base, '/', PUBLIC)[2].decode()
    versions = re.findall(r'/(app\.js|workspace\.js|strip\.js|ux\.css)\?v=([0-9a-f]{10})', html)
    assert len(versions) >= 4
    name, v = versions[0]
    assert call(base, f'/{name}?v={v}', PUBLIC)[1].get('Cache-Control', '').startswith('public, max-age=31536000')
    assert call(base, f'/{name}?v=stale', PUBLIC)[1].get('Cache-Control') == 'no-cache'


def test_health_reports_problems(base):
    import json
    body = json.loads(call(base, '/health', PUBLIC)[2])
    assert body['ok'] is True and body['status'] in ('ok', 'degraded')
    assert 'rates unavailable' in body['problems']  # nothing has been fetched in tests
