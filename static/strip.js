// Ticker strip on every view, plus the system-status light in the footer.
//
// The strip shows the cross-asset board (indices, yields, FX, commodities,
// volatility, crypto) as a slow-scrolling tape under the header. It pauses on
// hover or focus, and each item opens a chart window in WS. With reduced motion
// it becomes a plain row you can scroll sideways. For the owner, live.js puts
// real-time Schwab prices on the same items.
(() => {
  const escH = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
  const header = document.querySelector('header');
  if (!header) return;

  const strip = document.createElement('div');
  strip.id = 'tkr';
  strip.className = 'tkr';
  strip.setAttribute('aria-label', 'Market tape. Select an item to open its chart.');
  strip.innerHTML = '<div class="tkr-track"><span class="tkr-wait">LOADING MARKET TAPE…</span></div>';
  header.after(strip);

  const fmt = item => {
    if (typeof marketValue === 'function') { try { return marketValue(item); } catch {} }
    const v = item.value;
    return v == null ? '—' : Number(v).toLocaleString('en-US', {maximumFractionDigits: Math.abs(v) >= 1000 ? 0 : 2});
  };
  const pct = v => v == null ? '—' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toFixed(2)}%`;
  const tone = v => v == null ? '' : v > 0 ? 'up' : v < 0 ? 'down' : '';
  let lastItems = '';

  function render(items) {
    const key = JSON.stringify(items.map(i => [i.code, i.value, i.one_day]));
    if (key === lastItems) return;
    lastItems = key;
    const cells = items.map(i => `<button class="tkr-item" data-code="${escH(i.code)}" title="${escH(i.name)} · open chart"><b>${escH(i.code)}</b><span class="v">${escH(fmt(i))}</span><em class="${tone(i.one_day)}">${pct(i.one_day)}</em></button>`).join('');
    // Two copies make the loop seamless; the second is hidden from screen readers.
    strip.querySelector('.tkr-track').innerHTML = `<div class="tkr-run">${cells}</div><div class="tkr-run" aria-hidden="true">${cells.replace(/<button /g, '<button tabindex="-1" ')}</div>`;
    const run = strip.querySelector('.tkr-run');
    strip.style.setProperty('--tkr-duration', Math.max(30, Math.round(run.scrollWidth / 40)) + 's');
  }

  async function load() {
    try {
      const r = await fetch('/api/markets', {cache: 'no-store'});
      const d = await r.json();
      if (r.ok && d.items?.length) render(d.items);
      else if (!lastItems) strip.querySelector('.tkr-track').innerHTML = '<span class="tkr-wait">MARKET TAPE UNAVAILABLE · RETRYING</span>';
    } catch {}
  }
  strip.addEventListener('click', e => {
    const b = e.target.closest('.tkr-item');
    if (b && typeof executeCommand === 'function') executeCommand(`${b.dataset.code} G`);
  });
  load();
  setInterval(() => { if (!document.hidden) load(); }, 60000);

  // ---------- footer status light ----------
  const footer = document.querySelector('footer');
  const light = document.createElement('button');
  light.className = 'sys-status';
  light.type = 'button';
  light.textContent = '● CHECKING SOURCES';
  footer?.prepend(light);
  light.addEventListener('click', () => typeof openView === 'function' && openView('settings'));

  async function status() {
    try {
      const h = await fetch('/health', {cache: 'no-store'}).then(r => r.json());
      const s = h.sources || {};
      if (h.status === 'ok') {
        light.className = 'sys-status ok';
        light.textContent = `● ALL SYSTEMS NORMAL · ${s.ok}/${s.total} SOURCES`;
        light.title = 'News sources, rates and economic data are all updating.';
      } else {
        light.className = 'sys-status warn';
        light.textContent = `● ${(h.problems || ['degraded']).join(' · ').toUpperCase()}`;
        light.title = (s.failing || []).length ? 'Down: ' + s.failing.join(', ') : 'Some data is not updating.';
      }
    } catch {
      light.className = 'sys-status warn';
      light.textContent = '● STATUS UNKNOWN';
    }
  }
  status();
  setInterval(() => { if (!document.hidden) status(); }, 120000);
})();
