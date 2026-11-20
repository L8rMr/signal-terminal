const $=s=>document.querySelector(s);let csrf='';
function show(s){
  const st=s.schwab;$('#pill').textContent=st.connected?'CONNECTED':st.configured?'NOT SIGNED IN':'NO KEYS';$('#pill').className='pill '+(st.connected?'on':'off');
  $('#statusline').textContent=st.connected?`Connected. Sign-in good until ${new Date(st.refresh_expires*1000).toLocaleString()}.`:st.configured?'Keys saved. Sign in to Schwab to connect.':'Save your Schwab App Key and Secret first.';
  if(st.error)$('#statusline').textContent+=' Last error: '+st.error;
  $('#keys').open=!st.configured;$('#cb').value=st.callback||'https://127.0.0.1';
}
function showFred(f){if(!f)return;$('#fpill').textContent=f.configured?(f.series?`KEY SAVED · ${f.series} SERIES`:'KEY SAVED'):(f.series?`NO KEY · ${f.series} SERIES`:'NO KEY');$('#fpill').className='pill '+(f.series?'on':'off');if(f.from_env)$('#fredline').textContent='The key is set in the server environment (FRED_API_KEY).';if(f.error)$('#fmsg').textContent=f.error}
async function load(){const s=await fetch('/api/owner/setup',{cache:'no-store'}).then(r=>r.json());csrf=s.csrf;$('#link').textContent=s.owner_link||('Owner key: '+s.owner_key);show(s);showFred(s.fred)}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Signal-Token':csrf},body:JSON.stringify(body)});const v=await r.json();if(!r.ok)throw new Error(v.error||'Failed');return v}
function say(t,ok){$('#smsg').textContent=t;$('#smsg').className='msg '+(ok?'ok':'err')}
$('#copy').onclick=async()=>{await navigator.clipboard.writeText($('#link').textContent);$('#copymsg').textContent='Copied.';$('#copymsg').className='msg ok'};
$('#keysform').onsubmit=async e=>{e.preventDefault();try{show(await post('/api/owner/schwab/keys',{app_key:$('#ak').value,app_secret:$('#as').value,callback:$('#cb').value}));$('#as').value='';say('Keys saved. Now sign in.',true)}catch(err){say(err.message)}};
$('#auth').onclick=async()=>{const v=await fetch('/api/owner/schwab/authorize').then(r=>r.json());if(v.error)return say(v.error);window.open(v.url,'_blank','noopener');say('Schwab sign-in opened in a new tab.',true)};
$('#connform').onsubmit=async e=>{e.preventDefault();say('Connecting…',true);try{show(await post('/api/owner/schwab/connect',{url:$('#ru').value}));$('#ru').value='';say('Connected. Open the terminal: the badge should read LIVE · SCHWAB.',true)}catch(err){say(err.message)}};
load();
$('#fredform').onsubmit=async e=>{e.preventDefault();const m=$('#fmsg');try{await post('/api/owner/schwab/fred',{key:$('#fk').value});$('#fk').value='';m.className='msg ok';m.textContent='Saved. Loading economic data now; refresh this page in a minute to see the series count.';setTimeout(load,20000)}catch(err){m.className='msg err';m.textContent=err.message}};
