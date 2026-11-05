/*
 * STRM Indexer · Solicitar pela busca do Jellyfin
 * Cole este script no plugin "JavaScript Injector" (Painel → Plugins → JavaScript Injector).
 *
 * Ajuste API abaixo:
 *   - '/strm-api'                 → indexador publicado no MESMO domínio do Jellyfin pelo proxy (recomendado)
 *   - 'https://strm.seudominio/jf' → outro domínio (exige INDEXER_JF_ORIGINS no indexador)
 *
 * Usa o token do próprio usuário logado; nenhum segredo fica neste script.
 */
(function () {
  'use strict';
  const API = '/strm-api';
  const DEBOUNCE = 450;

  if (window.__strmIndexerBridge) return;
  window.__strmIndexerBridge = true;

  /* ---------------- token do usuário logado ---------------- */
  function token() {
    try {
      const c = window.ApiClient;
      if (c && typeof c.accessToken === 'function' && c.accessToken()) return c.accessToken();
      if (c && c._serverInfo && c._serverInfo.AccessToken) return c._serverInfo.AccessToken;
    } catch (e) { /* segue */ }
    try {
      const creds = JSON.parse(localStorage.getItem('jellyfin_credentials') || '{}');
      const s = (creds.Servers || []).find(x => x.AccessToken);
      if (s) return s.AccessToken;
    } catch (e) { /* segue */ }
    return '';
  }

  async function call(path, opts = {}) {
    const res = await fetch(API + path, {
      method: opts.method || 'GET',
      headers: Object.assign({ 'X-Emby-Token': token() }, opts.body ? { 'Content-Type': 'application/json' } : {}),
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: 'no-store'
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }

  /* ---------------- estilos ---------------- */
  const css = `
  .strmb{margin:1.2em 0 2em;padding:0 3.3%}
  .strmb-head{display:flex;align-items:center;gap:.6em;margin:0 0 .8em}
  .strmb-head h2{margin:0;font-size:1.25em;font-weight:600}
  .strmb-badge{font-size:.7em;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:.25em .6em;border-radius:99px;background:linear-gradient(135deg,#8b7cff,#ff8fc7);color:#fff}
  .strmb-sub{opacity:.6;font-size:.85em}
  .strmb-row{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:1.1em .9em}
  .strmb-card{display:flex;flex-direction:column;gap:.45em;min-width:0}
  .strmb-poster{position:relative;aspect-ratio:2/3;border-radius:.5em;overflow:hidden;background:linear-gradient(160deg,#252a3a,#12151f);box-shadow:0 .5em 1.5em rgba(0,0,0,.45)}
  .strmb-poster img{position:absolute;inset:0;z-index:1;width:100%;height:100%;object-fit:cover;display:block}
  .strmb-type{z-index:2}
  .strmb-ini{position:absolute;inset:0;display:grid;place-items:center;font-size:2.4em;font-weight:800;color:#a796ff;opacity:.85}
  .strmb-type{position:absolute;top:.5em;left:.5em;font-size:.68em;font-weight:700;padding:.25em .55em;border-radius:99px;background:rgba(0,0,0,.65);color:#fff;backdrop-filter:blur(6px)}
  .strmb-title{font-weight:600;font-size:.92em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .strmb-meta{font-size:.78em;opacity:.6;margin-top:-.3em}
  .strmb-actions{display:flex;gap:.4em}
  .strmb-actions select{flex:0 0 auto;max-width:5.5em;background:#1d2130;color:#fff;border:1px solid rgba(255,255,255,.15);border-radius:.4em;font-size:.8em;padding:0 .3em}
  .strmb-btn{flex:1;border:0;border-radius:.45em;padding:.6em .5em;font-weight:700;font-size:.82em;cursor:pointer;color:#fff;background:linear-gradient(135deg,#8b7cff,#b77dff);transition:filter .2s,opacity .2s}
  .strmb-btn:hover{filter:brightness(1.1)}
  .strmb-btn:disabled{cursor:default;opacity:.85;filter:none}
  .strmb-btn.wait{background:#3a3f55}
  .strmb-btn.ok{background:#1f8a5b}
  .strmb-btn.bad{background:#8a2f3d}
  .strmb-msg{font-size:.74em;opacity:.75;line-height:1.3;min-height:1em}
  .strmb-empty{opacity:.6;font-size:.9em}
  `;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  /* ---------------- utilidades DOM ---------------- */
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }
  const visible = n => n && n.offsetParent !== null;

  function isSearchRoute() { return /search/i.test(location.hash + location.pathname); }

  function currentQuery() {
    const inputs = document.querySelectorAll('input[type="search"], .searchfields input, #searchTextInput, input.searchfields-txtSearch');
    for (const i of inputs) if (visible(i) && i.value.trim()) return i.value.trim();
    const qs = (location.hash.split('?')[1] || '') + '&' + location.search.slice(1);
    return (new URLSearchParams(qs).get('query') || '').trim();
  }

  function anchor() {
    const sels = ['.searchResults', '#searchPage .padded-bottom-page', '#searchPage', '.searchPage',
      '.mainAnimatedPage:not(.hide)', '.page:not(.hide)', 'main'];
    for (const s of sels) {
      const list = document.querySelectorAll(s);
      for (const n of list) if (visible(n)) return n;
    }
    return null;
  }

  /* ---------------- renderização ---------------- */
  let section = null;
  function ensureSection() {
    const host = anchor();
    if (!host) return null;
    // Fica ANTES da lista de resultados (fora dela), para sobreviver às re-renderizações do Jellyfin.
    const isResults = host.classList.contains('searchResults');
    const parent = isResults ? host.parentElement : host;
    if (!section || !document.contains(section) || section.parentElement !== parent) {
      section && section.remove();
      section = el('section', 'strmb');
      if (isResults) parent.insertBefore(section, host); else parent.prepend(section);
    }
    return section;
  }

  function initials(t) {
    const w = String(t).replace(/[^\p{L}\p{N} ]/gu, ' ').split(/\s+/).filter(Boolean);
    return ((w[0] || '')[0] || '') + ((w[1] || '')[0] || '');
  }

  function card(it) {
    const c = el('div', 'strmb-card');
    const p = el('div', 'strmb-poster');
    p.appendChild(el('span', 'strmb-ini', initials(it.title).toUpperCase()));
    if (it.poster) {
      const img = el('img'); img.loading = 'lazy'; img.alt = it.title;
      img.onerror = () => img.remove();
      img.src = API + '/' + it.poster;
      p.appendChild(img);
    }
    p.appendChild(el('span', 'strmb-type', it.kind === 'movie' ? 'Filme' : 'Série'));
    c.appendChild(p);
    c.appendChild(el('div', 'strmb-title', it.title)).title = it.title;
    const meta = it.kind === 'movie'
      ? [it.year || '', it.sources > 1 ? it.sources + ' fontes' : ''].filter(Boolean).join(' · ')
      : [it.seasons.length + (it.seasons.length === 1 ? ' temporada' : ' temporadas'),
         it.in_library ? it.in_library + '/' + it.episodes + ' ep. no Jellyfin' : it.episodes + ' ep.'].join(' · ');
    c.appendChild(el('div', 'strmb-meta', meta));

    const actions = el('div', 'strmb-actions');
    let seasonSel = null;
    if (it.kind === 'series' && it.seasons.length > 1) {
      seasonSel = el('select');
      seasonSel.title = 'Temporada';
      seasonSel.appendChild(new Option('Todas', ''));
      it.seasons.forEach(s => seasonSel.appendChild(new Option('T' + s, String(s))));
      actions.appendChild(seasonSel);
    }
    const btn = el('button', 'strmb-btn', it.requested ? 'Em andamento' : 'Solicitar');
    btn.type = 'button';
    if (it.requested) { btn.disabled = true; btn.classList.add('wait'); }
    const msg = el('div', 'strmb-msg');
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.className = 'strmb-btn wait'; btn.textContent = 'Enviando…';
      try {
        const body = { kind: it.kind, title: it.title };
        if (it.kind === 'movie' && it.catalog_year) body.year = it.catalog_year;
        if (seasonSel && seasonSel.value !== '') body.season = Number(seasonSel.value);
        else if (it.kind === 'series' && it.seasons.length === 1) body.season = it.seasons[0];
        const r = await call('/request', { method: 'POST', body });
        btn.textContent = 'Validando…';
        follow(r.id, btn, msg);
      } catch (e) {
        btn.disabled = false; btn.className = 'strmb-btn'; btn.textContent = 'Solicitar';
        msg.textContent = e.message;
      }
    });
    actions.appendChild(btn);
    c.appendChild(actions);
    c.appendChild(msg);
    return c;
  }

  function follow(id, btn, msg) {
    const labels = { queued: 'Na fila…', validating: 'Validando…', importing: 'Importando…' };
    const tick = async () => {
      try {
        const s = await call('/status?id=' + encodeURIComponent(id));
        msg.textContent = s.message || '';
        if (labels[s.state]) { btn.textContent = labels[s.state]; return setTimeout(tick, 3000); }
        if (s.state === 'available') { btn.className = 'strmb-btn ok'; btn.textContent = '✓ Adicionado'; }
        else { btn.className = 'strmb-btn bad'; btn.textContent = 'Sem fonte agora'; }
      } catch (e) { msg.textContent = e.message; }
    };
    tick();
  }

  async function render(q) {
    const sec = ensureSection();
    if (!sec) return;
    if (!q || q.length < 2) { sec.replaceChildren(); return; }
    let data;
    try { data = await call('/search?q=' + encodeURIComponent(q)); }
    catch (e) { console.warn('[STRM Indexer]', e.message); sec.replaceChildren(); return; }
    if (q !== lastQuery) return; // usuário já digitou outra coisa
    sec.replaceChildren();
    if (!data.items.length) return;
    const head = el('div', 'strmb-head');
    head.appendChild(el('h2', null, 'Disponível para solicitar'));
    head.appendChild(el('span', 'strmb-badge', 'STRM'));
    head.appendChild(el('span', 'strmb-sub', data.items.length + ' resultado(s) ainda fora da biblioteca'));
    sec.appendChild(head);
    const row = el('div', 'strmb-row');
    data.items.forEach(it => row.appendChild(card(it)));
    sec.appendChild(row);
  }

  /* ---------------- observa a página de busca ---------------- */
  let lastQuery = null, timer = null;
  function check() {
    if (!isSearchRoute()) {
      if (section) { section.remove(); section = null; }
      lastQuery = null;
      return;
    }
    const q = currentQuery();
    if (q === lastQuery && section && document.contains(section)) return;
    lastQuery = q;
    clearTimeout(timer);
    timer = setTimeout(() => render(q), DEBOUNCE);
  }
  document.addEventListener('input', e => { if (e.target && e.target.matches && e.target.matches('input')) setTimeout(check, 50); }, true);
  window.addEventListener('hashchange', check);
  window.addEventListener('popstate', check);
  setInterval(check, 800);
})();
