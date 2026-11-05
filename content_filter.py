"""Filtro de conteúdo: categorias bloqueadas (ex.: canais adultos).

O conteúdo bloqueado não aparece na busca/solicitação do Jellyfin, do app StreamBox e do
StreamBox Web, e não pode ser solicitado nem importado por esses caminhos. No painel
administrativo ele também fica oculto, a menos que "mostrar no painel" esteja ligado.

A configuração fica em <INDEXER_STATE_DIR>/content_filter.json:
  {"auto": true, "blocked": ["Categoria X", ...], "allowed": [...], "show_in_panel": false}
- auto: bloqueia sozinho categorias com nomes como "Adultos", "XXX", "+18", "Hot"...
- blocked: categorias bloqueadas manualmente.
- allowed: exceções (categorias que o filtro automático pegaria, mas foram liberadas).
"""
import json
import re
import sqlite3
import threading

import media_control as media
from indexer import DB

FILE = media.DATA / 'content_filter.json'
DEFAULTS = {'auto': True, 'blocked': [], 'allowed': [], 'show_in_panel': False}

# Palavras que indicam conteúdo adulto no NOME DA CATEGORIA (não no título dos filmes)
ADULT = re.compile(
    r'adult|adulto|\bxxx\b|\+\s?18|18\s?\+|porn|hentai|er[oó]tic|sexy|'
    r'\bhot\b(?!\s*wheels)|playboy|onlyfans|brazzers|priv[eê]\b|\bnsfw\b|\bvivid\b',
    re.IGNORECASE,
)

_lock = threading.Lock()
_cache = {'mtime': None, 'cats': []}


def load():
    try:
        data = json.loads(FILE.read_text('utf-8'))
    except (OSError, ValueError):
        data = {}
    out = dict(DEFAULTS)
    out.update({k: data[k] for k in DEFAULTS if k in data})
    out['blocked'] = [str(x) for x in out['blocked'] if isinstance(x, str)][:2000]
    out['allowed'] = [str(x) for x in out['allowed'] if isinstance(x, str)][:2000]
    out['auto'] = bool(out['auto'])
    out['show_in_panel'] = bool(out['show_in_panel'])
    return out


def save(data):
    cfg = load()
    if 'auto' in data:
        cfg['auto'] = bool(data['auto'])
    if 'show_in_panel' in data:
        cfg['show_in_panel'] = bool(data['show_in_panel'])
    for key in ('blocked', 'allowed'):
        if key in data:
            if not isinstance(data[key], list) or len(data[key]) > 2000:
                raise ValueError('Lista de categorias inválida')
            cfg[key] = sorted({str(x)[:300] for x in data[key] if isinstance(x, str)})
    media.DATA.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), 'utf-8')
    tmp.replace(FILE)
    return status()


def is_adult(name):
    return bool(name and ADULT.search(name))


def _categories():
    """Categorias do catálogo com contagem (cache até o catálogo mudar)."""
    try:
        mtime = DB.stat().st_mtime
    except OSError:
        return []
    with _lock:
        if _cache['mtime'] != mtime:
            with sqlite3.connect(f'file:{DB}?mode=ro', uri=True, timeout=15) as db:
                _cache['cats'] = [(name or '', count) for name, count in
                                  db.execute('SELECT category, COUNT(*) FROM entries GROUP BY category')]
            _cache['mtime'] = mtime
        return list(_cache['cats'])


def blocked_set(cfg=None):
    cfg = cfg or load()
    blocked = set(cfg['blocked'])
    if cfg['auto']:
        allowed = set(cfg['allowed'])
        blocked |= {name for name, _ in _categories() if is_adult(name) and name not in allowed}
    return blocked


def where(column='category', for_panel=False):
    """Trecho SQL (e parâmetros) que exclui as categorias bloqueadas.

    for_panel=True respeita a opção "mostrar no painel" (nesse caso não filtra)."""
    cfg = load()
    if for_panel and cfg['show_in_panel']:
        return '1=1', []
    names = sorted(blocked_set(cfg))
    if not names:
        return '1=1', []
    return f"COALESCE({column},'') NOT IN ({','.join('?' * len(names))})", names


def status():
    cfg = load()
    blocked = blocked_set(cfg)
    cats = []
    for name, count in sorted(_categories(), key=lambda x: -x[1]):
        cats.append({
            'name': name or 'Sem categoria', 'raw': name, 'count': count,
            'blocked': name in blocked,
            'adult': is_adult(name),
            'auto': cfg['auto'] and is_adult(name),
            'manual': name in cfg['blocked'],
            'allowed': name in cfg['allowed'],
        })
    return {**cfg, 'categories': cats, 'blocked_count': len(blocked),
            'blocked_titles': sum(c['count'] for c in cats if c['blocked'])}
