const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let data={articles:[],sources:[],alerts:[],rates:[],economy:[],stream:{}}, settings={feeds:[],watchlist:[],rules:[],revision:0}, csrf='', readOnly=false;
let selected=null, linked='AMEX:SPY', paused=false, pending=null, initialized=false, seenAlerts=Number(localStorage.getItem('signal-alert-seen')||0);
let edgarData=null;
let selectedFred=null;
let briefing=null,briefingLoading=false;
let saved={};try{saved=JSON.parse(localStorage.getItem('signal-saved')||'{}')}catch{}
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const stamp=t=>t?new Date(t*1000).toLocaleTimeString('en-US',{hour:'2-digit',minute:'2-digit',hour12:false}):'—';
const full=t=>t?new Date(t*1000).toLocaleString():'Time unavailable';
const symbolCode=s=>s.split(':').pop();
function linkedItem(){return settings.watchlist.find(w=>w.symbol===linked)||{symbol:linked,name:symbolCode(linked),aliases:symbolCode(linked)}}
function articleMatches(a,w){const terms=[symbolCode(w.symbol),...w.aliases.split(',')].filter(x=>x.trim()).map(x=>x.trim().toLowerCase());const text=`${a.title} ${a.summary} ${(a.tickers||[]).join(' ')}`.toLowerCase();return terms.some(t=>new RegExp(`(^|[^a-z0-9])${t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')}([^a-z0-9]|$)`,'i').test(text))}
function sourceMark(source){const name=String(source||'News'),lower=name.toLowerCase(),known=lower.includes('reuters')?['R','reuters']:lower.includes('investing.com')?['INV','investing']:lower.includes('wall street journal')||lower.includes('wsj')?['WSJ','wsj']:lower.includes('marketwatch')?['MW','marketwatch']:lower.includes('bloomberg')?['BBG','bloomberg']:lower.includes('seeking alpha')?['SA','seekingalpha']:lower.includes('cnbc')?['CNBC','cnbc']:lower.includes('yahoo')?['Y!','yahoo']:lower.includes('bbc')?['BBC','bbc']:lower.includes('federal reserve')?['FED','fed']:null,words=name.split(/\s+/).filter(Boolean),label=known?.[0]||(words.length>1?words.slice(0,2).map(x=>x[0]).join(''):name.slice(0,3)).toUpperCase();return `<span class="source-mark ${known?'brand-'+known[1]:'brand-generic'}" title="${esc(name)}">${esc(label)}</span>`}

function openView(view){
  $$('.view').forEach(x=>x.classList.toggle('active',x.dataset.viewPanel===view));
  $$('#functions button').forEach(x=>x.classList.toggle('active',x.dataset.view===view));
  const names={brief:'01 / DAILY MARKET BRIEF',analysis:'02 / BRIEF ANALYTICS',news:'03 / TOP NEWS',research:'04 / COMPANY RESEARCH',macro:'05 / GLOBAL MACRO MOVERS',rates:'06 / RATES & CATALYSTS',fred:'07 / ECONOMIC DATA',watch:'08 / LINKED WATCHLIST',alerts:'09 / ALERTS',settings:'10 / SOURCES'};
  $('#workspace-name').textContent=names[view];
  if(view==='macro'&&!$('#macro-widget').dataset.loaded)marketOverview();
  if(view==='rates'&&!$('#calendar-widget').children.length)economicCalendar();
  if(view==='watch'&&!$('#watch-chart').children.length)chart(linked,'#watch-chart');
}
$('#functions').onclick=e=>{const b=e.target.closest('[data-view]');if(b)openView(b.dataset.view)};
document.body.addEventListener('click',e=>{const b=e.target.closest('[data-open]');if(b)openView(b.dataset.open)});

function sortedNews(){
  const category=$('#category').value,source=$('#source').value;
  let list=category==='Saved'?Object.values(saved):data.articles.slice();
  list=list.filter(a=>(category==='All'||category==='Saved'||a.category===category)&&(source==='All'||a.source===source));
  return list.sort((a,b)=>$('#sort').value==='newest'?(b.published||0)-(a.published||0):(b.relevance||0)-(a.relevance||0)||(b.published||0)-(a.published||0));
}
let newsSignature='';
function renderNews(){
  const full=sortedNews(),list=full.slice(0,newsLimit);
  $('#count').textContent=`${full.length} STORIES`;
  const signature=`${selected}|${newsLimit}|${list.map(a=>a.id+(saved[a.id]?'*':'')).join()}`;
  if(signature===newsSignature)return; newsSignature=signature;
  $('#news').innerHTML=list.length?list.map(a=>`<article class="story ${selected===a.id?'selected':''}"><div>${sourceMark(a.source)}<span class="source">${stamp(a.published)}<br>REL ${a.relevance||35}</span></div><button class="headline" data-story="${a.id}">${esc(a.title)}<small>${esc(a.category)} · ${esc(a.source)}${a.delivery==='STREAM'?'<span class="delivery">● PUSH</span>':''}</small></button><button class="save ${saved[a.id]?'saved':''}" data-save="${a.id}" aria-label="${saved[a.id]?'Unsave':'Save'} headline">${saved[a.id]?'★':'☆'}</button></article>`).join('')+(full.length>list.length?`<button id="news-more" class="news-more">SHOW MORE · ${full.length-list.length} REMAINING</button>`:''):`<div class="empty">${$('#category').value==='Saved'?'No saved stories yet.':'No stories match this filter.'}</div>`;
}
function storyDetail(a){if(!a)return;selected=a.id;$('#detail').innerHTML=`<div class="meta">RELEVANCE ${a.relevance||35} / ${esc(a.source)} / ${esc(a.category)} / ${esc(full(a.published))}</div><h3>${esc(a.title)}</h3><p>${esc(a.summary||'Load the dashboard reader or open the source article.')}</p>${(a.tickers||[]).length?`<div class="meta">SECURITIES / ${esc(a.tickers.join(', '))}</div>`:''}<div class="story-actions"><button data-reader="${esc(a.id)}">READ IN DASHBOARD</button><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">OPEN ORIGINAL ↗</a></div>`;renderNews()}
async function openReader(id){
  const article=data.articles.find(a=>a.id===id)||saved[id]||edgarData?.archive_news?.find(a=>a.id===id);if(!article)return;
  const dialog=$('#reader');$('#reader-source').textContent=article.source;$('#reader-content').innerHTML='<div class="empty">Loading the publisher page…</div>';dialog.showModal();
  try{const response=await fetch('/api/reader?id='+encodeURIComponent(id)),value=await response.json();if(!response.ok)throw new Error(value.error||'Article could not be loaded.');$('#reader-content').innerHTML=`<div class="reader-meta">${esc(value.source)} · ${esc(full(value.published))}</div><h1>${esc(value.title)}</h1>${value.paragraphs.map(p=>`<p>${esc(p)}</p>`).join('')}${value.truncated?'<div class="reader-note">Reader preview ends here. Continue at the original source.</div>':''}<a class="reader-original" href="${esc(value.url)}" target="_blank" rel="noopener noreferrer">OPEN ORIGINAL ARTICLE ↗</a>`}catch(err){$('#reader-content').innerHTML=`<h1>${esc(article.title)}</h1><p>${esc(article.summary||'')}</p><div class="reader-note">${esc(err.message)}</div><a class="reader-original" href="${esc(article.url)}" target="_blank" rel="noopener noreferrer">OPEN ORIGINAL ARTICLE ↗</a>`}
}
$('#detail').onclick=e=>{const b=e.target.closest('[data-reader]');if(b)openReader(b.dataset.reader)};
$('#reader-close').onclick=()=>$('#reader').close();
document.body.addEventListener('click',e=>{const save=e.target.closest('.brief-story [data-save]');if(!save)return;const id=save.dataset.save,a=data.articles.find(x=>x.id===id)||saved[id];if(saved[id])delete saved[id];else if(a)saved[id]=a;localStorage.setItem('signal-saved',JSON.stringify(saved));renderBriefing()});
$('#news').onclick=e=>{const save=e.target.closest('[data-save]'),story=e.target.closest('[data-story]');if(save){const id=save.dataset.save,a=data.articles.find(x=>x.id===id)||saved[id];if(saved[id])delete saved[id];else if(a)saved[id]=a;localStorage.setItem('signal-saved',JSON.stringify(saved));renderNews()}else if(story)storyDetail(data.articles.find(x=>x.id===story.dataset.story)||saved[story.dataset.story])};
['#category','#source','#sort'].forEach(s=>$(s).onchange=()=>{newsLimit=150;renderNews()});
$('#news').addEventListener('click',e=>{if(e.target.closest('#news-more')){newsLimit+=150;renderNews()}});
$('#pause').onclick=()=>{paused=!paused;$('#pause').textContent=paused?'Resume':'Pause';$('#feed-label').textContent=paused?'Feed paused':'Continuous coverage';if(!paused&&pending){receive(pending);pending=null}};

function renderMorning(){renderRates('#morning-rates',true);renderCurve()}
function renderCurve(){
  const order=['DFF','SOFR','DGS2','DGS10','DGS30'],labels={DFF:'FED FUNDS',SOFR:'SOFR',DGS2:'2Y',DGS10:'10Y',DGS30:'30Y'};
  const points=order.map(x=>data.rates.find(r=>r.series===x)).filter(Boolean);
  if(points.length<3)return $('#curve-chart').innerHTML='<div class="empty">Loading the curve…</div>';
  const w=760,h=240,pad=44,values=points.map(p=>p.value),min=Math.min(...values),max=Math.max(...values),range=(max-min)||1;
  const x=i=>pad+i*(w-pad*2)/(points.length-1),y=v=>h-pad-((v-min)/range)*(h-pad*2);
  const line=points.map((p,i)=>`${x(i)},${y(p.value)}`).join(' ');
  const prior=points.map((p,i)=>`${x(i)},${y(p.previous)}`).join(' ');
  const dots=points.map((p,i)=>`<circle cx="${x(i)}" cy="${y(p.value)}" r="4"/><text class="curve-value" x="${x(i)}" y="${y(p.value)-12}" text-anchor="middle">${p.value.toFixed(2)}%</text><text class="curve-label" x="${x(i)}" y="${h-16}" text-anchor="middle">${labels[p.series]}</text>`).join('');
  const spread=points.at(-2)&&points[2]?((points.find(p=>p.series==='DGS10').value-points.find(p=>p.series==='DGS2').value)*100).toFixed(0):null;
  $('#curve-chart').innerHTML=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="US yield curve"><polyline class="curve-prior" points="${prior}"/><polyline class="curve-now" points="${line}"/>${dots}</svg><div class="curve-meta">SOLID = LATEST · DASHED = PRIOR OBSERVATION${spread!==null?` · 2s10s ${spread>=0?'+':''}${spread} BP`:''}</div>`;
}
function briefStories(items){return items.length?items.map(a=>`<div class="brief-story"><button data-brief-article="${esc(a.id)}"><span>${esc(a.title)}${a.summary?`<small>${esc(a.summary)}</small>`:''}</span><time>${stamp(a.published)}${a.sources>1?`<i>${a.sources} SOURCES</i>`:''}</time></button><button class="save ${saved[a.id]?'saved':''}" data-save="${esc(a.id)}" aria-label="${saved[a.id]?'Unsave':'Save'} headline">${saved[a.id]?'★':'☆'}</button></div>`).join(''):'<div class="empty">No qualifying stories in the last 24 hours.</div>'}
function briefSignature(b){if(!b)return '';const keys=['headline','summary','posture','regime','tape','conditions','playbook','catalysts','risks','macro','micro','themes','watchlist'];return JSON.stringify(Object.fromEntries(keys.map(k=>[k,b[k]])))}
const signed=(v,d=1,suffix='%')=>v==null?'—':`${v>0?'+':v<0?'−':''}${Math.abs(v).toFixed(d)}${suffix}`;
const toneClass=v=>v==null?'':v>0?'up':v<0?'down':'';
function spark(points,dir){
  if(!points||points.length<3)return '';
  const min=Math.min(...points),max=Math.max(...points),range=max-min||1,w=72,h=20;
  const path=points.map((v,i)=>`${(i*w/(points.length-1)).toFixed(1)},${(h-((v-min)/range)*h).toFixed(1)}`).join(' ');
  return `<svg class="spark ${dir||''}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polyline points="${path}"/></svg>`;
}
const fmtNum=(v,d=2)=>v==null?'—':Number(v).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
let briefingPoll=null,economyHistory={},economyStamp=null,newsLimit=150;
const briefButtons=()=>$$('.refresh-brief');
function setBriefText(id,value){$$(`#${id}, #${id}-2`).forEach(el=>{el.textContent=value})}
function renderBriefAI(ai){
  const el=$('#brief-ai');
  if(!ai||ai.status==='off'){el.hidden=true;return}
  el.hidden=false;const n=ai.note;
  if(!n){el.innerHTML=`<div class="brief-ai-head"><b>ANALYST NOTE</b><span>${ai.status==='generating'?'WRITING FROM LATEST DATA…':esc(ai.error||'WAITING')}</span></div>`;return}
  el.innerHTML=`<div class="brief-ai-head"><b>ANALYST NOTE</b><span>${esc(n.model)} · ${stamp(n.generated_at)}${ai.status==='generating'?' · UPDATING…':''}</span></div><h3>${esc(n.headline)}</h3><div class="brief-ai-grid"><div><h4>MARKET CONDITIONS</h4><p>${esc(n.market_conditions)}</p><h4>ECONOMIC DATA</h4><p>${esc(n.economic_data)}</p><h4>GEOPOLITICS</h4><p>${esc(n.geopolitics)}</p></div><div><h4>TRADING PLAN</h4><ul>${n.trading_plan.map(x=>`<li>${esc(x)}</li>`).join('')}</ul><h4>RISKS TO THE VIEW</h4><ul>${n.risks_to_view.map(x=>`<li>${esc(x)}</li>`).join('')}</ul></div></div>`;
}
function renderBriefing(){
  if(!briefing)return;const b=briefing;
  setBriefText('brief-time',`BUILT ${full(b.generated_at).toUpperCase()}`);
  setBriefText('brief-posture',b.posture||'MONITORING');
  $$('#brief-posture, #brief-posture-2').forEach(el=>{el.dataset.regime=(b.regime?.label||'').toLowerCase()});
  setBriefText('brief-score',b.regime?`REGIME SCORE ${b.regime.score>0?'+':''}${b.regime.score}`:'');
  $$('#brief-session, #brief-session-2').forEach(el=>{el.textContent=b.session?b.session.label:'';el.title=b.session?.note||''});
  $('#brief-detail').textContent=b.summary||'';$('#brief-detail-2').textContent=b.analytics?.summary||b.summary||'';
  $('#brief-change').innerHTML=(b.change?.lines||[]).length?b.change.lines.map(x=>`<span>${esc(x)}</span>`).join(''):'<span class="muted">First brief stored for this date — day-over-day comparison starts with the next one.</span>';
  $('#brief-summary').textContent=b.headline||'The daily brief is ready.';$('#brief-summary-2').textContent=b.analytics?.headline||b.headline||'The analytics are ready.';
  const refresh=b.refresh,status=$('#brief-refresh-status');
  if(refresh){const changed=refresh.view_changed??(refresh.new_stories>0||refresh.market_changed),checked=refresh.checked_at?full(refresh.checked_at).toUpperCase():'JUST NOW',details=`${refresh.sources_online}/${refresh.sources_total} SOURCES · ${refresh.new_stories} NEW STORIES · ${refresh.market_changed?'MARKET DATA UPDATED':'MARKET TIMESTAMP UNCHANGED'}`;status.hidden=false;status.className=`brief-refresh-status ${changed?'changed':'current'}`;status.innerHTML=`<b>${changed?'BRIEF UPDATED':'NO MATERIAL CHANGE'}</b><span>Checked ${esc(checked)} · ${esc(details)}</span><p>${changed?'New inputs changed at least one ranked section of the brief.':'The refresh completed, but no new input changed the ranked analysis. The existing brief is still current.'}</p>`}else status.hidden=true;
  renderBriefAI(b.ai);
  clearTimeout(briefingPoll);if(b.ai?.status==='generating')briefingPoll=setTimeout(()=>loadBriefing(false,true),15000);
  $('#brief-tape').innerHTML=(b.tape||[]).length?b.tape.map(t=>`<div class="tape-tile ${t.group==='Futures'?'futures':''}"><span>${esc(t.code)}${t.group==='Futures'?' <i>FUT</i>':''}</span><strong>${esc(t.value)}</strong>${spark(t.spark,t.dir)}<small class="${t.dir==='up'?'up':t.dir==='down'?'down':''}">${esc(t.one_day)}</small><small>5D ${esc(t.five_day)}</small></div>`).join(''):'<div class="empty">Market prices are unavailable right now.</div>';
  $('#brief-regime').innerHTML=(b.regime?.components||[]).map(c=>`<span class="regime-chip ${c.score>0?'pos':c.score<0?'neg':''}" title="Score ${c.score>0?'+':''}${c.score}"><b>${esc(c.label.toUpperCase())}</b>${esc(c.detail)}</span>`).join('');
  $('#brief-conditions').innerHTML=(b.conditions||[]).length?b.conditions.map(c=>`<div class="tone-${esc(c.tone)}"><b>${esc(c.label)}</b><p>${esc(c.text)}</p></div>`).join(''):'<div class="empty">Waiting for market data.</div>';
  $('#brief-playbook').innerHTML=(b.playbook||[]).length?b.playbook.map(p=>`<div class="play stance-${esc(p.stance)}"><b>${esc(p.tag)}</b><p>${esc(p.text)}</p></div>`).join(''):'<div class="empty">No playbook items yet.</div>';
  $('#brief-levels').innerHTML=(b.levels||[]).length?`<table><thead><tr><th>KEY LEVELS</th><th>30D</th><th>LAST</th><th>PREV</th><th>20D</th><th>50D</th><th>1M HIGH</th><th>1M LOW</th></tr></thead><tbody>${b.levels.map(l=>`<tr><td><b>${esc(l.code)}</b><small>${esc(l.trend||'')}</small></td><td class="spark-cell">${spark(l.spark,l.trend==='uptrend'?'up':l.trend==='downtrend'?'down':'')}</td><td>${fmtNum(l.last)}</td><td>${fmtNum(l.prev_close)}</td><td class="${l.last>l.sma20?'up':'down'}">${fmtNum(l.sma20)}</td><td class="${l.last>l.sma50?'up':'down'}">${fmtNum(l.sma50)}</td><td>${fmtNum(l.high_1m)}</td><td>${fmtNum(l.low_1m)}</td></tr>`).join('')}</tbody></table>`:'';
  $('#brief-macro-read').textContent=b.macro_read||'';
  $('#brief-economy').innerHTML=(b.economy||[]).length?b.economy.map(r=>`<div class="econ-row tone-${esc(r.tone)}" data-fred-open="${esc(r.series)}" role="button" tabindex="0" title="Open ${esc(r.name)} in the FRED dashboard"><div><b>${esc(r.name)}</b><small>${esc(r.date)}</small></div><strong>${esc(r.value)}<i class="trend-${esc(r.trend)}">${r.trend==='up'?'▲':r.trend==='down'?'▼':'■'}</i></strong><span>PRIOR ${esc(r.prior)}</span><p>${esc(r.read)}</p></div>`).join(''):'<div class="empty">FRED observations are still loading.</div>';
  $('#brief-fed').innerHTML=(b.fed?.lines||[]).map(x=>`<div><p>${esc(x)}</p></div>`).join('')||'<div class="empty">No Fed coverage in the last 24 hours.</div>';
  $('#brief-catalysts').innerHTML=(b.catalysts||[]).length?b.catalysts.map(e=>`<button ${e.article_id?`data-brief-article="${esc(e.article_id)}"`:'disabled'} class="imp-${esc(e.importance)} ${e.calendar?'calendar-event':''}"><b>${esc(e.when.toUpperCase())}</b><span><strong>${esc(e.name)}</strong><small>${esc(e.timing)}${e.calendar?` · OFFICIAL ${esc(e.source)} CALENDAR`:e.mentions?` · ${e.mentions} stories`:''}${e.headline?` · ${esc(e.headline)}`:''}</small></span></button>`).join(''):'<div class="empty">No scheduled catalysts found in the next 60 days.</div>';
  $('#brief-geo').innerHTML=(b.geopolitics||[]).length?b.geopolitics.map(g=>`<article class="geo-card ${g.priced?'priced':''}"><div class="head"><b>${esc(g.region)}</b><span class="sev">${'●'.repeat(g.severity)}${'○'.repeat(5-g.severity)}</span></div><div class="geo-meta">${g.stories} STORIES · ${g.fresh} IN 6H · ${esc(g.momentum.toUpperCase())}</div><p><em>Channel:</em> ${esc(g.channel)}</p><p><em>Market check:</em> ${esc(g.market_check)}</p><p class="verdict">${esc(g.verdict)}</p>${g.top.map(s=>`<button data-brief-article="${esc(s.id)}">${esc(s.title)}</button>`).join('')}</article>`).join(''):'<div class="empty">No market-relevant geopolitical clusters in the last 24 hours.</div>';
  $('#brief-economy').onclick=e=>{const row=e.target.closest('[data-fred-open]');if(row){openView('fred');selectFred(row.dataset.fredOpen)}};
  const sectors=(b.sectors||[]).filter(s=>s.rs_5d!=null),max=Math.max(1,...sectors.map(s=>Math.abs(s.rs_5d)));
  $('#brief-sectors').innerHTML=sectors.length?sectors.map(s=>`<div class="sector-row"><span><b>${esc(s.code)}</b>${esc(s.name)}</span><div class="bar"><i class="${s.rs_5d>=0?'positive':'negative'}" style="${s.rs_5d>=0?'left:50%':'right:50%'};width:${Math.abs(s.rs_5d)/max*50}%"></i></div><small class="${toneClass(s.rs_5d)}">${signed(s.rs_5d)}</small><small class="${toneClass(s.one_day)}">1D ${signed(s.one_day,2)}</small></div>`).join(''):'<div class="empty">Sector prices are unavailable.</div>';
  $('#brief-themes').innerHTML=(b.themes||[]).length?b.themes.map(t=>`<div class="theme"><div class="head"><b>${esc(t.name)}</b><span>${t.count} STORIES · ${t.sources} SOURCES · ${esc(t.momentum.toUpperCase())}</span></div>${t.stories.map(s=>`<button data-brief-article="${esc(s.id)}">${esc(s.title)}<time>${esc(s.source)} · ${stamp(s.published)}</time></button>`).join('')}</div>`).join(''):'<div class="empty">No dominant themes yet.</div>';
  $('#brief-macro').innerHTML=briefStories(b.macro||[]);$('#brief-micro').innerHTML=briefStories(b.micro||[]);
  $('#brief-watch').innerHTML=(b.watchlist||[]).length?b.watchlist.map(x=>`<button ${x.article_id?`data-brief-article="${esc(x.article_id)}"`:''}><b>${esc(x.symbol)}</b><span class="watch-move">${x.price!=null?fmtNum(x.price):'—'} <em class="${toneClass(x.one_day)}">${signed(x.one_day,2)}</em><small>RS 5D <em class="${toneClass(x.rs_5d)}">${signed(x.rs_5d)}</em> · ${esc(x.trend||'—')}</small></span><span class="watch-news">${x.headline?esc(x.headline):'No news in 24h'}<small>${x.count} STORIES</small></span></button>`).join(''):'<div class="empty">Add symbols in MON.</div>';
  $('#brief-risks').innerHTML=(b.risks||[]).map(x=>`<div><p>${esc(x)}</p></div>`).join('')||'<div class="empty">No elevated risks flagged.</div>';
  const c=b.coverage||{};setBriefText('brief-footer',`${c.stories||0} STORIES IN 24H (${c.filtered||0} OFF-TOPIC FILTERED) · ${c.sources_online||0}/${c.sources_total||0} SOURCES · ${c.prices_available||0}/${c.prices_expected||0} PRICES${b.market_as_of?' · PRICES AS OF '+full(b.market_as_of).toUpperCase():''} · ${b.disclaimer||''}`);
  $$('.brief-prices').forEach(el=>{el.textContent=c.prices_expected&&c.prices_available<c.prices_expected?`${c.prices_available}/${c.prices_expected} PRICES`:''});
  briefButtons().forEach(button=>{button.disabled=false;button.textContent='REFRESH BRIEF';button.classList.remove('refresh-available')});
  renderWatchlist();
}
async function loadBriefing(refresh=false,quiet=false){
  if(briefingLoading)return;briefingLoading=true;const before=briefSignature(briefing);if(!quiet)briefButtons().forEach(button=>{button.disabled=true;button.textContent=refresh?'REFRESHING SOURCES…':'BUILDING BRIEF…'});
  try{const response=await fetch('/api/briefing'+(refresh&&!readOnly?'/refresh':''),refresh&&!readOnly?{method:'POST',headers:{'Content-Type':'application/json','X-Signal-Token':csrf},body:'{}'}:undefined),value=await response.json();if(!response.ok)throw new Error(value.error||'Brief could not be generated.');if(refresh&&value.refresh)value.refresh.view_changed=before!==briefSignature(value);briefing=value;renderBriefing()}catch(err){if(quiet&&briefing){briefingPoll=setTimeout(()=>loadBriefing(false,true),30000);return}briefButtons().forEach(button=>{button.disabled=false;button.textContent='TRY REFRESH AGAIN'});setBriefText('brief-time','BRIEF UNAVAILABLE');setBriefText('brief-summary',err.message)}finally{briefingLoading=false}
}
briefButtons().forEach(button=>{button.onclick=()=>loadBriefing(true)});
document.body.addEventListener('click',e=>{const b=e.target.closest('[data-brief-article]');if(b)openReader(b.dataset.briefArticle)});
function renderRates(target,compact=false){const el=$(target);el.innerHTML=data.rates.length?data.rates.map(r=>{const d=r.value-r.previous,cls=d>0?'up':d<0?'down':'';return compact?`<div class="rate-tile"><b>${esc(r.name)}</b><strong>${r.value.toFixed(2)}%</strong><small class="${cls}">${d>=0?'+':''}${d.toFixed(2)} · ${esc(r.date)}</small></div>`:`<div class="rate-row"><label>${esc(r.name)} / ${esc(r.series)}</label><strong>${r.value.toFixed(2)}%</strong><span class="${cls}">${d>=0?'▲':'▼'} ${Math.abs(d).toFixed(2)} FROM PRIOR · ${esc(r.date)}</span></div>`}).join(''):'<div class="empty">Loading official rate observations…</div>'}

function fredValue(item,value=item.value){const decimals=Math.abs(value)>=100?0:Math.abs(value)>=10?1:2,suffix=item.unit.startsWith('%')?'%':item.unit.startsWith('K ')?'K':'';return `${item.unit.startsWith('$')?'$':''}${Number(value).toLocaleString('en-US',{minimumFractionDigits:decimals,maximumFractionDigits:decimals})}${suffix}`}
function selectFred(series){
  const item=(data.economy||[]).find(x=>x.series===series);if(!item)return;selectedFred=series;
  $$('#fred-grid button').forEach(b=>b.classList.toggle('active',b.dataset.fred===series));$('#fred-code').textContent=item.series;$('#fred-title').textContent=item.name;$('#fred-source-link').href=item.url;
  const rows=item.history.slice(-60),values=rows.map(x=>x.value),width=900,height=330,pad=42,min=Math.min(...values),max=Math.max(...values),range=max-min||1;
  const points=rows.map((x,i)=>`${pad+i*(width-pad*2)/Math.max(1,rows.length-1)},${height-pad-(x.value-min)*(height-pad*2)/range}`).join(' ');
  const labels=[0,Math.floor((rows.length-1)/2),rows.length-1].filter((x,i,a)=>a.indexOf(x)===i).map(i=>`<text x="${pad+i*(width-pad*2)/Math.max(1,rows.length-1)}" y="${height-10}" text-anchor="${i===0?'start':i===rows.length-1?'end':'middle'}">${esc(rows[i].date)}</text>`).join('');
  $('#fred-chart').innerHTML=`<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(item.name)} history"><line x1="${pad}" y1="${pad}" x2="${pad}" y2="${height-pad}"/><line x1="${pad}" y1="${height-pad}" x2="${width-pad}" y2="${height-pad}"/><text x="8" y="${pad+4}">${esc(fredValue(item,max))}</text><text x="8" y="${height-pad+4}">${esc(fredValue(item,min))}</text><polyline points="${points}"/><circle cx="${width-pad}" cy="${height-pad-(values.at(-1)-min)*(height-pad*2)/range}" r="5"/>${labels}</svg>`;
  const delta=item.value-item.previous;$('#fred-chart-meta').innerHTML=`<b>${esc(fredValue(item))}</b> ${delta>=0?'▲':'▼'} ${esc(Math.abs(delta).toFixed(2))} from prior observation · ${esc(item.date)} · ${esc(item.unit)}`;
}
function renderFred(){
  const group=$('#fred-group').value,list=(data.economy||[]).filter(x=>group==='All indicators'||x.group===group);
  $('#fred-grid').innerHTML=list.length?list.map(x=>{const delta=x.value-x.previous,cls=delta>0?'up':delta<0?'down':'';return `<button data-fred="${esc(x.series)}" class="${x.series===selectedFred?'active':''}"><span>${esc(x.group)} · ${esc(x.series)}</span><strong>${esc(fredValue(x))}</strong><small class="${cls}">${delta>=0?'▲':'▼'} ${Math.abs(delta).toFixed(2)} · ${esc(x.date)}</small><b>${esc(x.name)}</b></button>`}).join(''):'<div class="empty">No indicators match this category.</div>';
  $('#fred-updated').textContent=data.economy_updated?`UPDATED ${stamp(data.economy_updated)} · ${data.economy.length} SERIES`:'LOADING SERIES';
  const next=list.some(x=>x.series===selectedFred)?selectedFred:list[0]?.series;if(next)selectFred(next);
}
$('#fred-grid').onclick=e=>{const b=e.target.closest('[data-fred]');if(b)selectFred(b.dataset.fred)};$('#fred-group').onchange=renderFred;

function selectSymbol(symbol){linked=symbol;$('#linked-symbol').textContent=symbol;$('#watch-linked').textContent=symbol;chart(symbol,'#chart');chart(symbol,'#watch-chart');renderWatchlist();renderLinkedNews()}
function watchStats(code){return (briefing?.watchlist||[]).find(x=>x.symbol===code)}
function renderWatchlist(){
  $('#watchlist').innerHTML=settings.watchlist.length?settings.watchlist.map((w,i)=>{const s=watchStats(symbolCode(w.symbol));return `<div class="watch-row ${w.symbol===linked?'active':''}"><button class="ticker" data-link="${esc(w.symbol)}"><b>${esc(symbolCode(w.symbol))}</b><span>${esc(w.name)}</span></button><span class="watch-stats">${s&&s.price!=null?`<b>${fmtNum(s.price)}</b><em class="${toneClass(s.one_day)}">${signed(s.one_day,2)}</em><small>RS 5D <em class="${toneClass(s.rs_5d)}">${signed(s.rs_5d)}</em> · ${esc(s.trend||'—')}</small>`:'<small class="muted">no price</small>'}</span><button class="icon-action" data-research-symbol="${esc(symbolCode(w.symbol))}">RESEARCH</button><button class="icon-action" data-edit-symbol="${i}">EDIT</button><button class="icon-action" data-delete-symbol="${i}" aria-label="Delete ${esc(w.name)}">×</button></div>`}).join(''):'<div class="empty">No symbols. Add one to link charts and news.</div>';
}
function renderLinkedNews(){const w=linkedItem();const list=w?data.articles.filter(a=>articleMatches(a,w)).slice(0,8):[];$('#linked-news').innerHTML=list.length?list.map(a=>`<button data-linked-story="${a.id}">${esc(a.title)}<small>${esc(a.source)} · ${stamp(a.published)}</small></button>`).join(''):'<div class="empty">No linked headlines found.</div>'}
$('#watchlist').onclick=e=>{const link=e.target.closest('[data-link]'),research=e.target.closest('[data-research-symbol]'),edit=e.target.closest('[data-edit-symbol]'),del=e.target.closest('[data-delete-symbol]');if(link)selectSymbol(link.dataset.link);if(research){openView('research');runEdgar(research.dataset.researchSymbol)}if(edit)symbolEditor(Number(edit.dataset.editSymbol));if(del){const next=settings.watchlist.filter((_,i)=>i!==Number(del.dataset.deleteSymbol));saveSettings({watchlist:next})}};
$('#linked-news').onclick=e=>{const b=e.target.closest('[data-linked-story]');if(b){openView('news');storyDetail(data.articles.find(a=>a.id===b.dataset.linkedStory))}};

function renderSettings(){
  $('#feeds').innerHTML=settings.feeds.map((f,i)=>`<div class="manage-item ${f.enabled?'':'off'}"><div><strong>${esc(f.name)}</strong><small>${esc(f.category)} · ${esc(f.url)}</small></div><span class="state">${f.enabled?'ON':'OFF'}</span><div><button class="icon-action" data-edit-feed="${i}">EDIT</button><button class="icon-action" data-delete-feed="${i}">×</button></div></div>`).join('');
  $('#rules').innerHTML=settings.rules.length?settings.rules.map((r,i)=>`<div class="manage-item ${r.enabled?'':'off'}"><div><strong>${esc(r.name)}</strong><small>${r.kind==='watchlist'?'Any linked watchlist name or symbol':esc(r.terms)}</small></div><span class="state">${r.enabled?'ON':'OFF'}</span><div><button class="icon-action" data-edit-rule="${i}">EDIT</button><button class="icon-action" data-delete-rule="${i}">×</button></div></div>`).join(''):'<div class="empty">No alert rules yet.</div>';
  $('#stream-status').textContent=data.stream?.status||'OPTIONAL — NO SUBSCRIPTION';
}
function renderSources(){const healthy=data.sources.filter(s=>s.ok).length;$('#health').textContent=`${healthy}/${data.sources.length} ONLINE`;$('#sources').innerHTML=data.sources.map(s=>`<div class="source-status ${s.ok?'':'error'}" title="${esc(s.ok?'Last checked '+full(s.checked):s.error)}"><span>${esc(s.name)}</span><small>${s.ok?'● CONNECTED':'RETRYING'}</small></div>`).join('')}
function renderAlerts(){
  $('#alert-log').innerHTML=data.alerts.length?data.alerts.map(a=>`<div><span class="score">!</span><button data-alert-story="${a.article.id}">${esc(a.rule)} — ${esc(a.article.title)}</button><small>${stamp(a.at)}</small></div>`).join(''):'<div class="empty">No alerts have fired.</div>';
  const unseen=Math.max(0,data.alerts.length-seenAlerts);$('#alert-count').textContent=unseen;$('#alert-count').hidden=!unseen;
}
$('#alert-log').onclick=e=>{const b=e.target.closest('[data-alert-story]');if(b){openView('news');storyDetail(data.articles.find(a=>a.id===b.dataset.alertStory)||data.alerts.find(a=>a.article.id===b.dataset.alertStory)?.article)}};
$('#clear-badge').onclick=()=>{seenAlerts=data.alerts.length;localStorage.setItem('signal-alert-seen',seenAlerts);renderAlerts()};
$('#notify').onclick=async()=>{if(!('Notification' in window))return alert('This browser does not support desktop notifications.');const p=await Notification.requestPermission();$('#notify').textContent=p==='granted'?'NOTIFICATIONS ENABLED':'NOTIFICATIONS BLOCKED'};

function factValue(value,unit){
  if(value===null||value===undefined)return '—';
  const negative=value<0,absolute=Math.abs(value),prefix=unit==='USD'?'$':'';
  if(unit==='USD/shares')return `${negative?'−':''}$${absolute.toFixed(2)}`;
  const scales=[[1e12,'T'],[1e9,'B'],[1e6,'M'],[1e3,'K']],scale=scales.find(x=>absolute>=x[0]);
  const rendered=scale?(absolute/scale[0]).toFixed(absolute/scale[0]>=100?0:absolute/scale[0]>=10?1:2)+scale[1]:absolute.toLocaleString('en-US',{maximumFractionDigits:2});
  return `${negative?'−':''}${prefix}${rendered}${unit==='shares'?' sh':''}`;
}
function factPeriod(row){return row?`${row.fp||''}${row.fy?' '+row.fy:''} · ${row.form} · END ${row.end}`:'NOT REPORTED'}
function researchArticles(){
  if(!edgarData)return [];
  const c=edgarData.company,tickers=(c.tickers||[]).map(x=>String(x).toUpperCase()),watch=settings.watchlist.find(w=>tickers.includes(symbolCode(w.symbol)));
  const base=String(c.name||'').replace(/[,\s]+(INCORPORATED|INC\.?|CORPORATION|CORP\.?|COMPANY|CO\.?|PLC|LTD\.?)$/i,'').trim();
  const aliases=[c.name,base,watch?.name,...String(watch?.aliases||'').split(',')].filter(x=>String(x).trim().length>=3);
  const matcher={symbol:'SEC:'+(tickers[0]||''),name:c.name||'',aliases:aliases.join(',')};
  const pool=[...data.articles,...(edgarData.archive_news||[])],unique=[...new Map(pool.map(a=>[a.id,a])).values()];
  return unique.filter(a=>(a.tickers||[]).some(t=>tickers.includes(String(t).toUpperCase()))||articleMatches(a,matcher)).sort((a,b)=>(b.published||0)-(a.published||0)||(b.relevance||0)-(a.relevance||0)).slice(0,20);
}
function renderResearchNews(){
  const target=$('#research-news');if(!target)return;const articles=researchArticles();
  const searched=articles.filter(a=>a.delivery==='SEARCH').length;$('#research-news-count').textContent=`${articles.length} MATCHES${searched?' · '+searched+' SEARCHED':''}`;
  target.innerHTML=articles.length?articles.map(a=>`<article><div>${sourceMark(a.source)}<small>${stamp(a.published)} · REL ${a.relevance||35}${a.delivery==='SEARCH'?'<br>NEWS SEARCH':''}</small></div><button data-research-article="${esc(a.id)}"><strong>${esc(a.title)}</strong><span>${esc(a.summary||'Open this story in the dashboard reader.')}</span></button><a href="${esc(a.url)}" target="_blank" rel="noopener noreferrer" aria-label="Open original article">↗</a></article>`).join(''):'<div class="empty">No matching articles were found in the current feeds or historical news search.</div>';
}
function renderFilings(filter='ALL'){
  if(!edgarData)return;
  const keyForms=new Set(['10-K','10-K/A','10-Q','10-Q/A','8-K','8-K/A','DEF 14A','20-F','40-F','6-K','S-1','S-1/A']);
  const filings=edgarData.filings.filter(f=>filter==='ALL'||filter==='KEY'?filter==='ALL'||keyForms.has(f.form):f.form===filter).slice(0,40);
  $('#filing-rows').innerHTML=filings.length?filings.map(f=>`<a class="filing-row" href="${esc(f.url)}" target="_blank" rel="noopener noreferrer"><b>${esc(f.form)}</b><span><strong>${esc(f.description||f.form)}</strong><small>ACCESSION ${esc(f.accession)}${f.report_date?' · PERIOD '+esc(f.report_date):''}</small></span><time>${esc(f.filed)}</time><i>OPEN ↗</i></a>`).join(''):'<div class="empty">No filings match this form.</div>';
}
function renderEdgar(result){
  edgarData=result;const c=result.company,ticker=(c.tickers||[])[0]||'—';
  const forms=[...new Set(result.filings.map(f=>f.form))];
  const byLabel=Object.fromEntries(result.metrics.map(m=>[m.label,m]));
  const annualValue=label=>byLabel[label]?.annual?.[0]?.val;
  const ratio=(numerator,denominator)=>Number.isFinite(numerator)&&Number.isFinite(denominator)&&denominator!==0?numerator/denominator:null;
  const derived=[['OPERATING MARGIN',ratio(annualValue('Operating income'),annualValue('Revenue')),'percent'],['NET MARGIN',ratio(annualValue('Net income'),annualValue('Revenue')),'percent'],['CURRENT RATIO',ratio(annualValue('Current assets'),annualValue('Current liabilities')),'ratio'],['DEBT / EQUITY',ratio(annualValue('Long-term debt'),annualValue('Stockholders equity')),'ratio'],['FREE CASH FLOW',Number.isFinite(annualValue('Operating cash flow'))&&Number.isFinite(annualValue('Capital expenditures'))?annualValue('Operating cash flow')-annualValue('Capital expenditures'):null,'USD']];
  const derivedHtml=derived.map(([label,value,unit])=>`<div><span>${label}</span><strong>${value===null?'—':unit==='percent'?(value*100).toFixed(1)+'%':unit==='ratio'?value.toFixed(2)+'×':factValue(value,unit)}</strong></div>`).join('');
  const metrics=result.metrics.map(m=>{const q=m.quarterly[0],a=m.annual[0],primary=q||a;return `<article class="fact-card" title="${esc(m.description)}"><label>${esc(m.label)}</label><strong>${primary?factValue(primary.val,primary.unit):'—'}</strong><small>${esc(factPeriod(primary))}</small>${q&&a?`<div><span>LATEST FY</span><b>${factValue(a.val,a.unit)}</b><em>${esc(a.fy||a.end)}</em></div>`:''}</article>`}).join('');
  const periods=(byLabel.Revenue?.annual||result.metrics.find(m=>m.annual.length)?.annual||[]).slice(0,5).map(x=>x.end);
  const historyRows=result.metrics.filter(m=>m.annual.length).map(m=>`<tr><th>${esc(m.label)}</th>${periods.map(period=>{const row=m.annual.find(x=>x.end===period);return `<td>${row?factValue(row.val,row.unit):'—'}</td>`}).join('')}</tr>`).join('');
  const history=`<div class="history-wrap"><table><thead><tr><th>METRIC</th>${periods.map(p=>`<th>${esc(p)}</th>`).join('')}</tr></thead><tbody>${historyRows}</tbody></table></div>`;
  const exchange=String(c.exchanges?.[0]||'NASDAQ').toUpperCase().replace('NASDAQ GLOBAL SELECT MARKET','NASDAQ');
  $('#edgar-result').innerHTML=`<section class="company-profile"><div><span class="company-ticker">${esc(ticker)}</span><h2>${esc(c.name||'SEC registrant')}</h2><p>${esc([c.exchanges?.filter(Boolean).join(' / '),c.industry,c.address].filter(Boolean).join(' · '))}</p></div><dl><div><dt>CIK</dt><dd>${esc(c.cik)}</dd></div><div><dt>FISCAL YEAR END</dt><dd>${esc(c.fiscal_year_end||'—')}</dd></div><div><dt>INCORPORATED</dt><dd>${esc(c.state||'—')}</dd></div><div><dt>FILER</dt><dd>${esc(c.category||'—')}</dd></div></dl><a href="${esc(result.sec_url)}" target="_blank" rel="noopener noreferrer">OPEN SEC COMPANY PAGE ↗</a></section><section class="research-overview"><div><div class="research-heading"><h3>MARKET CHART</h3><span>${esc(exchange+':'+ticker)}</span></div><div id="research-chart"></div></div><div><div class="research-heading"><h3>FY SNAPSHOT</h3><span>DERIVED FROM SEC FACTS</span></div><div class="ratio-grid">${derivedHtml}</div></div></section><div class="research-heading"><h3>RELATED NEWS</h3><span id="research-news-count">0 MATCHES</span></div><section id="research-news" class="research-news"><div class="empty">Matching current headlines…</div></section><div class="research-heading"><h3>STANDARDIZED FINANCIAL FACTS</h3><span>QUARTERLY WHERE AVAILABLE · LATEST FY COMPARISON</span></div><section class="fact-grid">${metrics||'<div class="empty">No standard US-GAAP facts were returned for this registrant.</div>'}</section><div class="research-heading"><h3>FIVE-YEAR FINANCIAL HISTORY</h3><span>REPORTED VALUES</span></div>${history}<div class="research-heading"><h3>RECENT FILINGS</h3><select id="filing-filter" aria-label="Filter filing form"><option value="KEY">KEY FILINGS</option><option value="ALL">ALL FORMS</option>${forms.map(f=>`<option>${esc(f)}</option>`).join('')}</select></div><section id="filing-rows" class="filing-list"></section><div class="research-disclosure">Facts are standardized SEC XBRL disclosures. Periods, taxonomy choices, amendments, and units can vary by registrant; open the source filing before using a value.</div>`;
  $('#filing-filter').onchange=e=>renderFilings(e.target.value);renderFilings('KEY');renderResearchNews();if(ticker!=='—')chart(exchange+':'+ticker,'#research-chart');
}
async function runEdgar(symbol){
  const query=String(symbol||'').trim().toUpperCase();if(!query)return;
  $('#edgar-symbol').value=query;$('#edgar-status').textContent='LOADING SEC…';$('#edgar-status').className='loading';
  $('#edgar-result').innerHTML='<div class="empty">Retrieving company submissions and XBRL facts from SEC EDGAR…</div>';
  try{const response=await fetch('/api/company?symbol='+encodeURIComponent(query)),value=await response.json();if(!response.ok)throw new Error(value.error||'Research request failed.');renderEdgar(value);$('#edgar-status').textContent='SEC CONNECTED';$('#edgar-status').className='ok'}catch(err){edgarData=null;$('#edgar-status').textContent='REQUEST FAILED';$('#edgar-status').className='error';$('#edgar-result').innerHTML=`<div class="empty error-text">${esc(err.message)}</div>`}
}
$('#edgar-search').onsubmit=e=>{e.preventDefault();runEdgar(new FormData(e.target).get('symbol'))};
$('#edgar-result').onclick=e=>{const b=e.target.closest('[data-research-article]');if(b)openReader(b.dataset.researchArticle)};

async function loadSettings(){const response=await fetch('/api/settings');const value=await response.json();csrf=value.csrf||'';readOnly=!!value.read_only;delete value.csrf;delete value.read_only;settings=value;document.body.classList.toggle('read-only',readOnly);if(!settings.watchlist.some(w=>w.symbol===linked)&&settings.watchlist[0])linked=settings.watchlist[0].symbol;renderSettings();renderWatchlist();renderLinkedNews();renderMorning()}
async function saveSettings(patch){
  if(readOnly)throw new Error('Read-only public view. Run your own copy from github.com/L8rMr/signal-terminal to edit feeds, watchlists and alerts.');
  const response=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json','X-Signal-Token':csrf},body:JSON.stringify({revision:settings.revision,...patch})});const value=await response.json();
  if(!response.ok)throw new Error(value.error||'Could not save settings.');settings=value;renderSettings();renderWatchlist();renderLinkedNews();renderMorning();return value;
}
function editDialog(title,fields,onSave,ready){
  $('#editor-title').textContent=title;$('#editor-fields').innerHTML=fields;$('#editor-error').textContent='';const dialog=$('#editor');dialog.showModal();
  if(ready)ready(dialog);
  $('#editor-save').onclick=async e=>{e.preventDefault();try{await onSave(new FormData(dialog.querySelector('form')));dialog.close()}catch(err){$('#editor-error').textContent=err.message}};
}
function symbolEditor(index=null){const w=index===null?{symbol:'NASDAQ:',name:'',aliases:''}:settings.watchlist[index];editDialog(index===null?'ADD WATCHLIST SYMBOL':'EDIT WATCHLIST SYMBOL',`<label>TRADINGVIEW SYMBOL<input name="symbol" value="${esc(w.symbol)}" placeholder="NASDAQ:AAPL" required></label><label>DISPLAY NAME<input name="name" value="${esc(w.name)}" placeholder="Apple" required></label><label>NEWS ALIASES<input name="aliases" value="${esc(w.aliases)}" placeholder="Apple, iPhone"></label>`,async f=>{const rows=settings.watchlist.slice(),value={symbol:f.get('symbol'),name:f.get('name'),aliases:f.get('aliases')};if(index===null)rows.push(value);else rows[index]=value;await saveSettings({watchlist:rows});selectSymbol(value.symbol.toUpperCase())})}
function feedEditor(index=null){const f=index===null?{name:'',url:'https://',category:'Markets',enabled:true}:settings.feeds[index];editDialog(index===null?'ADD NEWS SOURCE':'EDIT NEWS SOURCE',`<label>SOURCE NAME<input name="name" value="${esc(f.name)}" required></label><label>PUBLIC HTTPS RSS / ATOM URL<input name="url" type="url" value="${esc(f.url)}" required></label><label>CATEGORY<select name="category">${['Markets','Business','World','Policy','Technology'].map(c=>`<option ${c===f.category?'selected':''}>${c}</option>`).join('')}</select></label><label><input name="enabled" type="checkbox" ${f.enabled?'checked':''}> ENABLED</label>`,async form=>{const rows=settings.feeds.slice(),value={...(index===null?{}:f),name:form.get('name'),url:form.get('url'),category:form.get('category'),enabled:form.has('enabled')};if(index===null)rows.push(value);else rows[index]=value;await saveSettings({feeds:rows})})}
function ruleEditor(index=null){const r=index===null?{name:'',terms:'',kind:'keyword',enabled:true}:settings.rules[index];const signals=briefing?.signal_catalog||[];editDialog(index===null?'NEW ALERT RULE':'EDIT ALERT RULE',`<label>RULE NAME<input name="name" value="${esc(r.name)}" placeholder="Fed policy" required></label><label>MATCH TYPE<select name="kind"><option value="keyword" ${r.kind==='keyword'?'selected':''}>Keywords</option><option value="watchlist" ${r.kind==='watchlist'?'selected':''}>Entire watchlist</option><option value="brief" ${r.kind==='brief'?'selected':''}>Market condition (from the brief)</option></select></label><label class="rule-signal">MARKET CONDITION<select name="signal">${signals.map(x=>`<option value="${esc(x.id)}" ${r.kind==='brief'&&r.terms===x.id?'selected':''}>${esc(x.label)}</option>`).join('')}</select></label><label class="rule-terms">KEYWORDS, COMMA SEPARATED<input name="terms" value="${esc(r.kind==='brief'?'':r.terms)}" placeholder="FOMC, rate decision, Powell"></label><label><input name="enabled" type="checkbox" ${r.enabled?'checked':''}> ENABLED</label>`,async form=>{const rows=settings.rules.slice(),kind=form.get('kind'),value={...(index===null?{}:r),name:form.get('name'),kind,terms:kind==='brief'?form.get('signal'):form.get('terms'),enabled:form.has('enabled')};if(index===null)rows.push(value);else rows[index]=value;await saveSettings({rules:rows})},dialog=>{
    const kind=dialog.querySelector('[name=kind]'),sync=()=>{
      dialog.querySelector('.rule-signal').hidden=kind.value!=='brief';
      dialog.querySelector('.rule-terms').hidden=kind.value!=='keyword';
    };kind.onchange=sync;sync();
  })}
$('#add-symbol').onclick=()=>symbolEditor();$('#add-feed').onclick=()=>feedEditor();$('#add-rule').onclick=()=>ruleEditor();
$('#feeds').onclick=e=>{const edit=e.target.closest('[data-edit-feed]'),del=e.target.closest('[data-delete-feed]');if(edit)feedEditor(Number(edit.dataset.editFeed));if(del)saveSettings({feeds:settings.feeds.filter((_,i)=>i!==Number(del.dataset.deleteFeed))}).catch(err=>alert(err.message))};
$('#rules').onclick=e=>{const edit=e.target.closest('[data-edit-rule]'),del=e.target.closest('[data-delete-rule]');if(edit)ruleEditor(Number(edit.dataset.editRule));if(del)saveSettings({rules:settings.rules.filter((_,i)=>i!==Number(del.dataset.deleteRule))}).catch(err=>alert(err.message))};
$('#stream-setup').onclick=()=>alert('A paid Benzinga News Stream subscription and API key are required. The free RSS/Atom sources remain active without it.');

function applyPayload(next){
  if(next.type==='delta'){
    const byId=new Map(data.articles.map(a=>[a.id,a]));
    (next.removed||[]).forEach(id=>byId.delete(id));
    (next.added||[]).forEach(a=>byId.set(a.id,a));
    const articles=[...byId.values()].sort((a,b)=>(b.published||0)-(a.published||0)).slice(0,600);
    return {...data,...next,articles};
  }
  return {...data,...next};
}
function receive(payload){const next=applyPayload(payload);if(paused){pending=next;return}const prior=data.alerts?.length||0;data=next;
  data.economy=(data.economy||[]).map(x=>({...x,history:economyHistory[x.series]||x.history||[]}));
  if(data.economy.length&&data.economy_updated!==economyStamp){economyStamp=data.economy_updated;loadEconomy()}const source=$('#source').value;$('#source').innerHTML='<option value="All">All sources</option>'+[...new Set(data.sources.map(s=>s.name).concat(data.articles.map(a=>a.source)))].map(s=>`<option>${esc(s)}</option>`).join('');if([...$('#source').options].some(o=>o.value===source))$('#source').value=source;renderNews();renderSources();renderAlerts();renderRates('#rates-board');renderFred();renderMorning();renderLinkedNews();renderResearchNews();$('#updated').textContent=data.updated?`Checked ${stamp(data.updated)} · ${data.sources.filter(s=>s.ok).length} sources online`:'Waiting for first update';$('#cadence').textContent=`AUTO UPDATE / ${data.interval||30}s`;$('#stream-status').textContent=data.stream?.status||'OPTIONAL';if(briefing&&data.updated>briefing.generated_at&&!briefingLoading)briefButtons().forEach(button=>{button.textContent='NEW DATA — REFRESH';button.classList.add('refresh-available')});
  if(initialized&&data.alerts.length>prior&&'Notification' in window&&Notification.permission==='granted'){data.alerts.slice(0,data.alerts.length-prior).forEach(a=>new Notification(`Signal · ${a.rule}`,{body:a.article.title,tag:a.id}))}initialized=true;
  if(!selected&&data.articles.length)storyDetail(data.articles[0]);
}

function tv(target,src,config){const el=$(target);el.innerHTML='<div class="tradingview-widget-container" style="height:100%;width:100%"><div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div></div>';const script=document.createElement('script');script.src=src;script.async=true;script.textContent=JSON.stringify(config);el.firstChild.appendChild(script)}
function chart(symbol,target){tv(target,'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js',{autosize:true,symbol,interval:'15',timezone:'America/New_York',theme:'dark',style:'1',locale:'en',backgroundColor:'rgba(16,20,25,1)',gridColor:'rgba(41,49,58,.4)',allow_symbol_change:true,hide_side_toolbar:true,calendar:false,support_host:'https://www.tradingview.com'})}
function marketValue(item){
  if(item.code==='US10Y')return `${item.value.toFixed(3)}%`;
  if(item.code==='EURUSD')return item.value.toFixed(4);
  if(item.code==='USDJPY')return item.value.toFixed(2);
  return item.value.toLocaleString('en-US',{minimumFractionDigits:item.value>=1000?0:2,maximumFractionDigits:item.value>=1000?0:2});
}
function marketMove(value){if(value===null||value===undefined)return '<span class="flat">—</span>';const cls=value>0?'up':value<0?'down':'flat';return `<span class="${cls}">${value>0?'▲':value<0?'▼':'•'} ${Math.abs(value).toFixed(2)}%</span>`}
function renderMarketSnapshot(snapshot){
  const target=$('#macro-widget'),items=snapshot.items||[],groups=['Equities','Rates & FX','Commodities & Vol'];
  if(!items.length){target.innerHTML='<div class="empty">No cross-asset observations are available.</div>';return}
  const winners=[...items].sort((a,b)=>b.one_day-a.one_day),leader=winners[0],laggard=winners.at(-1),maxMove=Math.max(1,...items.map(x=>Math.abs(x.one_day||0)));
  target.dataset.loaded='true';
  target.innerHTML=`<div class="macro-summary"><div><span>LARGEST RISER / 1D</span><strong>${esc(leader.code)}</strong>${marketMove(leader.one_day)}</div><div><span>LARGEST LAGGARD / 1D</span><strong>${esc(laggard.code)}</strong>${marketMove(laggard.one_day)}</div><div><span>COVERAGE</span><strong>${items.length} / 13</strong><small>${snapshot.unavailable?.length?esc(snapshot.unavailable.join(', ')+' unavailable'):'ALL SYMBOLS ONLINE'}</small></div></div><div class="move-explainer"><strong>HOW TO READ THE BAR</strong><span><i class="loss"></i> Losses extend left</span><span><b>0</b> Unchanged</span><span><i class="gain"></i> Gains extend right</span><small>Bar length compares each market with the largest one-day move shown here.</small></div><div class="macro-table"><div class="macro-columns"><span>MARKET</span><span>LAST</span><span>1 DAY</span><span>5 DAY</span><span>1 MONTH</span><span class="move-heading"><b>LOSSES ←</b><em>1D COMPARISON</em><b>→ GAINS</b></span></div>${groups.map(group=>`<section><h2>${esc(group)}</h2>${items.filter(x=>x.group===group).map(item=>{const width=Math.abs(item.one_day||0)/maxMove*50,left=item.one_day>=0?50:50-width;return `<div class="macro-row"><div><b>${esc(item.code)}</b><span>${esc(item.name)}</span><small>${esc(item.exchange)}</small></div><strong>${esc(marketValue(item))}</strong>${marketMove(item.one_day)}${marketMove(item.five_day)}${marketMove(item.one_month)}<div class="move-track" aria-label="${esc(item.code)} moved ${item.one_day}% in one day, shown relative to the other markets"><i class="${item.one_day>=0?'positive':'negative'}" style="left:${left}%;width:${width}%"></i></div></div>`}).join('')}</section>`).join('')}</div>`;
  $('#macro-asof').textContent=`AS OF ${full(snapshot.as_of).toUpperCase()}${snapshot.stale?' · CACHED':''}`;
}
async function marketOverview(){
  const target=$('#macro-widget'),button=$('#refresh-macro');button.disabled=true;button.textContent='LOADING…';
  if(!target.dataset.loaded)target.innerHTML='<div class="empty">Loading values and multi-period performance…</div>';
  try{const response=await fetch('/api/markets'),snapshot=await response.json();if(!response.ok)throw new Error(snapshot.error||'Market snapshot could not be loaded.');renderMarketSnapshot(snapshot)}catch(err){if(!target.dataset.loaded)target.innerHTML=`<div class="empty error-text">${esc(err.message)}</div>`}finally{button.disabled=false;button.textContent='REFRESH'}
}
$('#refresh-macro').onclick=()=>marketOverview();
function economicCalendar(){tv('#calendar-widget','https://s3.tradingview.com/external-embedding/embed-widget-events.js',{colorTheme:'dark',isTransparent:false,width:'100%',height:'100%',locale:'en',importanceFilter:'0,1',countryFilter:'us,eu,gb,jp,cn'})}

function executeCommand(raw){
  const q=String(raw||'').trim().toUpperCase(),views={BRIEF:'brief',ANLY:'analysis',ANALYSIS:'analysis',HOME:'brief',TOP:'news',EDGAR:'research',GMM:'macro',BTMM:'rates',FRED:'fred',MON:'watch',ALRT:'alerts',SET:'settings'},indexes={NDX:'NASDAQ:NDX',SPX:'SP:SPX',DJI:'TVC:DJI',VIX:'TVC:VIX',DXY:'TVC:DXY',BTCUSD:'COINBASE:BTCUSD'};
  if(!q)return;
  if(q.startsWith('EDGAR ')){openView('research');runEdgar(q.slice(6).trim());return}
  if(views[q]){openView(views[q]);if(q==='EDGAR')$('#edgar-symbol').focus();return}
  if(indexes[q]){selectSymbol(indexes[q]);openView('watch');return}
  if(/^\d{1,10}$/.test(q)||/^[A-Z][A-Z0-9.-]{0,9}$/.test(q)){openView('research');runEdgar(q);return}
  $('#category').value='All';openView('news');const match=data.articles.find(a=>`${a.title} ${a.summary}`.toUpperCase().includes(q));if(match)storyDetail(match);
}
$('#command').addEventListener('keydown',e=>{if(e.key!=='Enter')return;executeCommand(e.target.value);e.target.value=''});
$('.quick-commands').onclick=e=>{const b=e.target.closest('[data-command]');if(b)executeCommand(b.dataset.command)};
window.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){e.preventDefault();$('#command').focus()}if(e.key==='Escape'){$('#command').value='';$('#command').blur()}});
function tick(){const now=new Date();$('#clock').textContent=now.toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:false});$('#date').textContent=now.toLocaleDateString('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',year:'numeric'}).toUpperCase()}tick();setInterval(tick,1000);
const stream=new EventSource('/api/events');stream.onopen=()=>{$('#connection').textContent='● LIVE SESSION';$('#connection').className='connection online'};stream.onerror=()=>{$('#connection').textContent='RECONNECTING';$('#connection').className='connection'};stream.onmessage=e=>{try{receive(JSON.parse(e.data))}catch(err){console.error(err)}};
async function loadEconomy(){try{const r=await fetch('/api/economy'),v=await r.json();if(r.ok){economyHistory={};(v.economy||[]).forEach(x=>{economyHistory[x.series]=x.history});data.economy=(data.economy||[]).map(x=>({...x,history:economyHistory[x.series]||[]}));renderFred()}}catch(err){console.error(err)}}
loadSettings().then(()=>{chart(linked,'#chart');loadBriefing()}).catch(err=>{console.error(err);$('#connection').textContent='SETTINGS ERROR'});
