const f=document.getElementById('f'),k=document.getElementById('k'),msg=document.getElementById('msg');
async function signIn(key){
  msg.className='';msg.textContent='Checking…';
  const r=await fetch('/api/owner/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key}),credentials:'same-origin'});
  if(r.ok){msg.className='ok';msg.textContent='Signed in. Opening the terminal…';setTimeout(()=>location.replace('/'),500)}
  else{msg.className='err';msg.textContent=(await r.json().catch(()=>({}))).error||'Sign-in failed.'}
}
f.addEventListener('submit',e=>{e.preventDefault();signIn(k.value.trim())});
// The owner link carries the key in the #fragment, which browsers never send to the server or logs.
const m=location.hash.match(/key=([^&]+)/);if(m){history.replaceState(null,'','/login');signIn(decodeURIComponent(m[1]))}
