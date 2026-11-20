// Owner-only live overlay. Visitors never get past /api/owner/status (it returns
// {owner:false}), so their tape and watchlist stay on the delayed feed.
// For the owner, Schwab last prices replace the delayed numbers in the price tape
// and watchlist every two seconds, with a brief flash when a price moves.
(() => {
  const POLL_MS = 2000;
  const last = {};
  let quotes = {}, timer = null, badge = null;

  const fmt = (v, d) => Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
  const pct = v => v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}%`;
  const tone = v => v == null ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '';

  function makeBadge(text, cls, title, href) {
    if (!badge) {
      badge = document.createElement('a');
      badge.className = 'live-badge';
      document.querySelector('header .workspace')?.after(badge);
    }
    badge.textContent = text;
    badge.className = 'live-badge ' + cls;
    badge.title = title || '';
    if (href) badge.href = href; else badge.removeAttribute('href');
  }

  function tapeTiles() {
    return [...document.querySelectorAll('#brief-tape .tape-tile')].map(el => ({el, code: (el.querySelector('span')?.textContent || '').trim().split(/\s+/)[0]}));
  }
  function watchRows() {
    return [...document.querySelectorAll('#watchlist .watch-row')].map(el => ({el, code: (el.querySelector('.ticker b')?.textContent || '').trim()}));
  }

  function setText(el, text) { if (el && el.textContent !== text) el.textContent = text; }
  function flash(el, code, price) {
    const prev = last[code];
    last[code] = price;
    if (prev == null || prev === price) return;
    el.classList.remove('tick-up', 'tick-down');
    void el.offsetWidth;
    el.classList.add(price > prev ? 'tick-up' : 'tick-down');
  }

  function apply() {
    for (const {el, code} of tapeTiles()) {
      const q = quotes[code];
      if (!q) continue;
      el.classList.add('live');
      const d = Math.abs(q.price) >= 1000 ? 0 : 2;
      setText(el.querySelector('strong'), fmt(q.price, d));
      const chg = el.querySelector('small');
      if (chg) { setText(chg, pct(q.change_pct)); chg.className = tone(q.change_pct); }
      flash(el, code, q.price);
    }
    for (const {el, code} of watchRows()) {
      const q = quotes[code];
      const stats = el.querySelector('.watch-stats');
      if (!q || !stats) continue;
      el.classList.add('live');
      let price = stats.querySelector('b'), chg = stats.querySelector('em');
      if (!price) { stats.innerHTML = '<b></b><em></em>'; price = stats.querySelector('b'); chg = stats.querySelector('em'); }
      setText(price, fmt(q.price, 2));
      if (chg) { setText(chg, pct(q.change_pct)); chg.className = tone(q.change_pct); }
      flash(el, code, q.price);
    }
  }

  async function poll() {
    clearTimeout(timer);
    if (document.hidden) { timer = setTimeout(poll, POLL_MS); return; }
    const codes = [...new Set([...tapeTiles(), ...watchRows()].map(x => x.code).filter(Boolean))];
    if (codes.length) {
      try {
        const r = await fetch('/api/live/quotes?codes=' + encodeURIComponent(codes.join(',')), {cache: 'no-store', credentials: 'same-origin'});
        const v = await r.json();
        if (!r.ok) throw new Error(v.error || 'Schwab unavailable');
        quotes = v.quotes || {};
        makeBadge('● LIVE · SCHWAB', 'on', `Owner view: real-time Schwab quotes for ${Object.keys(quotes).length} symbols. Visitors see delayed data.`);
        apply();
      } catch (err) {
        makeBadge('● SCHWAB OFFLINE', 'warn', err.message + ' Showing delayed data.', '/owner');
      }
    }
    timer = setTimeout(poll, POLL_MS);
  }

  async function start() {
    let s;
    try { s = await fetch('/api/owner/status', {cache: 'no-store', credentials: 'same-origin'}).then(r => r.json()); } catch { return; }
    if (!s.owner) return;
    document.body.classList.add('owner');
    if (!s.schwab?.connected) {
      makeBadge(s.schwab?.configured ? '● RECONNECT SCHWAB' : '● CONNECT SCHWAB', 'warn',
        'Open http://localhost:8098/owner on the host PC to sign in to Schwab.', s.local ? '/owner' : null);
      return;
    }
    const days = Math.floor((s.schwab.refresh_expires * 1000 - Date.now()) / 86400000);
    if (days <= 1) console.info('Schwab sign-in expires within a day; reconnect at /owner on the host PC.');
    new MutationObserver(apply).observe(document.querySelector('#terminal'), {childList: true, subtree: true});
    poll();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
