"""Canais ao vivo: escolha de canais das listas e geração de uma lista M3U própria.

Funciona como o "Channels" do Dispatcharr:
  - à esquerda, todos os canais ao vivo encontrados nas listas (filtro por nome, grupo e lista);
  - à direita, a seleção: ordem, número, nome/grupo editáveis e teste de cada canal;
  - a seleção vira uma lista M3U com endereço próprio (protegido por chave), pronta para
    a TV ao vivo do Jellyfin, o app StreamBox ou qualquer player IPTV.

Os canais são lidos direto dos arquivos em playlists/ (o catálogo de filmes/séries não guarda
logo nem tvg-id). A seleção fica em <INDEXER_STATE_DIR>/channels.sqlite com uma cópia dos dados,
então ela continua funcionando mesmo se a lista de origem mudar.
"""
import hashlib
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import media_control as media
import content_filter
from indexer import PLAYLISTS, SUPPORTED, EXTINF_ATTR, parse_title, norm

STATE_DB = media.DATA / 'channels.sqlite'
VOD_EXT = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v', '.webm', '.mpg', '.mpeg', '.flv')
MAX_SELECTED = 5000

_lock = threading.Lock()
_index = {'sig': None, 'items': [], 'by_id': {}}
_test = {'running': False, 'total': 0, 'done': 0, 'ok': 0, 'fail': 0, 'started': 0.0}


# ------------------------------------------------------------------ banco da seleção
def connect():
    media.DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE_DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.executescript('''
        CREATE TABLE IF NOT EXISTS selected(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, group_title TEXT NOT NULL DEFAULT '',
            logo TEXT NOT NULL DEFAULT '', tvg_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, number INTEGER,
            status TEXT NOT NULL DEFAULT 'untested', detail TEXT NOT NULL DEFAULT '', checked_at REAL,
            added_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    ''')
    return c


def _setting(c, key, default=''):
    row = c.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _set_setting(c, key, value):
    c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def token():
    with connect() as c:
        t = _setting(c, 'token')
        if not t:
            t = secrets.token_urlsafe(18)
            _set_setting(c, 'token', t)
        return t


# ------------------------------------------------------------------ índice das listas
def _is_live(title, group, url):
    kind = parse_title(title, group or '')[0]
    if kind in ('movie', 'episode'):
        return False
    path = urlsplit(url).path.lower()
    return not path.endswith(VOD_EXT)


def _scan():
    files = sorted(p for p in PLAYLISTS.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED) if PLAYLISTS.is_dir() else []
    sig = tuple((p.name, p.stat().st_mtime, p.stat().st_size) for p in files)
    if sig == _index['sig']:
        return
    items, seen = [], set()
    for path in files:
        title, attrs = None, {}
        with path.open('r', encoding='utf-8-sig', errors='replace') as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith('#EXTINF:'):
                    header, sep, display = line.partition(',')
                    attrs = dict(EXTINF_ATTR.findall(header))
                    title = (display if sep else '').strip() or attrs.get('tvg-name', '').strip()
                elif not line or line.startswith('#'):
                    continue
                elif title:
                    url, name, a = line, title, attrs
                    title, attrs = None, {}
                    parts = urlsplit(url)
                    if parts.scheme not in ('http', 'https') or not parts.hostname:
                        continue
                    group = a.get('group-title', '').strip()
                    if not _is_live(name, group, url):
                        continue
                    cid = hashlib.sha1(url.encode()).hexdigest()[:16]
                    if cid in seen:
                        continue
                    seen.add(cid)
                    items.append({'id': cid, 'name': name[:200], 'group': group[:200], 'logo': a.get('tvg-logo', '')[:500],
                                  'tvg_id': a.get('tvg-id', '')[:200], 'url': url, 'source': path.name,
                                  'n': norm(name)})
    _index.update(sig=sig, items=items, by_id={x['id']: x for x in items})


def index():
    with _lock:
        _scan()
        return _index['items'], _index['by_id']


def _hidden(group, cfg):
    """Grupo bloqueado pelo filtro de conteúdo (ex.: canais adultos)."""
    return not cfg['show_in_panel'] and content_filter.is_blocked(group, cfg)


def _filtered(q='', group='', source=''):
    items, _ = index()
    cfg = content_filter.load()
    term = norm(q) if q else ''
    out = []
    for x in items:
        if (group and x['group'] != group) or (source and x['source'] != source) or _hidden(x['group'], cfg):
            continue
        if term and term not in x['n']:
            continue
        out.append(x)
    return out


def browse(q='', group='', source='', page=1, limit=60):
    rows = _filtered(q, group, source)
    with connect() as c:
        chosen = {r['id'] for r in c.execute('SELECT id FROM selected')}
    start = (page - 1) * limit
    public = [{k: x[k] for k in ('id', 'name', 'group', 'logo', 'source')} | {'selected': x['id'] in chosen}
              for x in rows[start:start + limit]]
    return {'total': len(rows), 'page': page, 'limit': limit, 'items': public}


def facets(source=''):
    items, _ = index()
    cfg = content_filter.load()
    groups, sources = {}, {}
    for x in items:
        if _hidden(x['group'], cfg):
            continue
        sources[x['source']] = sources.get(x['source'], 0) + 1
        if not source or x['source'] == source:
            groups[x['group']] = groups.get(x['group'], 0) + 1
    return {'groups': sorted(({'name': k, 'count': v} for k, v in groups.items()), key=lambda g: (-g['count'], g['name'])),
            'sources': [{'name': k, 'count': v} for k, v in sorted(sources.items())],
            'total': sum(sources.values())}


# ------------------------------------------------------------------ seleção
def selected():
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM selected ORDER BY position, added_at')]
        settings = {'epg_url': _setting(c, 'epg_url'), 'only_ok': _setting(c, 'only_ok', '1') == '1'}
    import epg
    names = epg.labels()
    for r in rows:
        r.pop('url', None)            # o endereço da fonte não vai para o navegador
        r['epg'] = names.get(r['id'])
    return {'items': rows, 'settings': settings, 'token': token(), 'test': dict(_test), 'epg': epg.summary()}


def add(ids=None, q='', group='', source=''):
    _, by_id = index()
    if ids:
        if not isinstance(ids, list) or len(ids) > MAX_SELECTED:
            raise ValueError('Seleção inválida')
        cfg = content_filter.load()
        chosen = [by_id[i] for i in ids if isinstance(i, str) and i in by_id and not _hidden(by_id[i]['group'], cfg)]
    else:
        if not (q or group or source):
            raise ValueError('Escolha um grupo, uma lista ou uma busca para adicionar em lote')
        chosen = _filtered(q, group, source)
    now = time.time()
    with connect() as c:
        count = c.execute('SELECT COUNT(*) FROM selected').fetchone()[0]
        pos = (c.execute('SELECT MAX(position) FROM selected').fetchone()[0] or 0)
        added = 0
        for x in chosen:
            if count + added >= MAX_SELECTED:
                break
            if c.execute('SELECT 1 FROM selected WHERE id=?', (x['id'],)).fetchone():
                continue
            pos += 1
            c.execute('''INSERT INTO selected(id,name,group_title,logo,tvg_id,url,source,position,added_at)
                         VALUES(?,?,?,?,?,?,?,?,?)''',
                      (x['id'], x['name'], x['group'], x['logo'], x['tvg_id'], x['url'], x['source'], pos, now))
            added += 1
    if added:
        import epg
        epg.auto_match()
    return {'added': added, 'limit_reached': count + added >= MAX_SELECTED}


def remove(ids=None, failed=False):
    with connect() as c:
        if failed:
            n = c.execute("DELETE FROM selected WHERE status='fail'").rowcount
        else:
            if not isinstance(ids, list):
                raise ValueError('Seleção inválida')
            n = 0
            for i in ids[:MAX_SELECTED]:
                n += c.execute('DELETE FROM selected WHERE id=?', (str(i),)).rowcount
    return {'removed': n}


def update(cid, name=None, group=None, number=None):
    fields, params = [], []
    if name is not None:
        name = str(name).strip()
        if not 1 <= len(name) <= 200:
            raise ValueError('Nome inválido')
        fields.append('name=?'); params.append(name)
    if group is not None:
        fields.append('group_title=?'); params.append(str(group).strip()[:200])
    if number is not None:
        if number in ('', None):
            fields.append('number=NULL')
        else:
            n = int(number)
            if not 0 <= n <= 99999:
                raise ValueError('Número inválido')
            fields.append('number=?'); params.append(n)
    if not fields:
        return {'ok': True}
    with connect() as c:
        c.execute('UPDATE selected SET ' + ', '.join(fields) + ' WHERE id=?', (*params, str(cid)))
    import epg
    epg.state['dirty'] = True
    return {'ok': True}


def reorder(ids):
    if not isinstance(ids, list) or len(ids) > MAX_SELECTED:
        raise ValueError('Ordem inválida')
    with connect() as c:
        for pos, i in enumerate(ids, 1):
            c.execute('UPDATE selected SET position=? WHERE id=?', (pos, str(i)))
    return {'ok': True}


def renumber(start=1):
    with connect() as c:
        ids = [r['id'] for r in c.execute('SELECT id FROM selected ORDER BY position, added_at')]
        for n, i in enumerate(ids, int(start)):
            c.execute('UPDATE selected SET number=? WHERE id=?', (n, i))
    return {'ok': True}


def save_settings(epg_url=None, only_ok=None, new_token=False):
    with connect() as c:
        if epg_url is not None:
            epg_url = str(epg_url).strip()
            if epg_url and not re.match(r'^https?://', epg_url):
                raise ValueError('O EPG deve começar com http:// ou https://')
            _set_setting(c, 'epg_url', epg_url[:1000])
        if only_ok is not None:
            _set_setting(c, 'only_ok', '1' if only_ok else '0')
        if new_token:
            _set_setting(c, 'token', secrets.token_urlsafe(18))
    return selected()


# ------------------------------------------------------------------ teste dos canais
def _check(row):
    ok, detail = media.validate_media(row['url'])
    with connect() as c:
        c.execute('UPDATE selected SET status=?, detail=?, checked_at=? WHERE id=?',
                  ('ok' if ok else 'fail', str(detail)[:200], time.time(), row['id']))
    with _lock:
        _test['done'] += 1
        _test['ok' if ok else 'fail'] += 1


def start_test(ids=None, only_untested=False):
    with _lock:
        if _test['running']:
            return dict(_test)
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT id, url, status FROM selected ORDER BY position')]
    if ids:
        wanted = set(map(str, ids))
        rows = [r for r in rows if r['id'] in wanted]
    if only_untested:
        rows = [r for r in rows if r['status'] == 'untested']
    with _lock:
        _test.update(running=True, total=len(rows), done=0, ok=0, fail=0, started=time.time())

    def run():
        try:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(pool.map(_check, rows))
        finally:
            with _lock:
                _test['running'] = False
    threading.Thread(target=run, daemon=True, name='channel-test').start()
    return dict(_test)


def test_status():
    return dict(_test)


# ------------------------------------------------------------------ logos
LOGO_DIR = media.DATA / 'channel_logos'
LOGO_MAX = 400_000
IMAGE_TYPES = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/webp': '.webp', 'image/gif': '.gif', 'image/svg+xml': '.svg'}


def logo(cid):
    """Baixa (uma vez) e devolve o logo do canal: (bytes, tipo) ou None.

    Passar pelo indexador evita bloquear imagens externas no painel e não expõe o
    navegador do administrador aos servidores das listas."""
    if not re.fullmatch(r'[0-9a-f]{16}', cid or ''):
        return None
    LOGO_DIR.mkdir(parents=True, exist_ok=True)
    for path in LOGO_DIR.glob(cid + '.*'):
        if path.suffix == '.none':
            return None
        typ = next((t for t, ext in IMAGE_TYPES.items() if ext == path.suffix), 'image/png')
        return path.read_bytes(), typ
    _, by_id = index()
    url = (by_id.get(cid) or {}).get('logo')
    if not url:
        with connect() as c:
            row = c.execute('SELECT logo FROM selected WHERE id=?', (cid,)).fetchone()
        url = row['logo'] if row else ''
    try:
        if not url:
            raise ValueError('sem logo')
        _, typ, data, _ = media.get_bytes(url, LOGO_MAX)
        if typ not in IMAGE_TYPES or not data or len(data) >= LOGO_MAX:
            raise ValueError('não é imagem')
        (LOGO_DIR / (cid + IMAGE_TYPES[typ])).write_bytes(data)
        return data, typ
    except Exception:
        (LOGO_DIR / (cid + '.none')).write_bytes(b'')   # não tenta de novo a cada abertura
        return None


# ------------------------------------------------------------------ lista gerada
def _attr(v):
    return str(v or '').replace('"', "'").replace('\n', ' ').strip()


def valid_token(t):
    return bool(t) and secrets.compare_digest(str(t), token())


def playlist(t, base=''):
    """Lista M3U. [base] = endereço público do indexador (para apontar o guia de programação)."""
    if not valid_token(t):
        return None
    import epg
    with connect() as c:
        only_ok = _setting(c, 'only_ok', '1') == '1'
        guide = _setting(c, 'epg_url')
        rows = [dict(r) for r in c.execute('SELECT * FROM selected ORDER BY position, added_at')]
        mapped = c.execute('SELECT 1 FROM selected WHERE epg_channel IS NOT NULL LIMIT 1').fetchone()
    if mapped and base:
        guide = f'{base}/strm-api/channels.xml?t={t}'   # nosso guia, só com os canais da lista
    header = '#EXTM3U' + (f' x-tvg-url="{_attr(guide)}" url-tvg="{_attr(guide)}"' if guide else '')
    lines = [header]
    for r in rows:
        if only_ok and r['status'] != 'ok':
            continue
        attrs = [f'tvg-id="{_attr(epg.out_id(r))}"', f'tvg-name="{_attr(r["name"])}"']
        if r['logo']:
            attrs.append(f'tvg-logo="{_attr(r["logo"])}"')
        if r['number'] is not None:
            attrs.append(f'tvg-chno="{r["number"]}"')
        attrs.append(f'group-title="{_attr(r["group_title"])}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{_attr(r["name"])}')
        lines.append(r['url'])
    return '\n'.join(lines) + '\n'
