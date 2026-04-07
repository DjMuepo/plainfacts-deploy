(function(){
  const API_BASE = (window.API_BASE || `${location.origin}/api`).replace(/\/$/, "");

  function qs(sel, root=document){ return root.querySelector(sel); }
  function qsa(sel, root=document){ return Array.from(root.querySelectorAll(sel)); }
  function esc(v){
    return (v ?? "").toString()
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#39;");
  }

  async function jget(path){
    const res = await fetch(`${API_BASE}${path}`);
    const ct = res.headers.get("content-type") || "";
    const text = await res.text();
    if (!res.ok) throw new Error(text || `HTTP ${res.status}`);
    if (!ct.includes("application/json")) throw new Error("API returned HTML instead of JSON.");
    return JSON.parse(text);
  }

  function inferCategory(item){
    const hay = `${item.topic_title || ""} ${item.what || ""} ${item.sources?.[0]?.raw?.category || ""}`.toLowerCase();
    if (/(inflation|stock|market|economy|etf|crypto|fed|tariff|jobs|price)/.test(hay)) return "economy";
    if (/(congress|faa|fda|sec|u\.s\.|united states|supreme court|senate|house|california|texas|new york)/.test(hay)) return "domestic";
    return "global";
  }

  function imgHtml(item){
    const src = item.best_image || item.image_url || "";
    return src ? `<img src="${esc(src)}" alt="">` : "";
  }

  function summary(item){
    return item.plain_summary || item.what || "No summary available yet.";
  }

  function evidenceCount(item){ return (item.evidence_links || []).length; }
  function sourceCount(item){ return (item.sources || []).length; }
  function importance(item){ return Math.round(((item.confidence || 0) * 100) + evidenceCount(item) * 4 + sourceCount(item) * 2); }

  function cardHtml(item, withImage=true){
    return `
      <article class="card" data-item='${esc(JSON.stringify(item))}'>
        ${withImage ? `<div class="cardimg">${imgHtml(item)}</div>` : ""}
        <div class="cardmetaRow">
          <span class="chip subtle">${esc(inferCategory(item))}</span>
          <span class="score">Importance ${importance(item)}</span>
        </div>
        <div class="cardtitle">${esc(item.topic_title || "Untitled")}</div>
        <p class="cardsummary">${esc(summary(item))}</p>
        <div class="meta">${esc(item.source_summary || `${sourceCount(item)} sources • ${evidenceCount(item)} evidence links`)}</div>
      </article>
    `;
  }

  function openModal(item){
    const modal = qs("#modal");
    const body = qs("#modalBody");
    if (!modal || !body) return;
    const evidence = (item.evidence_links || []).slice(0, 8).map(u => `<li><a target="_blank" rel="noreferrer" href="${esc(u)}">${esc(u)}</a></li>`).join("");
    const sourceList = (item.sources || []).slice(0, 8).map(s => `<li><a target="_blank" rel="noreferrer" href="${esc(s.url || '#')}">${esc(s.source || 'Source')}</a> — ${esc(s.title || '')}</li>`).join("");
    const unknowns = (item.unknowns || []).slice(0, 8).map(u => `<li>${esc(u)}</li>`).join("");
    body.innerHTML = `
      <button class="modalClose" id="modalCloseBtn">✕</button>
      <h2 style="margin:0 0 8px 0;padding-right:42px;">${esc(item.topic_title || "Details")}</h2>
      <div class="toolbar" style="margin:0 0 12px 0;">
        <span class="chip">Category: ${esc(inferCategory(item))}</span>
        <span class="chip">Importance: ${importance(item)}</span>
        <span class="chip">Confidence: ${Math.round((item.confidence || 0) * 100)}%</span>
      </div>
      <p class="small" style="margin:0 0 10px 0;">${esc(summary(item))}</p>
      <div class="toolbar" style="margin:12px 0;">
        <span class="chip">Who: ${esc(item.who || "—")}</span>
        <span class="chip">Where: ${esc(item.where || "—")}</span>
        <span class="chip">When: ${esc(item.when || "—")}</span>
      </div>
      ${sourceList ? `<h3>Sources</h3><ul>${sourceList}</ul>` : ""}
      ${evidence ? `<h3>Evidence</h3><ul>${evidence}</ul>` : ""}
      ${unknowns ? `<h3>What we don't know yet</h3><ul>${unknowns}</ul>` : ""}
    `;
    modal.hidden = false;
    qs("#modalCloseBtn").onclick = () => modal.hidden = true;
    qs(".modalBack", modal).onclick = () => modal.hidden = true;
  }

  function wireCards(){
    qsa(".card").forEach(el => {
      el.onclick = () => {
        const item = JSON.parse(el.dataset.item);
        openModal(item);
      };
    });
  }

  function renderCardList(slot, items, withTopImages=3){
    slot.innerHTML = items.map((x,i)=> cardHtml(x, i < withTopImages)).join("");
    wireCards();
  }

  async function loadHome(){
    const queries = { global: "world diplomacy conflict climate", domestic: "united states congress agencies courts", economy: "inflation jobs fed market tariffs" };
    for (const [section, q] of Object.entries(queries)){
      const slot = qs(`#${section}Top`);
      if (!slot) continue;
      try{
        const items = await jget(`/briefs?q=${encodeURIComponent(q)}&max_clusters=6`);
        const filtered = items.filter(x => inferCategory(x) === section || section === "global").slice(0,3);
        renderCardList(slot, filtered.length ? filtered : items.slice(0,3), 3);
      }catch(e){
        slot.innerHTML = `<div class="small">Could not load: ${esc(e.message)}</div>`;
      }
    }
  }

  async function loadSection(section){
    const map = {
      global: "world diplomacy conflict climate elections",
      domestic: "united states congress agencies court policy regulation",
      economy: "inflation jobs fed tariffs stocks market crypto",
    };
    const q = map[section] || section;
    const slot = qs("#list");
    if (!slot) return;
    try{
      const items = await jget(`/briefs?q=${encodeURIComponent(q)}&max_clusters=18`);
      const filtered = items.filter(x => inferCategory(x) === section || section === "global");
      const sorted = (filtered.length ? filtered : items).sort((a,b)=> importance(b) - importance(a));
      renderCardList(slot, sorted, 3);
    }catch(e){
      slot.innerHTML = `<div class="small">Could not load: ${esc(e.message)}</div>`;
    }
  }

  async function sparkline(symbol){
    try{
      const data = await jget(`/markets/series?symbol=${encodeURIComponent(symbol)}&days=20`);
      const pts = (data.points || []).map(p => p.close).filter(v => typeof v === 'number');
      if (!pts.length) return '';
      const min = Math.min(...pts), max = Math.max(...pts);
      const coords = pts.map((v,i) => {
        const x = (i/(pts.length-1||1))*100;
        const y = 28 - (((v-min)/((max-min)||1))*24);
        return `${x},${y}`;
      }).join(' ');
      return `<svg viewBox="0 0 100 30" preserveAspectRatio="none"><polyline fill="none" stroke="currentColor" stroke-width="2" points="${coords}"/></svg>`;
    }catch(_e){ return ''; }
  }

  async function loadMarkets(){
    const sections = [
      ["#liveFeed","market news", null],
      ["#usStocks","stocks", "stocks"],
      ["#crypto","crypto", "crypto"],
    ];
    for (const [selector,q,kind] of sections){
      const slot = qs(selector);
      if (!slot) continue;
      try{
        if (kind){
          const items = await jget(`/markets/top?kind=${encodeURIComponent(kind)}&limit=6`);
          const rows = await Promise.all(items.map(async (x) => {
            const spark = await sparkline(x.symbol);
            return `
              <div class="marketItem">
                <div>
                  <div style="font-weight:800">${esc(x.symbol)}</div>
                  <div class="small">${esc(x.date || '')} • ${x.close ?? '—'} ${x.change_pct != null ? `(${x.change_pct.toFixed(2)}%)` : ''}</div>
                </div>
                <div class="spark">${spark}</div>
              </div>`;
          }));
          slot.innerHTML = rows.join('');
        } else {
          const items = await jget(`/briefs?q=${encodeURIComponent(q)}&max_clusters=5`);
          slot.innerHTML = items.map((x)=>`
            <div class="marketItem">
              <div>
                <div style="font-weight:800">${esc(x.topic_title || "Update")}</div>
                <div class="small">${esc(summary(x).slice(0,100))}</div>
              </div>
              <div class="spark"></div>
            </div>`).join("");
        }
      }catch(e){
        slot.innerHTML = `<div class="small">Could not load: ${esc(e.message)}</div>`;
      }
    }
  }

  function bindSearch(){
    const btn = qs("#goBtn");
    const input = qs("#searchInput");
    const mode = qs("#searchMode");
    if (!btn || !input || !mode) return;
    const run = ()=>{
      const q = input.value.trim();
      if (!q) return;
      const m = mode.value;
      const url = m === "markets" ? `/markets.html?q=${encodeURIComponent(q)}` : `/search.html?q=${encodeURIComponent(q)}`;
      location.href = url;
    };
    btn.onclick = run;
    input.addEventListener('keydown', (e)=>{ if (e.key === 'Enter') run(); });
  }

  async function loadSearch(){
    const params = new URLSearchParams(location.search);
    const q = params.get("q") || "";
    const input = qs("#searchInput");
    if (input) input.value = q;
    const slot = qs("#list");
    if (!slot || !q) return;
    try{
      const items = await jget(`/briefs?q=${encodeURIComponent(q)}&max_clusters=18`);
      const sorted = items.sort((a,b)=> importance(b) - importance(a));
      renderCardList(slot, sorted, 3);
      const resultMeta = qs('#resultMeta');
      if (resultMeta) resultMeta.textContent = `${sorted.length} results for “${q}”`;
    }catch(e){
      slot.innerHTML = `<div class="small">Could not load: ${esc(e.message)}</div>`;
    }
  }

  function setupStatus(){
    const el = qs("#statusLine");
    if (!el) return;
    jget("/health").then(data => {
      el.textContent = `API OK • v${data.version || ""}`;
    }).catch(() => {
      el.textContent = "API unavailable.";
    });
  }

  document.addEventListener("DOMContentLoaded", async ()=>{
    setupStatus();
    bindSearch();
    const page = document.body.dataset.page || "home";
    if (page === "home") await loadHome();
    if (page === "global" || page === "domestic" || page === "economy") await loadSection(page);
    if (page === "markets") await loadMarkets();
    if (page === "search") await loadSearch();
  });
})();
