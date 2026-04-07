const DEFAULT_API = 'http://localhost:8000';
const q = document.getElementById('query');
const go = document.getElementById('go');
const results = document.getElementById('results');
const status = document.getElementById('status');
function esc(v){return (v||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
async function getApi(){
  return new Promise(resolve => chrome.storage.sync.get(['plainfactsApi'], data => resolve((data.plainfactsApi || DEFAULT_API).replace(/\/$/, ''))));
}
async function run(){
  const query = q.value.trim();
  if(!query) return;
  results.innerHTML='';
  status.textContent='Loading…';
  try{
    const api = await getApi();
    const res = await fetch(`${api}/briefs?q=${encodeURIComponent(query)}&max_clusters=6`);
    const data = await res.json();
    results.innerHTML = data.map(item => `<div class="card"><div class="title">${esc(item.topic_title)}</div><div>${esc(item.what || '')}</div><div class="meta">Confidence ${Math.round((item.confidence||0)*100)}%</div></div>`).join('');
    status.textContent = `${data.length} results`;
  }catch(err){
    status.textContent = 'Could not reach PlainFacts API.';
  }
}
go.addEventListener('click', run); q.addEventListener('keydown', e => { if (e.key === 'Enter') run();});
