'use strict';
/* STRM Indexer — painel web.
 * Sem dependências externas e compatível com a CSP do api.py (sem inline script/style).
 * Sessão via cookie HttpOnly; o token CSRF fica apenas em memória.
 */

/* ============================== Utilidades ============================== */
const $ = id => document.getElementById(id);
const fmt = n => Number(n || 0).toLocaleString('pt-BR');
const SVGNS = 'http://www.w3.org/2000/svg';
const pad2 = n => String(n ?? 0).padStart(2, '0');
const epCode = (s, e) => 'S' + pad2(s) + 'E' + pad2(e);
const sleep = ms => new Promise(r => setTimeout(r, ms));

function icon(name) {
  const svg = document.createElementNS(SVGNS, 'svg');
  const use = document.createElementNS(SVGNS, 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

/** h('div', {class:'x', text:'y', onclick:fn, data:{a:1}, attrs:{title:'t'}}, ...children) */
function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k === 'data') Object.assign(el.dataset, v);
      else if (k === 'attrs') for (const [a, b] of Object.entries(v)) el.setAttribute(a, b);
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el[k] = v;
    }
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function btn(label, { cls = '', ico, onclick, title, disabled, type = 'button' } = {}) {
  return h('button', { class: 'btn ' + cls, type, onclick, title, disabled }, ico ? icon(ico) : null, label);
}

function pill(label, cls = '') { return h('span', { class: 'pill ' + cls, text: label }); }

async function busy(button, fn) {
  const kids = [...button.childNodes];
  button.disabled = true;
  button.replaceChildren(h('span', { class: 'spinner' }), ...kids.filter(k => k.nodeType === 3));
  try { return await fn(); }
  finally { button.replaceChildren(...kids); button.disabled = false; }
}

function hashHue(str) {
  let x = 0;
  for (const c of String(str)) x = (x * 31 + c.charCodeAt(0)) >>> 0;
  return x % 5;
}
function initials(title) {
  const words = String(title).replace(/[^\p{L}\p{N} ]/gu, ' ').split(/\s+/).filter(Boolean);
  return ((words[0]?.[0] || '') + (words[1]?.[0] || '')).toUpperCase() || '•';
}

/** Pôster com fallback elegante; a imagem só aparece quando carrega. */
function posterEl(title, src, { play = false } = {}) {
  const box = h('div', { class: 'poster' },
    h('div', { class: 'poster-fallback h' + hashHue(title) },
      h('span', { class: 'initials', text: initials(title) }),
      h('span', { class: 'ftitle', text: title })));
  if (src) addImage(box, src, title);
  if (play) box.append(h('span', { class: 'play' }, icon('play')));
  return box;
}
function addImage(box, src, title) {
  if (box.querySelector('img')) return;
  const img = h('img', { alt: 'Capa de ' + title, loading: 'lazy', decoding: 'async' });
  img.addEventListener('load', () => img.classList.add('loaded'));
  img.addEventListener('error', () => img.remove());
  img.src = src;
  box.prepend(img);
}

/* ============================== Toasts e diálogos ============================== */
function toast(message, type = 'info', ms = 4200) {
  const ico = type === 'ok' ? 'check' : type === 'bad' ? 'alert' : 'spark';
  const el = h('div', { class: 'toast ' + type, attrs: { role: 'status' } }, icon(ico), h('span', { text: message }));
  $('toasts').append(el);
  setTimeout(() => { el.classList.add('out'); setTimeout(() => el.remove(), 320); }, ms);
}

let dialogResolve = null;
function ask(title, text, { ok = 'Confirmar', danger = false } = {}) {
  $('dlgTitle').textContent = title;
  $('dlgText').textContent = text;
  $('dlgOk').textContent = ok;
  $('dlgOk').classList.toggle('danger-fill', danger);
  $('dialog').classList.remove('hidden');
  $('dlgOk').focus();
  return new Promise(resolve => { dialogResolve = resolve; });
}
function closeDialog(value) {
  $('dialog').classList.add('hidden');
  if (dialogResolve) { dialogResolve(value); dialogResolve = null; }
}
$('dlgOk').addEventListener('click', () => closeDialog(true));
$('dlgCancel').addEventListener('click', () => closeDialog(false));
$('dialog').addEventListener('click', e => { if (e.target === $('dialog')) closeDialog(false); });

/* ============================== API ============================== */
let csrfToken = '';
async function api(path, { method = 'GET', params, body, raw, headers = {} } = {}) {
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v !== '' && v !== null && v !== undefined) q.set(k, v);
    if ([...q].length) url += '?' + q;
  }
  const hdrs = { ...headers };
  if (method !== 'GET') hdrs['X-CSRF-Token'] = csrfToken;
  if (body !== undefined && !raw) hdrs['Content-Type'] = 'application/json';
  const res = await fetch(url, {
    method, cache: 'no-store', credentials: 'same-origin', headers: hdrs,
    body: raw || (body !== undefined ? JSON.stringify(body) : undefined)
  });
  if (res.status === 401) { location.replace('/login'); throw Error('Sessão expirada'); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Error(data.error || 'HTTP ' + res.status);
  return data;
}
const post = (path, body = {}) => api(path, { method: 'POST', body });

/* ============================== Estado ============================== */
const LIMIT = 24;
const state = {
  view: 'movie', page: 1, q: '', category: '', source: '', total: 0,
  filters: null, stats: null, noCatalog: false, seq: 0, heroType: null,
  managed: null, managedFilter: 'all', managedQuery: ''
};
const timers = { job: null, series: null, playlist: null, requests: null };

/* ============================== Sessão ============================== */
async function establishSession() {
  try {
    const res = await fetch('/auth/me', { cache: 'no-store', credentials: 'same-origin' });
    if (!res.ok) { location.replace('/login'); return false; }
    const data = await res.json();
    csrfToken = data.csrf;
    $('userName').textContent = data.user;
    $('avatar').textContent = (data.user || '?').slice(0, 1);
    return true;
  } catch { location.replace('/login'); return false; }
}
$('logout').addEventListener('click', async () => {
  try { await post('/auth/logout'); } catch { /* sessão já encerrada */ }
  location.replace('/login');
});

/* ============================== Roteamento ============================== */
const ROUTES = { '#/filmes': 'movie', '#/series': 'episode', '#/gerenciados': 'managed', '#/solicitacoes': 'requests', '#/listas': 'sources' };
function route() {
  const view = ROUTES[location.hash] || 'movie';
  const changedType = view !== state.view;
  state.view = view;
  document.querySelectorAll('[data-view]').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  const catalog = view === 'movie' || view === 'episode';
  $('viewCatalog').classList.toggle('hidden', !catalog);
  $('viewManaged').classList.toggle('hidden', view !== 'managed');
  $('viewSources').classList.toggle('hidden', view !== 'sources');
  $('viewRequests').classList.toggle('hidden', view !== 'requests');
  clearInterval(timers.playlist);
  clearInterval(timers.requests);
  if (catalog) {
    if (changedType) { state.page = 1; state.category = ''; }
    renderCatalogChrome();
    browse();
  } else if (view === 'managed') loadManaged();
  else if (view === 'requests') { loadRequests(); timers.requests = setInterval(() => { if (!document.hidden) loadRequests(true); }, 5000); }
  else loadSources();
  window.scrollTo({ top: 0 });
}
window.addEventListener('hashchange', route);

/* ============================== Metadados globais ============================== */
async function loadMeta() {
  try {
    const [stats, filters] = await Promise.all([api('/stats'), api('/filters')]);
    state.stats = stats; state.filters = filters; state.noCatalog = false;
    const c = stats.counts || {};
    $('statMovies').textContent = fmt(c.movie);
    $('statEpisodes').textContent = fmt(c.episode);
    $('statSources').textContent = fmt(filters.sources.length);
    $('statCategories').textContent = fmt(filters.categories.length);
    $('navMovies').textContent = c.movie ? compact(c.movie) : '';
    $('navSeries').textContent = c.episode ? compact(c.episode) : '';
    const when = new Date(stats.updated_at);
    $('lastUpdate').textContent = isNaN(when) ? '—' : when.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' });
    const sel = $('source');
    sel.replaceChildren(new Option('Todas as listas', ''));
    for (const s of filters.sources) sel.add(new Option(s.name + ' · ' + compact(s.count), s.name));
    sel.value = state.source;
  } catch (e) {
    state.noCatalog = /importe/i.test(e.message);
    document.querySelector('.pulse').classList.toggle('off', !state.noCatalog);
    $('lastUpdate').textContent = state.noCatalog ? 'vazio' : 'offline';
  }
  loadMetaQueue();
}
function compact(n) {
  return Number(n) >= 1000 ? new Intl.NumberFormat('pt-BR', { notation: 'compact', maximumFractionDigits: 1 }).format(n) : fmt(n);
}
async function loadMetaQueue() {
  try {
    const s = await api('/metadata/status');
    const c = s.counts || {};
    const pending = (c.queued || 0) + (c.running || 0);
    $('metaQueue').textContent = pending ? 'Metadados · ' + fmt(pending) + ' na fila' : 'Metadados em dia · ' + fmt(c.done || 0);
  } catch { $('metaQueue').textContent = 'Metadados —'; }
}

/* ============================== Catálogo ============================== */
const isMovie = () => state.view === 'movie';

function renderCatalogChrome() {
  const movie = isMovie();
  $('heroEyebrow').textContent = movie ? 'Biblioteca · Filmes' : 'Biblioteca · Séries';
  $('heroTitle').textContent = movie ? 'Filmes' : 'Séries';
  $('heroText').textContent = movie
    ? 'Explore o catálogo indexado, valide a fonte e envie o filme para o Jellyfin em um clique.'
    : 'Séries agrupadas por título. Importe temporadas inteiras com validação episódio a episódio.';
  $('search').placeholder = movie ? 'Buscar filmes…' : 'Buscar séries…';
  renderChips();
  $('clear').classList.toggle('hidden', !(state.q || state.category || state.source));
}

const TOP_CATS = 14;
let catSort = 'count';

function catLabel(name) {
  let label = String(name || '').replace(/^\s*(FILMES|S[ÉE]RIES)\s*[|/:\-–]\s*/i, '').trim() || name;
  // CAIXA ALTA → Título (mantém siglas curtas e emojis)
  if (/\p{L}/u.test(label) && label === label.toLocaleUpperCase('pt-BR')) {
    label = label.toLocaleLowerCase('pt-BR').replace(/(^|[\s/(\-])(\p{L})/gu, (m, a, b) => a + b.toLocaleUpperCase('pt-BR'))
      .replace(/\b(Tv|Hbo|Mma|Ufc|Dc|Hd|4k|Uhd|Bbc)\b/gi, w => w.toUpperCase());
  }
  return label;
}

function catalogCategories() {
  if (!state.filters) return [];
  const prefix = isMovie() ? /^FILMES/i : /^S[ÉE]RIES/i;
  const cats = state.filters.categories.filter(c => prefix.test(c.name) && c.name !== 'Sem categoria');
  return cats.length ? cats : state.filters.categories.filter(c => c.name !== 'Sem categoria');
}

function renderChips() {
  const box = $('categoryChips');
  box.replaceChildren();
  const cats = catalogCategories();
  const mk = (label, value, count) => {
    const b = h('button', { class: 'chip-btn' + (state.category === value ? ' on' : ''), type: 'button', title: value || 'Todas', attrs: { role: 'tab' } },
      label, count !== undefined ? h('small', { text: compact(count) }) : null);
    b.addEventListener('click', () => selectCategory(value));
    return b;
  };
  box.append(mk('Tudo', ''));
  const top = cats.slice(0, TOP_CATS);
  const current = state.category && cats.find(c => c.name === state.category);
  if (current && !top.includes(current)) top.unshift(current);
  for (const c of top) box.append(mk(catLabel(c.name), c.name, c.count));
  const more = cats.length - Math.min(cats.length, TOP_CATS);
  if (more > 0) {
    const b = h('button', { class: 'chip-btn', type: 'button' }, icon('plus'), '+' + fmt(more) + ' categorias');
    b.addEventListener('click', openCatPicker);
    box.append(b);
  }
  $('allCatsLabel').replaceChildren(document.createTextNode(state.category ? catLabel(state.category) : 'Categorias'), ' ', h('small', { text: fmt(cats.length) }));
  $('allCats').classList.toggle('on', Boolean(state.category));
  $('allCats').classList.toggle('hidden', cats.length <= 1);
  requestAnimationFrame(() => {
    const on = box.querySelector('.chip-btn.on');
    if (on && state.category) on.scrollIntoView({ block: 'nearest', inline: 'center' });
    updateChipArrows();
  });
}

function selectCategory(value) {
  state.category = value; state.page = 1;
  renderCatalogChrome(); browse();
}

function updateChipArrows() {
  const box = $('categoryChips');
  const overflow = box.scrollWidth > box.clientWidth + 4;
  const atStart = box.scrollLeft <= 4;
  const atEnd = box.scrollLeft + box.clientWidth >= box.scrollWidth - 4;
  $('chipsPrev').classList.toggle('hidden', !overflow || atStart);
  $('chipsNext').classList.toggle('hidden', !overflow || atEnd);
  box.classList.remove('fade-l', 'fade-r-only', 'fade-l-only', 'nofade');
  box.classList.add(!overflow ? 'nofade' : atStart ? 'fade-r-only' : atEnd ? 'fade-l-only' : 'fade-l');
}
$('categoryChips').addEventListener('scroll', updateChipArrows, { passive: true });
window.addEventListener('resize', updateChipArrows);
$('chipsPrev').addEventListener('click', () => { const b = $('categoryChips'); b.scrollBy({ left: -b.clientWidth * 0.8 }); });
$('chipsNext').addEventListener('click', () => { const b = $('categoryChips'); b.scrollBy({ left: b.clientWidth * 0.8 }); });
// Roda do mouse rola a faixa na horizontal
$('categoryChips').addEventListener('wheel', e => {
  const b = e.currentTarget;
  if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && b.scrollWidth > b.clientWidth) { b.scrollLeft += e.deltaY; e.preventDefault(); }
}, { passive: false });

/* Seletor com todas as categorias */
function openCatPicker() {
  $('catpopTitle').textContent = (isMovie() ? 'Categorias de filmes' : 'Categorias de séries') + ' · ' + fmt(catalogCategories().length);
  $('catpopSearch').value = '';
  renderCatPicker();
  $('catpop').classList.remove('hidden');
  setTimeout(() => $('catpopSearch').focus(), 50);
}
function closeCatPicker() { $('catpop').classList.add('hidden'); }
function renderCatPicker() {
  const q = $('catpopSearch').value.trim().toLocaleLowerCase('pt-BR').normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  let cats = catalogCategories().map(c => ({ ...c, label: catLabel(c.name) }));
  if (q) cats = cats.filter(c => (c.label + ' ' + c.name).toLocaleLowerCase('pt-BR').normalize('NFD').replace(/[\u0300-\u036f]/g, '').includes(q));
  if (catSort === 'name') cats.sort((a, b) => a.label.localeCompare(b.label, 'pt-BR'));
  const list = $('catpopList');
  list.replaceChildren();
  if (!q) {
    const all = h('button', { class: 'cat-item' + (!state.category ? ' on' : ''), type: 'button' }, h('span', { text: 'Todas as categorias' }));
    all.addEventListener('click', () => { closeCatPicker(); selectCategory(''); });
    list.append(all);
  }
  for (const c of cats) {
    const b = h('button', { class: 'cat-item' + (state.category === c.name ? ' on' : ''), type: 'button', title: c.name },
      h('span', { text: c.label }), h('small', { text: compact(c.count) }));
    b.addEventListener('click', () => { closeCatPicker(); selectCategory(c.name); });
    list.append(b);
  }
  if (!cats.length) list.append(h('div', { class: 'catpop-empty', text: 'Nenhuma categoria encontrada.' }));
}
$('allCats').addEventListener('click', openCatPicker);
$('catpopClose').addEventListener('click', closeCatPicker);
$('catpop').addEventListener('click', e => { if (e.target === $('catpop')) closeCatPicker(); });
$('catpopSearch').addEventListener('input', renderCatPicker);
$('catpopSearch').addEventListener('keydown', e => {
  if (e.key === 'Enter') { const first = $('catpopList').querySelector('.cat-item:not(.on)') || $('catpopList').querySelector('.cat-item'); if (first) first.click(); }
});
$('catpopSort').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  catSort = b.dataset.s;
  $('catpopSort').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  renderCatPicker();
});

/* Filtro de conteúdo (categorias bloqueadas nas buscas do Jellyfin/StreamBox) */
const cf = { auto: true, show: false, blocked: new Set(), allowed: new Set(), cats: [], view: 'all' };
const cfIsBlocked = c => cf.blocked.has(c.raw) || (cf.auto && c.adult && !cf.allowed.has(c.raw));
async function cfLoad() {
  try {
    const d = await api('/content-filter');
    cf.auto = d.auto; cf.show = d.show_in_panel; cf.cats = d.categories || [];
    cf.blocked = new Set(d.blocked); cf.allowed = new Set(d.allowed);
    const n = d.blocked_count || 0;
    $('cfBadge').textContent = n; $('cfBadge').classList.toggle('hidden', !n);
  } catch { /* catálogo vazio */ }
}
async function openContentFilter() {
  await cfLoad();
  $('cfAuto').checked = cf.auto; $('cfPanel').checked = cf.show; $('cfSearch').value = '';
  renderContentFilter();
  $('cfpop').classList.remove('hidden');
}
function renderContentFilter() {
  const q = $('cfSearch').value.trim().toLocaleLowerCase('pt-BR').normalize('NFD').replace(/[\u0300-\u036f]/g, '');
  let cats = cf.cats;
  if (q) cats = cats.filter(c => c.name.toLocaleLowerCase('pt-BR').normalize('NFD').replace(/[\u0300-\u036f]/g, '').includes(q));
  if (cf.view === 'blocked') cats = cats.filter(cfIsBlocked);
  const list = $('cfList');
  list.replaceChildren();
  for (const c of cats.slice(0, 600)) {
    const blk = cfIsBlocked(c);
    const b = h('button', { class: 'cat-item' + (blk ? ' blk' : ''), type: 'button', title: c.name + (blk ? ' — bloqueada (clique para liberar)' : ' — clique para bloquear') },
      h('span', { text: c.name }), h('small', {}, compact(c.count), c.adult && cf.auto ? h('em', { class: 'tag-auto', text: 'auto' }) : null));
    b.addEventListener('click', () => {
      if (cfIsBlocked(c)) { cf.blocked.delete(c.raw); if (cf.auto && c.adult) cf.allowed.add(c.raw); }
      else { cf.allowed.delete(c.raw); if (!(cf.auto && c.adult)) cf.blocked.add(c.raw); }
      renderContentFilter();
    });
    list.append(b);
  }
  if (!cats.length) list.append(h('div', { class: 'catpop-empty', text: cf.view === 'blocked' ? 'Nenhuma categoria bloqueada.' : 'Nenhuma categoria encontrada.' }));
  const blocked = cf.cats.filter(cfIsBlocked);
  $('cfSummary').textContent = blocked.length
    ? `${fmt(blocked.length)} categoria(s) bloqueada(s) · ${fmt(blocked.reduce((a, c) => a + c.count, 0))} itens`
    : 'Nada bloqueado';
}
$('cfBtn').addEventListener('click', openContentFilter);
$('cfClose').addEventListener('click', () => $('cfpop').classList.add('hidden'));
$('cfpop').addEventListener('click', e => { if (e.target === $('cfpop')) $('cfpop').classList.add('hidden'); });
$('cfSearch').addEventListener('input', renderContentFilter);
$('cfAuto').addEventListener('change', e => { cf.auto = e.target.checked; renderContentFilter(); });
$('cfPanel').addEventListener('change', e => { cf.show = e.target.checked; });
$('cfShow').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  cf.view = b.dataset.s;
  $('cfShow').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  renderContentFilter();
});
$('cfSave').addEventListener('click', async () => {
  const btn = $('cfSave'); btn.disabled = true;
  try {
    await post('/content-filter', { auto: cf.auto, show_in_panel: cf.show, blocked: [...cf.blocked], allowed: [...cf.allowed] });
    toast('Filtro de conteúdo salvo', 'ok');
    $('cfpop').classList.add('hidden');
    await cfLoad();
    if (state.category && cf.cats.some(c => c.raw === state.category && cfIsBlocked(c)) && !cf.show) state.category = '';
    await loadMeta();
    renderCatalogChrome(); browse();
  } catch (err) { toast(err.message, 'bad'); }
  finally { btn.disabled = false; }
});
cfLoad();

function skeletons(n = 12) {
  return Array.from({ length: n }, () => h('div', { class: 'card skeleton' },
    h('div', { class: 'poster' }), h('div', { class: 'sk', attrs: { 'aria-hidden': 'true' } }),
    h('div', { class: 'sk', attrs: { 'aria-hidden': 'true' } })));
}

function emptyState(iconName, title, text, action) {
  return h('div', { class: 'empty' }, icon(iconName), h('strong', { text: title }), h('span', { text: text }), action || null);
}

async function browse({ silent = false } = {}) {
  const seq = ++state.seq;
  const grid = $('results');
  if (!silent) {
    grid.replaceChildren(...skeletons());
    $('pager').replaceChildren();
  }
  $('resultsTitle').textContent = state.q ? 'Resultados para “' + state.q + '”'
    : state.category ? catLabel(state.category) : (isMovie() ? 'Todos os filmes' : 'Todas as séries');
  if (state.noCatalog) {
    grid.replaceChildren(emptyState('list', 'Nenhuma lista indexada',
      'Adicione um arquivo M3U/M3U8 para montar o catálogo.',
      h('a', { class: 'btn primary', href: '#/listas' }, icon('plus'), 'Adicionar lista')));
    $('totalLabel').textContent = '';
    return;
  }
  try {
    const data = await api('/browse', {
      params: { type: state.view, q: state.q, category: state.category, source: state.source, page: state.page, limit: LIMIT }
    });
    if (seq !== state.seq) return;
    state.total = data.total;
    $('totalLabel').textContent = fmt(data.total) + (isMovie() ? ' filmes' : ' séries');
    if (silent) return patchPosters(data.items);
    grid.replaceChildren();
    if (!data.items.length) {
      grid.append(emptyState('search', 'Nada encontrado', 'Tente outro termo ou remova os filtros.'));
    } else {
      data.items.forEach((it, i) => grid.append(card(it, i)));
    }
    renderPager();
    updateHero(data.items);
    $('tvmazeCredit').classList.toggle('hidden', !data.items.some(it => it.metadata?.provider === 'tvmaze'));
  } catch (e) {
    if (seq !== state.seq) return;
    if (!silent) grid.replaceChildren(emptyState('alert', 'Falha ao consultar o catálogo', e.message));
  }
}

function card(it, i) {
  const movie = isMovie();
  const meta = it.metadata || {};
  const title = it.title;
  const p = posterEl(title, meta.poster, { play: true });
  if (movie && it.managed) {
    p.append(it.managed.status === 'active'
      ? h('span', { class: 'tag ok' }, icon('check'), 'No Jellyfin')
      : h('span', { class: 'tag bad' }, icon('alert'), 'Quarentena'));
  } else if (!movie && it.managed_count) {
    p.append(h('span', { class: 'tag ok' }, icon('check'), fmt(it.managed_count) + ' ep.'));
  }
  if (it.source_count > 1) p.append(h('span', { class: 'tag right', text: it.source_count + ' listas' }));
  const year = meta.year || it.year;
  const sub = movie
    ? [year || 'Ano —', fmt(it.entries) + (it.entries === 1 ? ' fonte' : ' fontes')]
    : [it.seasons + (it.seasons === 1 ? ' temporada' : ' temporadas'), fmt(it.entries) + ' ep.'];
  const el = h('button', { class: 'card', type: 'button', data: { title } },
    p, h('div', { class: 'card-title', text: title, title }),
    h('div', { class: 'card-sub' }, sub[0], h('i'), sub[1]));
  el.style.animationDelay = Math.min(i * 22, 400) + 'ms';
  el.addEventListener('click', () => (movie ? openMovie(it) : openSeries(it)));
  return el;
}

/** Atualiza só as capas que chegaram desde a última consulta (sem piscar a grade). */
function patchPosters(items) {
  const cards = [...$('results').querySelectorAll('.card')];
  for (const it of items) {
    if (!it.metadata?.poster) continue;
    const c = cards.find(x => x.dataset.title === it.title);
    if (c) addImage(c.querySelector('.poster'), it.metadata.poster, it.title);
  }
}

function updateHero(items) {
  const posters = items.map(x => x.metadata?.poster).filter(Boolean);
  if (state.heroType === state.view && $('heroBg').childElementCount && posters.length < 4) return;
  state.heroType = state.view;
  const bg = $('heroBg');
  bg.replaceChildren();
  for (let i = 0; i < 18; i++) {
    const s = h('span');
    if (posters.length) s.style.backgroundImage = 'url("' + posters[i % posters.length] + '")';
    bg.append(s);
  }
}

function renderPager() {
  const pages = Math.max(1, Math.ceil(state.total / LIMIT));
  const nav = $('pager');
  nav.replaceChildren();
  if (pages <= 1) return;
  const go = p => { state.page = p; browse(); $('results').scrollIntoView({ behavior: 'smooth', block: 'start' }); };
  const b = (label, p, { on = false, disabled = false, aria } = {}) => {
    const el = h('button', { type: 'button', class: on ? 'on' : '', disabled, attrs: aria ? { 'aria-label': aria } : undefined }, label);
    el.addEventListener('click', () => go(p));
    return el;
  };
  nav.append(b(icon('left'), state.page - 1, { disabled: state.page <= 1, aria: 'Página anterior' }));
  const set = new Set([1, pages, state.page, state.page - 1, state.page + 1, state.page - 2, state.page + 2]);
  const list = [...set].filter(p => p >= 1 && p <= pages).sort((a, b) => a - b);
  let prev = 0;
  for (const p of list) {
    if (p - prev > 1) nav.append(h('span', { class: 'gap', text: '…' }));
    nav.append(b(fmt(p), p, { on: p === state.page }));
    prev = p;
  }
  nav.append(b(icon('right'), state.page + 1, { disabled: state.page >= pages, aria: 'Próxima página' }));
}

/* Busca e filtros */
let searchTimer;
$('search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.q = e.target.value.trim();
    state.page = 1;
    if (state.view !== 'movie' && state.view !== 'episode') { location.hash = '#/filmes'; return; }
    renderCatalogChrome();
    browse();
  }, 280);
});
$('source').addEventListener('change', e => { state.source = e.target.value; state.page = 1; renderCatalogChrome(); browse(); });
$('clear').addEventListener('click', () => {
  state.q = ''; state.category = ''; state.source = ''; state.page = 1;
  $('search').value = ''; $('source').value = '';
  renderCatalogChrome(); browse();
});

/* Capas que chegam em segundo plano pela fila de metadados */
setInterval(() => {
  if (document.hidden || !$('drawer').classList.contains('hidden')) return;
  if (state.view !== 'movie' && state.view !== 'episode') return;
  const cards = [...document.querySelectorAll('#results .card:not(.skeleton) .poster')];
  if (!cards.some(p => !p.querySelector('img'))) return;
  browse({ silent: true });
  loadMetaQueue();
}, 20000);

/* ============================== Painel de detalhes ============================== */
function openDrawer({ eyebrow, title, poster, meta = [], overview = '' }) {
  stopTimers();
  $('dEyebrow').textContent = eyebrow;
  $('dTitle').textContent = title;
  $('dPoster').replaceChildren(posterEl(title, poster));
  $('dBackdrop').style.backgroundImage = poster ? 'url("' + poster + '")' : '';
  $('dMeta').replaceChildren(...meta.filter(Boolean));
  $('dOverview').textContent = overview || '';
  $('dOverview').classList.toggle('hidden', !overview);
  $('dBody').replaceChildren();
  $('drawer').classList.remove('hidden');
  document.body.style.overflow = 'hidden';
  $('drawer').querySelector('.drawer').scrollTop = 0;
  $('closeDrawer').focus();
}
function closeDrawer() {
  stopTimers();
  const v = $('dBody').querySelector('video');
  if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
  $('drawer').classList.add('hidden');
  document.body.style.overflow = '';
}
function stopTimers() { clearInterval(timers.series); timers.series = null; }
$('closeDrawer').addEventListener('click', closeDrawer);
$('scrim').addEventListener('click', closeDrawer);

function section(title, iconName, ...kids) {
  return h('section', { class: 'd-section' }, h('h3', null, icon(iconName), title), ...kids);
}

/** Player compartilhado do painel (prévia temporária gerada pelo /validate). */
function previewSection() {
  const video = h('video', { class: 'player', controls: true, playsInline: true, preload: 'none' });
  const note = h('p', { class: 'note', text: 'A prévia depende do suporte do navegador ao formato e do servidor remoto.' });
  const sec = section('Prévia', 'play', video, note);
  sec.classList.add('hidden');
  video.addEventListener('error', () => {
    if (video.getAttribute('src')) note.textContent = 'Prévia indisponível neste navegador. A validação HTTP não garante reprodução.';
  });
  sec.show = url => {
    sec.classList.remove('hidden');
    note.textContent = 'A prévia expira em 5 minutos.';
    video.src = url; video.load();
    sec.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  };
  return sec;
}

/**
 * Linha de fonte: validar → (prévia) → adicionar STRM.
 * opts.managed: estado já gerenciado; opts.onAdded(result); opts.preview: seção de prévia
 */
function sourceRow(entry, opts = {}) {
  const row = h('div', { class: 'src' });
  const title = entry.original_title || entry.title;
  const info = h('div', { class: 'src-main' },
    h('div', { class: 'src-name', text: title, title }),
    h('div', { class: 'src-meta' }, icon('list'), entry.source || '—', h('code', { text: entry.host || 'host —' })));
  const msg = h('div', { class: 'src-msg hidden' });
  let validatedAt = 0;
  const add = btn(opts.managed ? 'No Jellyfin' : 'Adicionar', { cls: 'sm ' + (opts.managed ? 'ok' : 'primary'), ico: opts.managed ? 'check' : 'plus', disabled: true });
  const test = btn('Testar', { cls: 'sm', ico: 'play' });
  const say = (text, kind) => { msg.textContent = text; msg.className = 'src-msg ' + (kind || ''); };

  test.addEventListener('click', () => busy(test, async () => {
    say('Verificando disponibilidade da fonte…');
    try {
      const r = await post('/validate', { id: entry.id });
      row.classList.toggle('valid', r.ok);
      row.classList.toggle('invalid', !r.ok);
      say((r.ok ? 'Fonte acessível · ' : 'Indisponível · ') + r.reason + (r.cached ? ' (cache)' : ''), r.ok ? 'ok' : 'bad');
      validatedAt = r.ok ? Date.now() : 0;
      if (!opts.managed) add.disabled = !r.ok;
      if (r.ok && r.preview && opts.preview) opts.preview.show(r.preview);
    } catch (e) { say('Falha na validação: ' + e.message, 'bad'); }
  }));

  add.addEventListener('click', () => {
    if (opts.managed) return;
    if (!validatedAt || Date.now() - validatedAt > 600000) { say('Valide a fonte novamente antes de adicionar.', 'bad'); add.disabled = true; return; }
    busy(add, async () => {
      try {
        const r = await post('/add', { id: entry.id });
        say((r.already_added ? 'Já estava cadastrado: ' : 'STRM criado: ') + r.path, 'ok');
        toast(r.already_added ? 'Esse conteúdo já estava no Jellyfin' : 'Adicionado ao Jellyfin', 'ok');
        opts.managed = true;
        if (opts.onAdded) await opts.onAdded(r);
      } catch (e) { say('Não foi possível adicionar: ' + e.message, 'bad'); }
    }).then(() => {
      if (opts.managed) { add.replaceChildren(icon('check'), 'No Jellyfin'); add.className = 'btn sm ok'; add.disabled = true; }
    });
  });

  if (opts.managed) add.disabled = true;
  row.append(info, h('div', { class: 'src-actions' }, test, add), msg);
  return row;
}

/* ---------- Limpeza de títulos para busca de metadados ---------- */
const TAG_WORDS = "4k|uhd|fhd|full\\s*hd|hd|sd|hdr10\\+?|hdr|dolby\\s*vision|dv|imax|3d|2160p|1080p|720p|480p|x26[45]|h\\.?26[45]|hevc|web-?dl|webrip|bluray|blu-ray|brrip|dvdrip|cam|ts|dublado|dub|legendado|leg|l|dual(?:\\s*[aá]udio)?|multi|nacional|remasterizado|remastered|vers[aã]o\\s+estendida|estendida|extended|director'?s\\s*cut|unrated|sem\\s+cortes";
const RX = {
  bracket: /\s*\[[^\]]{0,40}\]\s*/g,
  parenTag: new RegExp('\\s*\\((?:\\s*(?:' + TAG_WORDS + ')\\s*[,/|+-]?\\s*)+\\)\\s*', 'gi'),
  trailTag: new RegExp('(?:\\s*[-–|:/]?\\s*(?<![\\p{L}\\p{N}])(?:' + TAG_WORDS + ')(?![\\p{L}\\p{N}])\\.?)+\\s*$', 'iu'),
  leadNum: /^\s*(?:#|n[ºo°]\.?\s*)?\d{1,3}\s*[-–.:)|]\s+(?=\S)/i,
  parenYear: /\(\s*((?:19|20)\d{2})\s*\)\s*$/,
  bareYear: /\s((?:19|20)\d{2})\s*$/
};
const trimSep = t => t.replace(/^[\s\-–|:/.]+|[\s\-–|:/.]+$/g, '');

/** '1 - Harry Potter E A Pedra Filosofal 4K' → {title:'Harry Potter E A Pedra Filosofal', year:null} */
function parseTitle(raw) {
  let t = String(raw || '').replace(RX.bracket, ' ');
  let year = null;
  for (let i = 0; i < 4; i++) {
    const before = t;
    t = trimSep(t.replace(RX.parenTag, ' ').replace(RX.trailTag, ''));
    const m = RX.parenYear.exec(t);
    if (m && t.slice(0, m.index).trim().length >= 2) { year = year || Number(m[1]); t = trimSep(t.slice(0, m.index)); }
    if (t === before) break;
  }
  t = trimSep(t.replace(RX.leadNum, '').replace(/\s{2,}/g, ' '));
  return { title: t.length >= 2 ? t : String(raw || '').trim(), year };
}

/** Variações tentadas em sequência até o provedor responder algo. */
function searchVariants(clean) {
  const out = [clean];
  const by = RX.bareYear.exec(clean);
  if (by && clean.slice(0, by.index).trim().length >= 2) out.push({ q: clean.slice(0, by.index).trim(), year: Number(by[1]) });
  const cut = clean.split(/\s*(?::|\s[-–]\s)\s*/);
  if (cut.length > 1) { if (cut[0].length >= 3) out.push(cut[0]); if (cut.at(-1).length >= 3) out.push(cut.at(-1)); }
  const seen = new Set();
  return out.map(v => (typeof v === 'string' ? { q: v } : v)).filter(v => !seen.has(v.q.toLowerCase()) && seen.add(v.q.toLowerCase()));
}

const normTxt = t => String(t || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
function similarity(a, b) {
  const x = normTxt(a), y = normTxt(b);
  if (!x || !y) return 0;
  if (x === y) return 1;
  const A = x.split(' '), B = new Set(y.split(' '));
  const inter = A.filter(w => B.has(w)).length;
  let score = (2 * inter) / (A.length + B.size);
  if (y.includes(x) || x.includes(y)) score = Math.max(score, 0.8);
  return score;
}
function rankCandidates(items, query, year) {
  return items.map(c => {
    let score = Math.max(similarity(query, c.title), similarity(query, c.original_title));
    if (year && c.year) score += c.year === year ? 0.15 : -0.1;
    return { ...c, score: Math.max(0, Math.min(1, score)) };
  }).sort((a, b) => b.score - a.score);
}

/** Extrai o ID de um número ou de um link do TMDB/TheTVDB. */
function parseManualId(provider, value, kind) {
  const v = String(value || '').trim();
  if (/^\d{1,9}$/.test(v)) return Number(v);
  if (provider === 'tmdb') {
    const m = /themoviedb\.org\/(movie|tv)\/(\d+)/i.exec(v);
    if (!m) throw Error('Cole o número ou um link themoviedb.org/movie/… ou /tv/…');
    const linkKind = m[1].toLowerCase() === 'movie' ? 'movie' : 'episode';
    if (linkKind !== kind) throw Error(kind === 'movie' ? 'Esse link é de uma série, não de um filme' : 'Esse link é de um filme, não de uma série');
    return Number(m[2]);
  }
  const m = /[?&]id=(\d+)/.exec(v) || /\/(?:series|movies)\/(\d+)(?:[/?#]|$)/.exec(v);
  if (m) return Number(m[1]);
  throw Error('No TheTVDB use o número do ID (aparece na página da obra)');
}

/**
 * Identificação de metadados (TMDB / TheTVDB): busca inteligente + ID manual.
 * ctx: { kind:'movie'|'episode', title, year, key? (STRM de filme), managedSeries? }
 */
function metadataSection(ctx, current) {
  const parsed = parseTitle(ctx.title);
  const autoYear = ctx.year || parsed.year || '';
  const results = h('div', { class: 'candidates' });
  const status = h('p', { class: 'note' });
  const setStatus = meta => {
    status.textContent = meta?.provider
      ? 'Metadados atuais: ' + providerName(meta.provider) + (meta.title ? ' · ' + meta.title : '') + (meta.year ? ' (' + meta.year + ')' : '')
      : 'Sem metadados confirmados. Busque pelo título ou informe o ID manualmente.';
  };
  setStatus(current);

  const query = h('input', { type: 'text', value: parsed.title, placeholder: 'Título para buscar', attrs: { 'aria-label': 'Título para buscar' } });
  const year = h('input', { type: 'text', value: autoYear ? String(autoYear) : '', placeholder: 'Ano', inputMode: 'numeric', maxLength: 4, class: 'year-input', attrs: { 'aria-label': 'Ano' } });
  const hint = h('p', { class: 'note' });
  if (parsed.title !== ctx.title.trim()) hint.append('Título limpo automaticamente de ', h('em', { text: '“' + ctx.title + '”' }), '.');

  const runSearch = async (provider, button) => busy(button, async () => {
    results.replaceChildren();
    const q = query.value.trim();
    const y = /^\d{4}$/.test(year.value.trim()) ? Number(year.value.trim()) : null;
    if (q.length < 2) { results.append(h('p', { class: 'note', text: 'Digite pelo menos 2 letras.' })); return; }
    const edited = q !== parsed.title;
    const variants = edited ? [{ q }] : searchVariants(q);
    const tried = [];
    let found = [], used = null;
    try {
      for (const v of variants.slice(0, 4)) {
        const vy = v.year || y;
        const attempts = provider === 'tmdb' && vy ? [vy, null] : [null];
        for (const yy of attempts) {
          tried.push(v.q + (yy ? ' (' + yy + ')' : ''));
          const data = provider === 'tvdb'
            ? await api('/tvdb/search', { params: { title: v.q, type: ctx.kind } })
            : await api('/tmdb/search', { params: { title: v.q, type: ctx.kind === 'movie' ? 'movie' : 'tv', year: ctx.kind === 'movie' ? (yy || '') : '' } });
          if (data.items?.length) { found = data.items; used = { q: v.q, year: vy }; break; }
        }
        if (found.length) break;
      }
    } catch (e) { results.append(h('p', { class: 'note', text: providerName(provider) + ': ' + e.message })); return; }
    if (!found.length) {
      results.append(h('p', { class: 'note', text: 'Nada em ' + providerName(provider) + '. Tentativas: ' + tried.join(' · ') + '. Ajuste o título ou use o ID manual.' }));
      return;
    }
    const ranked = rankCandidates(found, used.q, used.year);
    if (used.q !== q) results.append(h('p', { class: 'note', text: 'Resultados para “' + used.q + '”.' }));
    ranked.forEach((c, i) => results.append(candidate(provider, c, i === 0 && c.score >= 0.6)));
  });

  const confirmAndApply = async (provider, id, label) => {
    if (!await ask('Confirmar identificação', label + ' · ' + providerName(provider) + ' #' + id)) return false;
    results.querySelectorAll('.cand').forEach(x => { x.disabled = true; });
    try {
      await applyMetadata(ctx, provider, id);
      const meta = await refreshDrawerPoster(ctx.kind, ctx.title);
      setStatus(meta || { provider, title: label });
      if (meta?.overview) { $('dOverview').textContent = meta.overview; $('dOverview').classList.remove('hidden'); }
      results.replaceChildren();
      toast('Metadados confirmados', 'ok');
      if (state.view === 'movie' || state.view === 'episode') setTimeout(() => browse({ silent: true }), 600);
      return true;
    } catch (e) {
      toast('Não foi possível salvar: ' + e.message, 'bad', 7000);
      results.querySelectorAll('.cand').forEach(x => { x.disabled = false; });
      return false;
    }
  };

  const candidate = (provider, c, best) => {
    const pct = Math.round(c.score * 100);
    const el = h('button', { class: 'cand' + (best ? ' best' : ''), type: 'button' },
      posterEl(c.title || '?', c.poster),
      h('div', null,
        h('div', { class: 'cand-top' },
          best ? h('span', { class: 'pill ok', text: 'Melhor correspondência' }) : null,
          h('span', { class: 'score ' + (pct >= 80 ? 'hi' : pct >= 50 ? 'mid' : 'lo'), text: pct + '%' })),
        h('strong', { text: (c.title || 'Sem título') + (c.year ? ' (' + c.year + ')' : '') }),
        h('small', { text: providerName(provider) + ' #' + c.id + (c.original_title && c.original_title !== c.title ? ' · ' + c.original_title : '') }),
        c.overview ? h('p', { text: c.overview }) : null));
    el.addEventListener('click', () => confirmAndApply(provider, c.id, (c.title || '') + (c.year ? ' (' + c.year + ')' : '')));
    return el;
  };

  const tmdbBtn = btn('TMDB', { cls: 'sm primary', ico: 'search' });
  const tvdbBtn = btn('TheTVDB', { cls: 'sm', ico: 'search' });
  tmdbBtn.addEventListener('click', () => runSearch('tmdb', tmdbBtn));
  tvdbBtn.addEventListener('click', () => runSearch('tvdb', tvdbBtn));
  query.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); runSearch('tmdb', tmdbBtn); } });

  // ID manual
  const provSel = h('select', { attrs: { 'aria-label': 'Provedor' } }, new Option('TMDB', 'tmdb'), new Option('TheTVDB', 'tvdb'));
  const idInput = h('input', { type: 'text', placeholder: ctx.kind === 'movie' ? 'ID ou link · ex.: themoviedb.org/movie/671' : 'ID ou link · ex.: themoviedb.org/tv/1399', attrs: { 'aria-label': 'ID ou link' } });
  const apply = btn('Aplicar', { cls: 'sm primary', ico: 'check' });
  const applyManual = () => {
    let id;
    try { id = parseManualId(provSel.value, idInput.value, ctx.kind); }
    catch (e) { toast(e.message, 'bad'); return; }
    busy(apply, async () => { if (await confirmAndApply(provSel.value, id, ctx.title)) idInput.value = ''; });
  };
  apply.addEventListener('click', applyManual);
  idInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); applyManual(); } });
  idInput.addEventListener('input', () => { if (/themoviedb\.org/i.test(idInput.value)) provSel.value = 'tmdb'; else if (/thetvdb\.com/i.test(idInput.value)) provSel.value = 'tvdb'; });
  const manual = h('details', { class: 'manual' },
    h('summary', null, icon('chev'), 'Identificar manualmente por ID ou link'),
    h('div', { class: 'meta-search' }, h('div', { class: 'select' }, provSel, icon('chev')), idInput, apply),
    h('p', { class: 'note', text: 'Abra a obra no site do TMDB ou TheTVDB e cole o link (ou só o número do ID). O servidor confere o ID antes de salvar.' }));

  return section('Metadados', 'image', status,
    h('div', { class: 'meta-search' }, query, year, tmdbBtn, tvdbBtn), hint, results, manual);
}
/** Busca a capa salva localmente (/artwork/...) e aplica no painel aberto. Retorna os metadados. */
async function refreshDrawerPoster(kind, title) {
  try {
    const d = await api('/browse', { params: { type: kind, q: title, limit: 60 } });
    const meta = d.items.find(x => x.title === title)?.metadata;
    if (meta?.poster) {
      const box = $('dPoster').querySelector('.poster');
      box.querySelector('img')?.remove();
      addImage(box, meta.poster, title);
      $('dBackdrop').style.backgroundImage = 'url("' + meta.poster + '")';
    }
    return meta || null;
  } catch { return null; }
}
function providerName(p) { return ({ tmdb: 'TMDB', tvdb: 'TheTVDB', tvmaze: 'TVmaze', jellyfin: 'Jellyfin', seerr: 'Seerr' })[p] || p; }

async function applyMetadata(ctx, provider, id) {
  await post('/metadata/confirm', { provider, kind: ctx.kind, title: ctx.title, id });
  // Gera NFO junto do STRM quando o conteúdo já está no Jellyfin.
  try {
    if (ctx.kind === 'movie' && ctx.key) {
      if (provider === 'tmdb') await post('/tmdb/apply', { key: ctx.key, tmdb_id: id });
      else await post('/tvdb/apply', { kind: 'movie', title: ctx.title, tvdb_id: id, key: ctx.key });
    } else if (ctx.kind === 'episode' && ctx.managedSeries) {
      if (provider === 'tmdb') await post('/tmdb/apply-series', { title: ctx.title, tmdb_id: id });
      else await post('/tvdb/apply', { kind: 'episode', title: ctx.title, tvdb_id: id });
    }
  } catch (e) { toast('Metadados salvos, mas o NFO não foi gerado: ' + e.message, 'bad', 6000); }
}

/** Depois de adicionar um filme, aproveita metadados já confirmados para gerar NFO/pôster. */
async function autoNfo(kind, title, key, meta) {
  if (!meta || !key) return;
  const provider = meta.provider === 'seerr' ? 'tmdb' : meta.provider;
  if (provider !== 'tmdb' && provider !== 'tvdb') return;
  try {
    await applyMetadata({ kind, title, key }, provider, Number(meta.provider_id));
    toast('NFO e capa enviados para o Jellyfin', 'ok');
  } catch { /* já sinalizado */ }
}

/* ---------- Filme ---------- */
async function openMovie(it) {
  const meta = it.metadata || {};
  const year = meta.year || it.year;
  openDrawer({
    eyebrow: 'Filme', title: it.title, poster: meta.poster, overview: meta.overview,
    meta: [year ? pill(String(year), 'plain') : null,
      it.category ? pill(catLabel(it.category), 'plain') : null,
      pill(fmt(it.entries) + (it.entries === 1 ? ' fonte' : ' fontes'), 'info'),
      it.managed ? pill(it.managed.status === 'active' ? 'No Jellyfin' : 'Quarentena', it.managed.status === 'active' ? 'ok' : 'bad') : null,
      meta.provider ? pill(providerName(meta.provider), 'plain') : null]
  });
  const body = $('dBody');
  let managedKey = it.managed?.key;
  const statusBox = h('div');
  const showStatus = (m) => {
    statusBox.replaceChildren(m ? h('div', { class: 'callout' + (m.status === 'active' ? '' : ' warn') },
      icon(m.status === 'active' ? 'check' : 'alert'),
      h('div', null, m.status === 'active' ? 'Disponível no Jellyfin como ' : 'Em quarentena · ', h('code', { text: m.path }))) : '');
  };
  showStatus(it.managed);
  const preview = previewSection();
  const list = h('div', null, ...[1, 2].map(() => h('div', { class: 'src skeleton' }, h('div', { class: 'sk' }))));
  const count = h('span', { class: 'count' });
  const srcSection = section('Fontes disponíveis', 'layers', list);
  srcSection.querySelector('h3').append(count);
  body.append(statusBox, srcSection, preview, metadataSection({ kind: 'movie', title: it.title, year, get key() { return managedKey; } }, meta));
  try {
    const r = await api('/search', { params: { title: it.title, type: 'movie', exact: 1, limit: 100 } });
    const matches = r.items.filter(x => !it.year || !x.year || x.year === it.year);
    count.textContent = fmt(matches.length);
    list.replaceChildren();
    if (!matches.length) list.append(h('p', { class: 'note', text: 'Nenhuma fonte correspondente encontrada.' }));
    for (const e of matches) {
      list.append(sourceRow(e, {
        managed: Boolean(managedKey), preview,
        onAdded: async res => {
          managedKey = res.key;
          showStatus({ status: res.status || 'active', path: res.path });
          await autoNfo('movie', it.title, res.key, meta.provider ? meta : null);
          setTimeout(() => browse({ silent: false }), 400);
        }
      }));
    }
  } catch (e) { list.replaceChildren(h('p', { class: 'note', text: 'Erro: ' + e.message })); }
}

/* ---------- Série ---------- */
async function openSeries(it) {
  const meta = it.metadata || {};
  openDrawer({
    eyebrow: 'Série', title: it.title, poster: meta.poster, overview: meta.overview,
    meta: [meta.year ? pill(String(meta.year), 'plain') : null,
      pill(it.seasons + (it.seasons === 1 ? ' temporada' : ' temporadas'), 'plain'),
      pill(fmt(it.entries) + ' episódios', 'info'),
      it.managed_count ? pill(fmt(it.managed_count) + ' no Jellyfin', 'ok') : null,
      meta.provider ? pill(providerName(meta.provider), 'plain') : null]
  });
  const body = $('dBody');
  const loading = h('div', { class: 'src skeleton' }, h('div', { class: 'sk' }));
  body.append(loading);
  let rows;
  try {
    rows = (await api('/episodes', { params: { title: it.title, source: state.source } })).items;
  } catch (e) { loading.replaceWith(h('p', { class: 'note', text: 'Erro ao carregar: ' + e.message })); return; }
  loading.remove();

  // Agrupa por temporada → episódio → fontes
  const seasons = new Map();
  for (const r of rows) {
    const s = r.season ?? 0;
    if (!seasons.has(s)) seasons.set(s, new Map());
    const eps = seasons.get(s);
    if (!eps.has(r.episode)) eps.set(r.episode, []);
    eps.get(r.episode).push(r);
  }
  const seasonNums = [...seasons.keys()].sort((a, b) => a - b);
  const preview = previewSection();
  body.append(batchPanel(it.title, seasonNums), preview);

  const pills = h('div', { class: 'seasons', attrs: { role: 'tablist' } });
  const epsBox = h('div', { class: 'eps' });
  const showSeason = s => {
    pills.querySelectorAll('.chip-btn').forEach(b => b.classList.toggle('on', Number(b.dataset.s) === s));
    epsBox.replaceChildren();
    const eps = [...seasons.get(s).entries()].sort((a, b) => a[0] - b[0]);
    for (const [ep, sources] of eps) {
      const managed = sources.find(x => x.managed)?.managed;
      const d = h('details', { class: 'ep' },
        h('summary', null,
          h('span', { class: 'ep-code', text: epCode(s, ep) }),
          h('span', { class: 'ep-title', text: sources[0].original_title }),
          h('span', null, managed ? pill(managed.status === 'active' ? 'No Jellyfin' : 'Quarentena', managed.status === 'active' ? 'ok' : 'bad')
            : pill(sources.length + (sources.length === 1 ? ' fonte' : ' fontes'), 'plain'))));
      d.addEventListener('toggle', () => {
        if (!d.open || d.querySelector('.ep-body')) return;
        d.append(h('div', { class: 'ep-body' }, ...sources.map(e => sourceRow(e, { managed: Boolean(managed), preview }))));
      }, { once: false });
      epsBox.append(d);
    }
  };
  for (const s of seasonNums) {
    const b = h('button', { class: 'chip-btn', type: 'button', data: { s } }, 'Temporada ' + s, h('small', { text: seasons.get(s).size }));
    b.addEventListener('click', () => showSeason(s));
    pills.append(b);
  }
  const epSection = section('Episódios', 'tv', pills, epsBox);
  epSection.querySelector('h3').append(h('span', { class: 'count', text: fmt(rows.length) + ' entradas' + (rows.length >= 2000 ? ' (limitado)' : '') }));
  body.append(epSection, metadataSection({ kind: 'episode', title: it.title, managedSeries: it.managed_count > 0 }, meta));
  if (seasonNums.length) showSeason(seasonNums[0]);
  else epsBox.append(h('p', { class: 'note', text: 'Nenhum episódio numerado encontrado.' }));
}

function batchPanel(title, seasonNums) {
  const select = h('select', { attrs: { 'aria-label': 'Temporada' } }, new Option('Todas as temporadas', ''));
  for (const s of seasonNums) select.add(new Option('Temporada ' + s, String(s)));
  const start = btn('Adicionar ao Jellyfin', { cls: 'primary', ico: 'plus' });
  const progress = h('div', { class: 'progress hidden' });
  const pending = h('div');
  const panel = h('div', { class: 'batch' },
    h('strong', { text: 'Importar em lote' }),
    h('p', { text: 'Cada episódio é validado antes de virar STRM. Os indisponíveis ficam pendentes e são verificados de novo todos os dias.' }),
    h('div', { class: 'batch-row' }, h('div', { class: 'select' }, select, icon('chev')), start), progress, pending);

  start.addEventListener('click', async () => {
    const scope = select.value ? 'a temporada ' + select.value : 'todas as temporadas';
    if (!await ask('Importar ' + title, 'Validar e adicionar ' + scope + '? Apenas fontes acessíveis serão exportadas.', { ok: 'Importar' })) return;
    start.disabled = true;
    try {
      const payload = { title, source: state.source };
      if (select.value !== '') payload.season = Number(select.value);
      const r = await post('/series/batch', payload);
      toast(r.already_running ? 'Essa importação já está na fila' : 'Fila iniciada: ' + fmt(r.episodes) + ' episódios', 'info');
      stopTimers();
      const tick = () => pollJob(r.job_id, progress, start);
      await tick();
      timers.series = setInterval(tick, 3000);
    } catch (e) { toast('Erro: ' + e.message, 'bad'); start.disabled = false; }
  });

  api('/series/pending', { params: { title, source: state.source } }).then(d => {
    const waiting = d.items.filter(x => x.state === 'pending');
    if (waiting.length) pending.append(h('p', { class: 'note', text: fmt(waiting.length) + ' episódio(s) aguardando nova verificação automática.' }));
  }).catch(() => {});
  return panel;
}

async function pollJob(id, box, start) {
  try {
    const { job: j, unavailable } = await api('/series/job', { params: { id } });
    const pct = j.total ? Math.round((j.completed / j.total) * 100) : 0;
    const labels = { queued: 'Na fila', running: 'Processando', finished: 'Concluído', failed: 'Interrompido' };
    const bar = h('div', { class: 'bar' }, h('i'));
    bar.firstChild.style.width = pct + '%';
    box.classList.remove('hidden');
    box.replaceChildren(
      h('div', { class: 'progress-line' }, h('span', { text: labels[j.state] || j.state }), h('span', { text: fmt(j.completed) + ' / ' + fmt(j.total) + ' · ' + pct + '%' })),
      bar,
      h('div', { class: 'pills' }, pill(fmt(j.added) + ' adicionados', 'ok'), pill(fmt(j.already) + ' já existiam', 'plain'), pill(fmt(j.pending) + ' indisponíveis', j.pending ? 'warn' : 'plain')));
    if (unavailable.length) {
      box.append(h('details', { class: 'unavail' }, h('summary', { text: 'Ver indisponíveis (' + unavailable.length + ')' }),
        ...unavailable.map(u => h('div', { text: epCode(u.season, u.episode) + ' — ' + u.last_error }))));
    }
    if (j.state === 'finished' || j.state === 'failed') {
      stopTimers();
      start.disabled = false;
      if (j.error) box.append(h('p', { class: 'note', text: 'Erro: ' + j.error }));
      toast(j.state === 'finished' ? 'Importação concluída: ' + fmt(j.added) + ' episódios adicionados' : 'Importação interrompida', j.state === 'finished' ? 'ok' : 'bad');
    }
  } catch (e) { stopTimers(); start.disabled = false; box.replaceChildren(h('p', { class: 'note', text: 'Não foi possível consultar a fila: ' + e.message })); }
}

/* ============================== Gerenciados ============================== */
async function loadManaged() {
  const list = $('managedList');
  list.replaceChildren(...[1, 2, 3, 4].map(() => h('div', { class: 'mrow skeleton' }, h('div', { class: 'sk' }), h('div', { class: 'sk' }))));
  try {
    state.managed = await api('/managed');
    renderManaged();
  } catch (e) { list.replaceChildren(emptyState('alert', 'Não foi possível consultar', e.message)); }
}

function updateBadges(items) {
  const q = items.filter(x => x.status === 'quarantined').length;
  $('alertBadge').textContent = q;
  $('alertBadge').classList.toggle('hidden', !q);
  $('bellDot').classList.toggle('hidden', !q);
}

function renderManaged() {
  const { items, alerts } = state.managed;
  updateBadges(items);
  const active = items.filter(x => x.status === 'active').length;
  const quarantined = items.length - active;
  const series = new Set(items.filter(x => x.kind === 'episode').map(x => x.title)).size;
  const kpi = (label, value, cls) => h('div', { class: 'kpi ' + cls }, h('span', { text: label }), h('strong', { text: fmt(value) }));
  $('managedKpis').replaceChildren(kpi('STRMs', items.length, ''), kpi('Ativos', active, 'ok'), kpi('Em quarentena', quarantined, 'bad'), kpi('Séries', series, 'info'));

  const f = state.managedFilter, q = state.managedQuery.toLowerCase();
  let rows = items.filter(x => !q || x.title.toLowerCase().includes(q) || x.relative_path.toLowerCase().includes(q));
  if (f === 'movie') rows = rows.filter(x => x.kind === 'movie');
  if (f === 'episode') rows = rows.filter(x => x.kind === 'episode');
  if (f === 'quarantined') rows = rows.filter(x => x.status !== 'active');

  const list = $('managedList');
  list.replaceChildren();
  if (!rows.length) list.append(emptyState('shield', items.length ? 'Nada com esse filtro' : 'Nenhum STRM ainda', items.length ? 'Ajuste o filtro ou a busca.' : 'Abra um filme ou série no catálogo e adicione ao Jellyfin.'));

  const flat = f === 'quarantined';
  const movies = flat ? rows : rows.filter(x => x.kind === 'movie');
  for (const m of movies) list.append(managedRow(m, true));
  if (!flat) {
    const groups = new Map();
    for (const e of rows.filter(x => x.kind === 'episode')) { if (!groups.has(e.title)) groups.set(e.title, []); groups.get(e.title).push(e); }
    for (const [title, eps] of groups) list.append(seriesGroup(title, eps));
  }

  const tl = $('alertList');
  tl.replaceChildren();
  if (!alerts.length) tl.append(h('p', { class: 'note', text: 'Nenhuma notificação até agora.' }));
  for (const a of alerts) {
    const bad = a.event === 'unavailable';
    tl.append(h('div', { class: 'tl ' + (bad ? 'bad' : 'ok') },
      h('span', { class: 'ico' }, icon(bad ? 'alert' : 'check')),
      h('div', null, h('strong', { text: a.title }), h('p', { text: a.message }), h('small', { text: new Date(a.created_at).toLocaleString('pt-BR') }))));
  }
}

function epFromPath(p) { const m = /S(\d{1,3})E(\d{1,4})/i.exec(p); return m ? epCode(+m[1], +m[2]) : null; }

function managedActions(item, withMeta) {
  const box = h('div', { class: 'mrow-actions' });
  const check = btn('Verificar', { cls: 'sm', ico: 'refresh', title: 'Testar a fonte agora' });
  check.addEventListener('click', e => { e.stopPropagation(); busy(check, async () => {
    try {
      const r = await post('/recheck', { key: item.key });
      toast((r.ok ? 'Fonte OK · ' : 'Falhou (' + r.failures + '/' + r.required_failures + ') · ') + r.reason, r.ok ? 'ok' : 'bad', 6000);
      await loadManaged();
    } catch (err) { toast('Erro: ' + err.message, 'bad'); }
  }); });
  box.append(check);
  if (withMeta) {
    const meta = btn('Metadados', { cls: 'sm', ico: 'image' });
    meta.addEventListener('click', () => {
      openDrawer({ eyebrow: 'Filme gerenciado', title: item.title, meta: [pill(item.status === 'active' ? 'Ativo' : 'Quarentena', item.status === 'active' ? 'ok' : 'bad')] });
      $('dBody').append(h('div', { class: 'callout' }, icon('folder'), h('code', { text: item.relative_path })),
        metadataSection({ kind: 'movie', title: item.title, key: item.key }, null));
    });
    box.append(meta);
  }
  const del = btn('', { cls: 'sm danger', ico: 'trash', title: 'Excluir STRM' });
  del.setAttribute('aria-label', 'Excluir');
  del.addEventListener('click', async e => {
    e.stopPropagation();
    if (!await ask('Excluir STRM?', 'Remove apenas este STRM e o NFO/pôster criados pelo indexador.', { ok: 'Excluir', danger: true })) return;
    busy(del, async () => {
      try { await post('/delete', { key: item.key }); toast('STRM excluído', 'ok'); await loadManaged(); }
      catch (err) { toast('Exclusão: ' + err.message, 'bad'); }
    });
  });
  box.append(del);
  return box;
}

function statusPill(item) {
  return item.status === 'active' ? pill('Ativo', 'ok') : pill('Quarentena · ' + item.failures, 'bad');
}

function managedLabel(item, standalone) {
  if (item.kind !== 'episode') return item.title;
  const code = epFromPath(item.relative_path);
  return standalone ? item.title + (code ? ' · ' + code : '') : (code || item.title);
}

function managedRow(item, standalone) {
  const isEp = item.kind === 'episode';
  const main = h('div', { class: 'mrow-main' },
    h('div', { class: 'mrow-title' }, h('span', { text: managedLabel(item, standalone) }), statusPill(item)),
    h('div', { class: 'path', text: item.relative_path, title: item.relative_path }),
    item.last_error ? h('div', { class: 'err', text: item.last_error }) : null);
  const row = h('div', { class: 'mrow' });
  if (standalone) row.append(h('span', { class: 'thumb' + (item.status === 'active' ? '' : ' bad') }, icon(isEp ? 'tv' : 'film')));
  row.append(main, managedActions(item, item.kind === 'movie'));
  return row;
}

function seriesGroup(title, eps) {
  eps.sort((a, b) => a.relative_path.localeCompare(b.relative_path, 'pt-BR', { numeric: true }));
  const bad = eps.filter(x => x.status !== 'active').length;
  const del = btn('Excluir série', { cls: 'sm danger', ico: 'trash' });
  del.addEventListener('click', async e => {
    e.preventDefault(); e.stopPropagation();
    if (!await ask('Excluir “' + title + '”?', 'Remove todos os STRMs desta série e cancela os episódios pendentes.', { ok: 'Excluir série', danger: true })) return;
    busy(del, async () => {
      try { const r = await post('/delete', { series_title: title }); toast(fmt(r.removed) + ' episódios removidos', 'ok'); await loadManaged(); }
      catch (err) { toast('Erro: ' + err.message, 'bad'); }
    });
  });
  const g = h('details', { class: 'group' },
    h('summary', null,
      h('span', { class: 'thumb' + (bad ? ' bad' : '') }, icon('tv')),
      h('div', { class: 'mrow-main' },
        h('div', { class: 'mrow-title' }, h('span', { text: title }), pill(fmt(eps.length) + ' ep.', 'plain'), bad ? pill(bad + ' em quarentena', 'bad') : null),
        h('div', { class: 'path', text: 'Series/' + title })),
      h('div', { class: 'mrow-actions' }, del, h('span', { class: 'icon-btn chev' }, icon('chev')))));
  g.addEventListener('toggle', () => {
    if (g.open && !g.querySelector('.group-body')) g.append(h('div', { class: 'group-body' }, ...eps.map(e => managedRow(e, false))));
  });
  return g;
}

$('refreshManaged').addEventListener('click', e => busy(e.currentTarget, loadManaged));
$('managedFilter').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  state.managedFilter = b.dataset.f;
  $('managedFilter').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  if (state.managed) renderManaged();
});
let mTimer;
$('managedSearch').addEventListener('input', e => {
  clearTimeout(mTimer);
  mTimer = setTimeout(() => { state.managedQuery = e.target.value.trim(); if (state.managed) renderManaged(); }, 200);
});

/* ============================== Solicitações ============================== */
const REQ_STATE = {
  queued: ['Na fila', 'plain'], validating: ['Validando', 'info'], importing: ['Importando', 'info'],
  available: ['Adicionado', 'ok'], unavailable: ['Sem fonte', 'warn'], failed: ['Falhou', 'bad']
};
async function loadRequests(silent = false) {
  const list = $('requestList');
  if (!silent) list.replaceChildren(...[1, 2, 3].map(() => h('div', { class: 'mrow skeleton' }, h('div', { class: 'sk' }), h('div', { class: 'sk' }))));
  try {
    const data = await api('/requests');
    const c = data.counts || {};
    const active = (c.queued || 0) + (c.validating || 0) + (c.importing || 0);
    const kpi = (label, value, cls) => h('div', { class: 'kpi ' + cls }, h('span', { text: label }), h('strong', { text: fmt(value) }));
    $('requestKpis').replaceChildren(kpi('Em andamento', active, 'info'), kpi('Adicionados', c.available || 0, 'ok'),
      kpi('Sem fonte', c.unavailable || 0, 'bad'), kpi('Total', data.items.length, ''));
    $('reqBadge').textContent = active; $('reqBadge').classList.toggle('hidden', !active);
    list.replaceChildren();
    if (!data.items.length) {
      list.append(emptyState('bell', 'Nenhuma solicitação ainda', 'Quando alguém pedir um título pela busca do Jellyfin, ele aparece aqui.'));
      return;
    }
    for (const r of data.items) {
      const [label, cls] = REQ_STATE[r.state] || [r.state, 'plain'];
      const actions = h('div', { class: 'mrow-actions' });
      if (r.state === 'unavailable' || r.state === 'failed') {
        const retry = btn('Tentar de novo', { cls: 'sm', ico: 'refresh' });
        retry.addEventListener('click', () => busy(retry, async () => {
          try { await post('/requests/retry', { id: r.id }); toast('Pedido reenviado para a fila', 'ok'); await loadRequests(true); }
          catch (e) { toast(e.message, 'bad'); }
        }));
        actions.append(retry);
      }
      const what = r.kind === 'movie' ? (r.year ? 'Filme · ' + r.year : 'Filme') : (r.season !== null ? 'Série · temporada ' + r.season : 'Série · todas as temporadas');
      list.append(h('div', { class: 'mrow' },
        h('span', { class: 'thumb' + (r.state === 'failed' || r.state === 'unavailable' ? ' bad' : '') }, icon(r.kind === 'movie' ? 'film' : 'tv')),
        h('div', { class: 'mrow-main' },
          h('div', { class: 'mrow-title' }, h('span', { text: r.title }), pill(label, cls)),
          h('div', { class: 'req-meta' }, h('span', { text: what }), h('span', { text: 'por ' + r.user }), h('span', { text: new Date(r.created_at * 1000).toLocaleString('pt-BR') })),
          r.message ? h('div', { class: 'req-msg', text: r.message }) : null,
          r.path ? h('div', { class: 'path', text: r.path }) : null),
        actions));
    }
  } catch (e) { if (!silent) list.replaceChildren(emptyState('alert', 'Não foi possível consultar', e.message)); }
}
$('refreshRequests').addEventListener('click', e => busy(e.currentTarget, () => loadRequests()));

/* ============================== Listas ============================== */
async function loadSources() {
  const grid = $('sourceList');
  if (!grid.childElementCount) grid.replaceChildren(...[1, 2, 3].map(() => h('div', { class: 'file skeleton' }, h('div', { class: 'sk' }), h('div', { class: 'sk' }))));
  try {
    const st = await api('/playlists/status');
    renderJob(st);
    const counts = new Map((state.filters?.sources || []).map(s => [s.name, s.count]));
    const max = Math.max(1, ...counts.values());
    $('filesCount').textContent = fmt(st.files.length) + (st.files.length === 1 ? ' arquivo' : ' arquivos');
    grid.replaceChildren();
    if (!st.files.length) grid.append(emptyState('file', 'Nenhuma lista ainda', 'Envie um arquivo ou importe do GitHub para começar.'));
    for (const f of st.files) {
      const n = counts.get(f.name);
      const bar = h('div', { class: 'bar' }, h('i'));
      bar.firstChild.style.width = (n ? Math.max(3, (n / max) * 100) : 0) + '%';
      const actions = h('div', { class: 'batch-row' });
      if (f.github) {
        const ref = btn('Atualizar do GitHub', { cls: 'sm', ico: 'refresh' });
        ref.addEventListener('click', () => busy(ref, () => playlistAction(() => post('/playlists/refresh', { filename: f.name }), 'Lista atualizada')));
        actions.append(ref);
      }
      grid.append(h('div', { class: 'file' },
        h('div', { class: 'file-top' }, h('span', { class: 'file-ico' }, icon(f.github ? 'github' : 'file')),
          h('div', { class: 'file-name' }, h('strong', { text: f.name, title: f.name }), h('small', { text: bytes(f.size) }))),
        h('div', { class: 'file-stats' }, pill(n !== undefined ? fmt(n) + ' entradas' : 'não indexada', n !== undefined ? 'info' : 'warn'), pill(f.github ? 'GitHub' : 'Upload local', 'plain')),
        bar, actions.childElementCount ? actions : null));
    }
  } catch (e) { grid.replaceChildren(emptyState('alert', 'Falha ao listar arquivos', e.message)); }
}

function bytes(n) {
  if (n >= 1048576) return (n / 1048576).toLocaleString('pt-BR', { maximumFractionDigits: 1 }) + ' MB';
  if (n >= 1024) return (n / 1024).toLocaleString('pt-BR', { maximumFractionDigits: 0 }) + ' KB';
  return fmt(n) + ' B';
}

let lastJobState = null;
function renderJob(st) {
  const box = $('playlistJob');
  const running = st.state === 'running';
  box.classList.toggle('hidden', st.state === 'idle' || (!running && !st.message));
  box.classList.toggle('err', st.state === 'error');
  box.classList.toggle('done', st.state === 'done');
  $('playlistJobText').textContent = running ? 'Indexando listas… isso pode levar alguns minutos.' : (st.message || '');
  $('reindexPlaylists').disabled = running;
  clearInterval(timers.playlist);
  if (running) timers.playlist = setInterval(async () => {
    try { const s = await api('/playlists/status'); if (s.state !== 'running') { clearInterval(timers.playlist); await afterImport(s); } }
    catch { clearInterval(timers.playlist); }
  }, 2500);
  lastJobState = st.state;
}
async function afterImport(st) {
  if (st.state === 'done') toast('Catálogo atualizado', 'ok'); else if (st.state === 'error') toast('Falha na indexação: ' + st.message, 'bad', 7000);
  state.heroType = null;
  await loadMeta();
  if (state.view === 'sources') await loadSources(); else renderJob(st);
}

async function playlistAction(fn, okMsg = 'Lista recebida') {
  try {
    const r = await fn();
    toast(okMsg + (r.filename ? ': ' + r.filename : '') + ' · indexação iniciada', 'ok');
  } catch (e) { toast('Erro: ' + e.message, 'bad', 6000); }
  await loadSources();
}

function uploadFile(file) {
  return new Promise((resolve, reject) => {
    if (!/\.m3u8?$/i.test(file.name)) return reject(Error('Use um arquivo .m3u ou .m3u8'));
    if (file.size > 100 * 1024 * 1024) return reject(Error('Limite de 100 MiB'));
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/playlists/upload');
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    xhr.setRequestHeader('X-Playlist-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = e => { if (e.lengthComputable) $('dzHint').textContent = 'Enviando ' + file.name + ' · ' + Math.round(e.loaded / e.total * 100) + '%'; };
    xhr.onload = () => {
      let data = {}; try { data = JSON.parse(xhr.responseText); } catch { /* vazio */ }
      if (xhr.status === 401) location.replace('/login');
      xhr.status >= 200 && xhr.status < 300 ? resolve(data) : reject(Error(data.error || 'Falha no upload'));
    };
    xhr.onerror = () => reject(Error('Conexão interrompida'));
    xhr.send(file);
  });
}
async function handleFile(file) {
  if (!file) return;
  const hint = $('dzHint');
  await playlistAction(() => uploadFile(file), 'Upload concluído');
  hint.textContent = 'ou clique para escolher · até 100 MiB';
  $('playlistFile').value = '';
}
$('playlistFile').addEventListener('change', e => handleFile(e.target.files[0]));
const dz = $('dropzone');
['dragenter', 'dragover'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add('over'); }));
['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove('over'); }));
dz.addEventListener('drop', e => handleFile(e.dataTransfer.files[0]));
$('githubForm').addEventListener('submit', e => {
  e.preventDefault();
  const b = $('addGithub');
  busy(b, () => playlistAction(() => post('/playlists/github', { url: $('githubPlaylist').value.trim() }), 'Lista do GitHub adicionada'))
    .then(() => { $('githubPlaylist').value = ''; });
});
$('reindexPlaylists').addEventListener('click', async () => {
  if (!await ask('Reindexar todas as listas?', 'O catálogo é reconstruído e substituído somente quando a importação terminar com sucesso.', { ok: 'Reindexar' })) return;
  playlistAction(() => post('/playlists/reindex', {}), 'Reindexação');
});

/* ============================== Teclado ============================== */
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (!$('dialog').classList.contains('hidden')) return closeDialog(false);
    if (!$('catpop').classList.contains('hidden')) return closeCatPicker();
    if (!$('drawer').classList.contains('hidden')) return closeDrawer();
  }
  if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    e.preventDefault(); $('search').focus();
  }
});

/* ============================== Início ============================== */
(async () => {
  if (!await establishSession()) return;
  await loadMeta();
  route();
  api('/managed').then(d => updateBadges(d.items)).catch(() => {});
})();
