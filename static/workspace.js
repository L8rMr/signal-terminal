// WS: a Godel/Bloomberg-style windowed workspace and command grammar.
//
//   AAPL DES      company description and key stats      (also: AAPL US Equity DES)
//   AAPL FA       standardized financials from SEC filings
//   AAPL G        chart (TradingView)
//   AAPL OMON     option monitor with greeks (Cboe, delayed)
//   MOST          most active / gainers / losers          (MOST GAINERS, MOST LOSERS)
//   NS fed cut    news search across feeds and Google News
//   WS            open the workspace;  HELP  list functions
//
// Every function opens as a window on the WS canvas. Windows drag by their title
// bar, resize from the corner, and the layout is saved in this browser.
(() => {
  const $ = s => document.querySelector(s);
  const escH = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const STORE = 'signal.ws.v1', HIST = 'signal.cmd.history.v1';
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };

  const FUNCS = {
    DES:  {name: 'Description', ticker: true, w: 560, h: 540, about: 'Company profile, valuation and key stats'},
    FA:   {name: 'Financial analysis', ticker: true, w: 760, h: 540, about: 'Income, balance sheet and cash flow from SEC filings'},
    G:    {name: 'Chart', ticker: true, w: 660, h: 440, about: 'Interactive price chart'},
    OMON: {name: 'Option monitor', ticker: true, w: 880, h: 560, about: 'Option chain with greeks, delayed ~15 min'},
    MOST: {name: 'Most active', ticker: false, w: 560, h: 540, about: 'Most active, top gainers and losers (MOST GAINERS / MOST LOSERS)'},
    NS:   {name: 'News search', ticker: false, w: 560, h: 520, about: 'Every headline matching your words (NS fed rate cut)'},
    HELP: {name: 'Functions', ticker: false, w: 520, h: 520, about: 'This list'},
  };
  const VIEW_CODES = {BRIEF: 'Daily market brief', ANLY: 'Brief analytics', TOP: 'Ranked news', EDGAR: 'Company research (EDGAR)', GMM: 'Global macro movers', BTMM: 'Rates & money markets', FRED: 'Economic data', MON: 'Watchlist', ALRT: 'Alerts', SET: 'Sources', WS: 'Workspace'};
  const NOISE = new Set(['US', 'EQUITY', 'EQUITIES', '<GO>', 'GO']);
  const TV_SYMBOLS = {SPX: 'SP:SPX', NDX: 'NASDAQ:NDX', DJI: 'TVC:DJI', RUT: 'TVC:RUT', VIX: 'TVC:VIX'};

  // ---------- formatting ----------
  const num = (v, d = 2) => v == null || isNaN(v) ? '—' : Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
  const big = v => {
    if (v == null || isNaN(v)) return '—';
    const a = Math.abs(v), s = v < 0 ? '−' : '';
    for (const [n, u] of [[1e12, 'T'], [1e9, 'B'], [1e6, 'M'], [1e3, 'K']]) if (a >= n) return s + (a / n).toFixed(a / n >= 100 ? 0 : 1) + u;
    return s + a.toFixed(0);
  };
  const pctv = (v, d = 2) => v == null || isNaN(v) ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(d)}%`;
  const tone = v => v == null ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '';
  const ago = t => { if (!t) return ''; const m = Math.round((Date.now() / 1000 - t) / 60); return m < 1 ? 'now' : m < 60 ? m + 'm' : m < 1440 ? Math.round(m / 60) + 'h' : Math.round(m / 1440) + 'd'; };
  async function api(url) {
    const r = await fetch(url, {cache: 'no-store'}); const v = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(v.error || `Request failed (${r.status})`); return v;
  }

  // ---------- command grammar ----------
  function parse(raw) {
    const words = String(raw || '').trim().split(/\s+/).filter(Boolean);
    if (!words.length) return null;
    const up = words.map(w => w.toUpperCase());
    if (up[0] === 'NS') return {fn: 'NS', arg: words.slice(1).join(' ') || 'stock market'};
    if (up[0] === 'MOST') return {fn: 'MOST', arg: ['GAINERS', 'LOSERS'].includes(up[1]) ? up[1] : 'ACTIVE'};
    if (up[0] === 'HELP' || up[0] === '?') return {fn: 'HELP'};
    if (up[0] === 'WS' && up.length === 1) return {fn: 'WS'};
    const core = up.filter(w => !NOISE.has(w));
    if (core.length === 2) {
      const [a, b] = core;
      if (FUNCS[b]?.ticker && !FUNCS[a]) return {fn: b, arg: a};
      if (FUNCS[a]?.ticker && !FUNCS[b]) return {fn: a, arg: b};
    }
    return null;
  }

  // ---------- window manager ----------
  let layout = store.get(STORE, null);
  let zTop = 10;
  const canvas = () => $('#ws');
  const save = () => store.set(STORE, layout);

  function defaultLayout() {
    return [
      {fn: 'MOST', arg: 'ACTIVE', x: 0, y: 0, w: 520, h: 500},
      {fn: 'G', arg: 'SPY', x: 530, y: 0, w: 640, h: 500},
      {fn: 'NS', arg: 'Federal Reserve', x: 0, y: 510, w: 520, h: 440},
      {fn: 'DES', arg: 'NVDA', x: 530, y: 510, w: 640, h: 440},
    ].map((w, i) => ({...w, id: 'w' + Date.now().toString(36) + i, z: i + 1}));
  }

  function open(fn, arg) {
    if (fn === 'WS') { showWorkspace(); return; }
    showWorkspace();
    const spec = FUNCS[fn];
    const existing = layout.find(w => w.fn === fn && String(w.arg || '') === String(arg || ''));
    if (existing) { focus(existing.id, true); return; }
    const n = layout.length, el = canvas();
    const maxX = Math.max(0, (el?.clientWidth || 1200) - spec.w);
    const win = {id: 'w' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5), fn, arg,
      x: Math.min(maxX, 30 + (n % 6) * 34), y: (el?.scrollTop || 0) + 20 + (n % 6) * 30, w: spec.w, h: spec.h, z: ++zTop};
    layout.push(win); save(); mount(win); focus(win.id, true);
  }

  function focus(id, flash) {
    const w = layout.find(x => x.id === id); const el = document.getElementById(id);
    if (!w || !el) return;
    w.z = ++zTop; el.style.zIndex = w.z; save();
    document.querySelectorAll('.ws-win.focused').forEach(x => x.classList.remove('focused')); el.classList.add('focused');
    if (flash) { el.classList.remove('ws-pop'); void el.offsetWidth; el.classList.add('ws-pop'); el.scrollIntoView({block: 'nearest', behavior: 'smooth'}); }
  }

  function close(id) {
    layout = layout.filter(w => w.id !== id); save();
    const el = document.getElementById(id); if (el) { clearInterval(el._timer); el.remove(); }
    emptyHint();
  }

  function title(w) {
    const spec = FUNCS[w.fn];
    if (spec.ticker) return `<b>${escH(w.arg)}</b> <i>US EQUITY</i> <span>${w.fn}</span> <em>${escH(spec.name)}</em>`;
    if (w.fn === 'NS') return `<span>NS</span> <em>“${escH(w.arg)}”</em>`;
    if (w.fn === 'MOST') return `<span>MOST</span> <em>${escH(w.arg === 'ACTIVE' ? 'Most active' : w.arg === 'GAINERS' ? 'Top gainers' : 'Top losers')}</em>`;
    return `<span>${w.fn}</span> <em>${escH(spec.name)}</em>`;
  }

  function mount(w) {
    const el = document.createElement('section');
    el.className = 'ws-win'; el.id = w.id; el.dataset.fn = w.fn;
    Object.assign(el.style, {left: w.x + 'px', top: w.y + 'px', width: w.w + 'px', height: w.h + 'px', zIndex: w.z || 1});
    el.innerHTML = `<header class="ws-head"><div class="ws-title">${title(w)}</div><div class="ws-tools"><button data-ws="refresh" title="Refresh" aria-label="Refresh">↻</button><button data-ws="close" title="Close" aria-label="Close window">×</button></div></header><div class="ws-body"><div class="ws-loading">LOADING…</div></div><div class="ws-grip" aria-hidden="true"></div>`;
    canvas().appendChild(el); emptyHint();
    el.addEventListener('pointerdown', () => focus(w.id));
    el.querySelector('[data-ws=close]').onclick = e => { e.stopPropagation(); close(w.id); };
    el.querySelector('[data-ws=refresh]').onclick = () => render(w, el, true);
    dragger(el.querySelector('.ws-head'), (dx, dy, start) => { w.x = Math.max(0, start.x + dx); w.y = Math.max(0, start.y + dy); el.style.left = w.x + 'px'; el.style.top = w.y + 'px'; }, () => ({x: w.x, y: w.y}));
    dragger(el.querySelector('.ws-grip'), (dx, dy, start) => { w.w = Math.max(320, start.w + dx); w.h = Math.max(220, start.h + dy); el.style.width = w.w + 'px'; el.style.height = w.h + 'px'; }, () => ({w: w.w, h: w.h}));
    render(w, el);
  }

  function dragger(handle, move, startState) {
    handle.addEventListener('pointerdown', e => {
      if (e.button !== 0 || e.target.closest('button,input,select,a') || window.matchMedia('(max-width: 820px)').matches) return;
      e.preventDefault(); handle.setPointerCapture(e.pointerId);
      const sx = e.clientX, sy = e.clientY, start = startState();
      const snap = v => Math.round(v / 10) * 10;
      const mv = ev => move(snap(ev.clientX - sx), snap(ev.clientY - sy), start);
      const up = () => { handle.removeEventListener('pointermove', mv); handle.removeEventListener('pointerup', up); save(); };
      handle.addEventListener('pointermove', mv); handle.addEventListener('pointerup', up);
    });
  }

  function emptyHint() {
    const c = canvas(); if (!c) return;
    let hint = c.querySelector('.ws-empty');
    if (!layout.length && !hint) { hint = document.createElement('div'); hint.className = 'ws-empty'; hint.innerHTML = 'Empty workspace. Type <kbd>AAPL DES</kbd>, <kbd>NVDA OMON</kbd>, <kbd>MOST</kbd> or <kbd>NS oil</kbd> in the command line, or <button data-ws-reset>restore the default layout</button>.'; c.appendChild(hint); }
    if (layout.length && hint) hint.remove();
  }

  // ---------- renderers ----------
  const actions = sym => `<span class="ws-acts">${['DES', 'FA', 'G', 'OMON'].map(f => `<button data-open-fn="${f}" data-arg="${escH(sym)}">${f}</button>`).join('')}</span>`;

  async function render(w, el, force) {
    const body = el.querySelector('.ws-body');
    clearInterval(el._timer);
    try {
      if (w.fn === 'G') return renderChart(w, body);
      if (w.fn === 'HELP') return renderHelp(body);
      if (force || !body.dataset.ready) body.innerHTML = '<div class="ws-loading">LOADING…</div>';
      if (w.fn === 'DES') renderDES(body, await api('/api/des?symbol=' + encodeURIComponent(w.arg)));
      if (w.fn === 'FA') renderFA(w, body, await api(`/api/fa?symbol=${encodeURIComponent(w.arg)}&periods=${w.periods || 'annual'}`));
      if (w.fn === 'MOST') { renderMOST(w, body, await api('/api/most?kind=' + w.arg)); el._timer = setInterval(() => render(w, el), 60000); }
      if (w.fn === 'NS') { renderNS(w, body, await api('/api/ns?q=' + encodeURIComponent(w.arg))); el._timer = setInterval(() => render(w, el), 90000); }
      if (w.fn === 'OMON') renderOMON(w, body, await api(`/api/omon?symbol=${encodeURIComponent(w.arg)}${w.expiry ? '&expiry=' + w.expiry : ''}`));
      body.dataset.ready = '1';
    } catch (err) {
      body.innerHTML = `<div class="ws-error"><b>${escH(w.fn)} UNAVAILABLE</b><p>${escH(err.message)}</p><button data-ws-retry>TRY AGAIN</button></div>`;
      body.querySelector('[data-ws-retry]').onclick = () => render(w, el, true);
    }
  }

  function renderChart(w, body) {
    const id = 'tv-' + w.id; body.innerHTML = `<div id="${id}" class="ws-chart"></div>`;
    if (typeof chart === 'function') chart(TV_SYMBOLS[w.arg] || w.arg, '#' + id);
  }

  function renderHelp(body) {
    const rows = Object.entries(FUNCS).map(([k, f]) => `<tr><td><b>${f.ticker ? '<i>TICKER</i> ' : ''}${k}</b></td><td>${escH(f.about)}</td></tr>`).join('');
    const views = Object.entries(VIEW_CODES).map(([k, v]) => `<tr><td><b>${k}</b></td><td>${escH(v)}</td></tr>`).join('');
    body.innerHTML = `<div class="ws-pad"><p class="ws-note">Type into the command line at the top. <kbd>↑</kbd> recalls history, <kbd>Tab</kbd> accepts a suggestion. Godel syntax works too: <kbd>AAPL US Equity DES</kbd>.</p><table class="ws-table help"><tbody>${rows}</tbody></table><h4>VIEWS</h4><table class="ws-table help"><tbody>${views}</tbody></table></div>`;
  }

  function renderDES(body, d) {
    const pctKeys = /margin|growth|yield|short/i;
    const stats = Object.entries(d.stats).filter(([, v]) => v != null).map(([k, v]) => {
      const val = pctKeys.test(k) ? pctv(v * 100, 1).replace('+', '') : /cap|value|revenue|cash|debt|shares|volume/i.test(k) ? big(v) : num(v);
      return `<div><span>${escH(k)}</span><b>${val}</b></div>`;
    }).join('');
    const a = d.analyst || {}, range = d.stats['52-week high'] && d.stats['52-week low'] ? Math.min(100, Math.max(0, (d.price - d.stats['52-week low']) / (d.stats['52-week high'] - d.stats['52-week low']) * 100)) : null;
    const target = a.target_mean && d.price ? (a.target_mean / d.price - 1) * 100 : null;
    body.innerHTML = `<div class="ws-pad des">
      <div class="des-top"><div><h3>${escH(d.name)}</h3><p class="muted">${escH([d.exchange, d.sector, d.industry].filter(Boolean).join(' · '))}</p></div>
        <div class="des-px"><b>${num(d.price)}</b><em class="${tone(d.change_pct)}">${pctv(d.change_pct)}</em><small>${escH(d.currency || '')} · ${escH(d.market_state || '')}</small></div></div>
      ${range != null ? `<div class="des-range" title="Position in 52-week range"><span>${num(d.stats['52-week low'])}</span><div><i style="left:${range}%"></i></div><span>${num(d.stats['52-week high'])}</span></div>` : ''}
      ${actions(d.symbol)}
      <div class="des-grid">${stats}</div>
      ${a.recommendation ? `<p class="des-line"><b>STREET</b> ${escH(String(a.recommendation).replace('_', ' ').toUpperCase())} · ${a.analysts || '?'} analysts · target ${num(a.target_mean)} <em class="${tone(target)}">(${pctv(target, 1)})</em>, range ${num(a.target_low)}–${num(a.target_high)}</p>` : ''}
      ${d.next_earnings ? `<p class="des-line"><b>EARNINGS</b> ${new Date(d.next_earnings * 1000).toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: 'numeric'})}</p>` : ''}
      ${d.summary ? `<details class="des-sum"><summary>BUSINESS DESCRIPTION</summary><p>${escH(d.summary)}</p></details>` : ''}
      ${d.officers?.length ? `<p class="des-line"><b>MANAGEMENT</b> ${d.officers.map(o => `${escH(o.name)} <span class="muted">${escH(o.title || '')}</span>`).join(' · ')}</p>` : ''}
      <p class="ws-src">${escH(d.source)}${d.website ? ` · <a href="${escH(d.website)}" target="_blank" rel="noopener">${escH(d.website.replace(/^https?:\/\//, ''))}</a>` : ''}</p></div>`;
  }

  function sparkRow(values) {
    const v = values.filter(x => x != null); if (v.length < 2) return '';
    const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
    const pts = values.map((x, i) => x == null ? null : `${(i / (values.length - 1)) * 60},${16 - ((x - lo) / span) * 14}`).filter(Boolean).join(' ');
    const up = v[v.length - 1] >= v[0];
    return `<svg viewBox="0 0 60 18" class="fa-spark ${up ? 'up' : 'down'}"><polyline points="${pts}"/></svg>`;
  }

  function renderFA(w, body, d) {
    const head = d.columns.map(c => { const dt = new Date(c + 'T12:00:00'); return `<th>${d.periods === 'annual' ? 'FY' : 'Q'} ${dt.toLocaleDateString('en-US', {month: 'short', year: '2-digit'})}</th>`; }).join('');
    let section = '', rows = '';
    for (const l of d.lines) {
      if (l.section !== section) { section = l.section; rows += `<tr class="fa-sec"><td colspan="${d.columns.length + 2}">${escH(section.toUpperCase())}</td></tr>`; }
      const cell = v => v == null ? '—' : l.kind === 'pct' ? (v * 100).toFixed(1) + '%' : l.kind === 'eps' ? num(v) : (v < 0 ? `(${big(-v)})` : big(v));
      rows += `<tr><td>${escH(l.label)}</td>${l.values.map(v => `<td class="${v < 0 ? 'neg' : ''}">${cell(v)}</td>`).join('')}<td>${sparkRow(l.values)}</td></tr>`;
    }
    body.innerHTML = `<div class="ws-pad"><div class="ws-row"><div class="ws-seg"><button data-p="annual" class="${d.periods === 'annual' ? 'on' : ''}">ANNUAL</button><button data-p="quarter" class="${d.periods === 'quarter' ? 'on' : ''}">QUARTERLY</button></div><span class="muted">${escH(d.name)} · USD</span>${actions(d.symbol)}</div>
      <div class="ws-scroll"><table class="ws-table fa"><thead><tr><th></th>${head}<th>TREND</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="ws-src">${escH(d.source)} · fiscal Q4 derived as full year minus Q1–Q3 · negatives in parentheses</p></div>`;
    body.querySelectorAll('[data-p]').forEach(b => b.onclick = () => { w.periods = b.dataset.p; save(); render(w, b.closest('.ws-win'), true); });
  }

  function renderMOST(w, body, d) {
    const maxR = Math.max(1, ...d.rows.map(r => r.rvol || 0));
    const rows = d.rows.map(r => `<tr data-sym="${escH(r.symbol)}"><td><b>${escH(r.symbol)}</b><small>${escH(r.name)}</small></td><td>${num(r.price)}</td><td class="${tone(r.change_pct)}">${pctv(r.change_pct)}</td><td>${big(r.volume)}</td><td><div class="rvol"><i style="width:${Math.min(100, (r.rvol || 0) / maxR * 100)}%"></i><span>${r.rvol != null ? r.rvol.toFixed(1) + '×' : '—'}</span></div></td><td>${big(r.market_cap)}</td></tr>`).join('');
    body.innerHTML = `<div class="ws-pad"><div class="ws-row"><div class="ws-seg">${['ACTIVE', 'GAINERS', 'LOSERS'].map(k => `<button data-k="${k}" class="${w.arg === k ? 'on' : ''}">${k}</button>`).join('')}</div><span class="muted">updated ${new Date(d.at * 1000).toLocaleTimeString('en-US', {hour: 'numeric', minute: '2-digit'})} · click a row</span></div>
      <div class="ws-scroll"><table class="ws-table most"><thead><tr><th>SYMBOL</th><th>LAST</th><th>CHG</th><th>VOLUME</th><th>RVOL</th><th>MKT CAP</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="muted">No data right now.</td></tr>'}</tbody></table></div>
      <p class="ws-src">${escH(d.source)} · RVOL = today's volume ÷ 3-month average</p></div>`;
    body.querySelectorAll('[data-k]').forEach(b => b.onclick = () => {
      w.arg = b.dataset.k; save(); const el = b.closest('.ws-win'); el.querySelector('.ws-title').innerHTML = title(w); render(w, el, true);
    });
    body.querySelectorAll('tr[data-sym]').forEach(tr => tr.onclick = e => open(e.shiftKey ? 'G' : 'DES', tr.dataset.sym));
  }

  function renderNS(w, body, d) {
    const rows = d.results.map(a => `<li><time>${ago(a.published)}</time><div><a href="${escH(a.url)}" target="_blank" rel="noopener">${escH(a.title)}</a><small>${escH(a.source)}${a.delivery === 'SEARCH' ? '' : ' · <b>FEED</b>'}</small></div><button data-read="${escH(a.id)}" title="Open in reader">READ</button></li>`).join('');
    body.innerHTML = `<div class="ws-pad"><form class="ws-row ns-form"><input value="${escH(w.arg)}" aria-label="Search words"><button>SEARCH</button></form>
      <p class="muted">${d.results.length} stories · ${d.local} from Signal's own feeds · past 7 days · refreshes every 90s</p>
      <ol class="ns-list">${rows || '<li class="muted">Nothing matched. Try fewer words.</li>'}</ol></div>`;
    body.querySelector('form').onsubmit = e => {
      e.preventDefault(); const v = e.target.querySelector('input').value.trim(); if (!v) return;
      w.arg = v; save(); const el = e.target.closest('.ws-win'); el.querySelector('.ws-title').innerHTML = title(w); render(w, el, true);
    };
    body.querySelectorAll('[data-read]').forEach(b => b.onclick = () => typeof openReader === 'function' && openReader(b.dataset.read));
  }

  function renderOMON(w, body, d) {
    const cols = ['bid', 'ask', 'last_trade_price', 'iv', 'delta', 'open_interest', 'volume'];
    const labels = ['BID', 'ASK', 'LAST', 'IV', 'Δ', 'OI', 'VOL'];
    const fmt = (k, v) => v == null ? '—' : k === 'iv' ? (v * 100).toFixed(1) : k === 'delta' ? v.toFixed(2) : /open|volume/.test(k) ? big(v) : num(v);
    const spot = d.underlying, atm = spot ? d.rows.reduce((b, r) => Math.abs(r.strike - spot) < Math.abs(b - spot) ? r.strike : b, d.rows[0]?.strike) : null;
    const rows = d.rows.map(r => {
      const c = r.C || {}, p = r.P || {};
      return `<tr class="${r.strike === atm ? 'atm' : ''}">${cols.map(k => `<td class="${spot && r.strike < spot ? 'itm' : ''}">${fmt(k, c[k])}</td>`).join('')}<td class="strike">${num(r.strike)}</td>${cols.map(k => `<td class="${spot && r.strike > spot ? 'itm' : ''}">${fmt(k, p[k])}</td>`).join('')}</tr>`;
    }).join('');
    const opts = d.expiries.map(e => { const dt = new Date(e + 'T12:00:00'); const dte = Math.round((dt - Date.now()) / 864e5); return `<option value="${e}" ${e === d.expiry ? 'selected' : ''}>${dt.toLocaleDateString('en-US', {month: 'short', day: 'numeric', year: '2-digit'})} · ${Math.max(0, dte)}d</option>`; }).join('');
    body.innerHTML = `<div class="ws-pad"><div class="ws-row"><label class="muted">EXPIRY <select>${opts}</select></label><span><b>${escH(d.symbol)}</b> ${num(spot)} <em class="${tone(d.change_pct)}">${pctv(d.change_pct)}</em></span>${actions(d.symbol)}</div>
      <div class="ws-scroll"><table class="ws-table omon"><thead><tr><th colspan="7" class="side">CALLS</th><th></th><th colspan="7" class="side">PUTS</th></tr><tr>${labels.map(l => `<th>${l}</th>`).join('')}<th>STRIKE</th>${labels.map(l => `<th>${l}</th>`).join('')}</tr></thead><tbody>${rows}</tbody></table></div>
      <p class="ws-src">${escH(d.source)} · as of ${escH(d.as_of || '')} · 40 strikes nearest the money · shaded = in the money</p></div>`;
    body.querySelector('select').onchange = e => { w.expiry = e.target.value; save(); render(w, e.target.closest('.ws-win'), true); };
    const atmRow = body.querySelector('tr.atm'); if (atmRow) atmRow.scrollIntoView({block: 'center'});
  }

  // ---------- view + chrome ----------
  function showWorkspace() {
    if (typeof openView === 'function') openView('ws');
    const name = $('#workspace-name'); if (name) name.textContent = '11 / WORKSPACE';
  }

  function buildCanvas() {
    if (!layout) { layout = defaultLayout(); save(); }
    zTop = Math.max(10, ...layout.map(w => w.z || 1));
    canvas().innerHTML = '';
    layout.forEach(mount);
    emptyHint();
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-open-fn]');
    if (b) { e.preventDefault(); open(b.dataset.openFn, b.dataset.arg); }
    if (e.target.closest('[data-ws-reset]')) { [...document.querySelectorAll('.ws-win')].forEach(x => { clearInterval(x._timer); x.remove(); }); layout = defaultLayout(); save(); buildCanvas(); }
  });

  // Route Godel-style commands to windows; everything else falls through to the
  // terminal's original command handler.
  const originalExecute = typeof executeCommand === 'function' ? executeCommand : null;
  const history = store.get(HIST, []);
  window.executeCommand = executeCommand = function (raw) {
    const text = String(raw || '').trim(); if (!text) return;
    if (history[history.length - 1] !== text) { history.push(text); if (history.length > 50) history.shift(); store.set(HIST, history); }
    const cmd = parse(text);
    if (cmd) return open(cmd.fn, cmd.arg);
    if (originalExecute) originalExecute(raw);
  };

  // ---------- autocomplete + history ----------
  const input = $('#command');
  const box = document.createElement('div'); box.className = 'cmd-suggest'; box.setAttribute('role', 'listbox'); box.hidden = true;
  input?.closest('.commandbar')?.after(box);
  let items = [], sel = -1, hpos = null;

  function suggestions(text) {
    const words = text.trim().split(/\s+/).filter(Boolean), up = words.map(w => w.toUpperCase());
    const out = [];
    if (!words.length) return out;
    const all = {...Object.fromEntries(Object.entries(FUNCS).filter(([, f]) => !f.ticker).map(([k, f]) => [k, f.about])), ...VIEW_CODES};
    if (words.length === 1 && !text.endsWith(' ')) {
      for (const [k, about] of Object.entries(all)) if (k.startsWith(up[0])) out.push({cmd: k === 'NS' ? 'NS ' : k, label: k, about});
    }
    const ticker = up[0];
    if (/^[A-Z][A-Z0-9.\-]{0,9}$/.test(ticker) && !all[ticker] && !FUNCS[ticker]) {
      const partial = words.length > 1 ? up[up.length - 1] : (text.endsWith(' ') ? '' : null);
      if (partial !== null || words.length === 1)
        for (const [k, f] of Object.entries(FUNCS)) if (f.ticker && (!partial || k.startsWith(partial) || NOISE.has(partial))) out.push({cmd: `${ticker} ${k}`, label: `${ticker} ${k}`, about: f.about});
    }
    return out.slice(0, 8);
  }
  function paint() {
    box.hidden = !items.length;
    box.innerHTML = items.map((s, i) => `<div role="option" class="${i === sel ? 'on' : ''}" data-i="${i}"><b>${escH(s.label)}</b><span>${escH(s.about)}</span></div>`).join('');
  }
  input?.addEventListener('input', () => { hpos = null; items = suggestions(input.value); sel = items.length ? 0 : -1; paint(); });
  input?.addEventListener('blur', () => setTimeout(() => { items = []; paint(); }, 150));
  box.addEventListener('mousedown', e => {
    const d = e.target.closest('[data-i]'); if (!d) return; e.preventDefault();
    const s = items[+d.dataset.i]; items = []; paint();
    if (s.cmd.endsWith(' ')) { input.value = s.cmd; input.focus(); } else { input.value = ''; executeCommand(s.cmd); }
  });
  // Capture phase so we run before the terminal's own Enter handler.
  document.addEventListener('keydown', e => {
    if (e.target !== input) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (items.length) { e.preventDefault(); sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; paint(); return; }
      if (!history.length) return;
      e.preventDefault();
      hpos = hpos == null ? history.length : hpos;
      hpos = Math.max(0, Math.min(history.length, hpos + (e.key === 'ArrowUp' ? -1 : 1)));
      input.value = history[hpos] || '';
      return;
    }
    if (e.key === 'Tab' && items.length) { e.preventDefault(); input.value = items[Math.max(0, sel)].cmd; items = suggestions(input.value); sel = items.length ? 0 : -1; paint(); return; }
    if (e.key === 'Enter') {
      if (items.length && sel >= 0 && input.value.trim().split(/\s+/).length === 1 && !items[sel].cmd.includes(' ') && items[sel].cmd.startsWith(input.value.trim().toUpperCase())) input.value = items[sel].cmd;
      items = []; paint(); hpos = null;
    }
    if (e.key === 'Escape') { items = []; paint(); }
  }, true);

  // Quick-start chips for the new functions
  const quick = document.querySelector('.quick-commands');
  if (quick) quick.insertAdjacentHTML('beforeend', '<button data-command="WS">Workspace</button><button data-command="MOST">Most active</button><button data-command="NVDA OMON">NVDA options</button><button data-command="HELP">All functions</button>');

  let built = false;
  const origOpenView = typeof openView === 'function' ? openView : null;
  if (origOpenView) {
    window.openView = openView = function (view) {
      origOpenView(view);
      if (view === 'ws') { const n = $('#workspace-name'); if (n) n.textContent = '11 / WORKSPACE'; if (!built) { built = true; buildCanvas(); } }
    };
  }
  if (location.hash === '#ws') showWorkspace();
})();
