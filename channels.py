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
import json
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qsl, quote, urljoin
from urllib.request import Request

import media_control as media
import content_filter
from indexer import PLAYLISTS, SUPPORTED, EXTINF_ATTR, parse_title, norm

STATE_DB = media.DATA / 'channels.sqlite'
VOD_EXT = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.m4v', '.webm', '.mpg', '.mpeg', '.flv')
MAX_SELECTED = 5000

_lock = threading.Lock()
_index = {'sig': None, 'items': [], 'by_id': {}}
_test = {'running': False, 'total': 0, 'done': 0, 'ok': 0, 'fail': 0, 'started': 0.0}
DEFAULT_UA = 'Mozilla/5.0 (Linux; Android 12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
_HDR_OK = re.compile(r'^[\x20-\x7e]{1,300}$')
_QUALITY = re.compile(r'\((?:\d{3,4}p|[^)]*geo[^)]*|[^)]*block[^)]*)\)|\[[^\]]*\]|\b(?:hd|fhd|sd|4k|uhd|h265|hevc|alt|backup|\d{3,4}p)\b', re.I)


def _hdr(value):
    """Valor de cabeçalho seguro (sem quebra de linha, tamanho limitado)."""
    value = (value or '').strip().strip('"')
    return value if _HDR_OK.match(value) else ''


def _split_kodi(url):
    """'http://x/canal.m3u8|User-Agent=Abc&Referer=http://y' -> url limpa + cabeçalhos."""
    if '|' not in url:
        return url, {}
    base, _, opts = url.partition('|')
    found = {}
    for k, v in parse_qsl(opts, keep_blank_values=False):
        k = k.strip().lower()
        if k in ('user-agent', 'http-user-agent'):
            found['ua'] = v
        elif k in ('referer', 'referrer', 'http-referrer', 'http-referer'):
            found['referer'] = v
    return base.strip(), found


def match_keys(tvg_id, name):
    """Chaves para achar o mesmo canal em outras listas: tvg-id (sem a variante @HD) e nome limpo."""
    keys = []
    base = (tvg_id or '').split('@')[0].strip().lower()
    if base:
        keys.append('id:' + base)
    n = norm(_QUALITY.sub(' ', name or ''))
    if len(n) >= 2:
        keys.append('n:' + n)
    return keys


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
        CREATE TABLE IF NOT EXISTS alternates(
            channel_id TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', url TEXT NOT NULL,
            ua TEXT NOT NULL DEFAULT '', referer TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
            position INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'untested',
            detail TEXT NOT NULL DEFAULT '', checked_at REAL, PRIMARY KEY(channel_id, id));
    ''')
    cols = {r['name'] for r in c.execute('PRAGMA table_info(selected)')}
    for col in ('ua', 'referer', 'hls'):
        if col not in cols:
            c.execute(f"ALTER TABLE selected ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
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
# Endereços típicos de transmissão ao vivo (Dispatcharr /proxy/, Xtream /live/, painéis /play/…)
LIVE_PATH = re.compile(r'/(?:live|proxy|channels?|stream)/', re.I)


def _is_live(title, group, url, attrs=None):
    path = urlsplit(url).path.lower()
    if path.endswith(VOD_EXT) or '/movie/' in path or '/series/' in path:
        return False                                    # arquivo de filme/episódio
    kind = parse_title(title, group or '')[0]
    if kind == 'episode':
        return False
    if kind == 'movie':
        # Grupo "Filmes…" também é usado para CANAIS (HBO, Telecine, AXN no Dispatcharr, por exemplo):
        # entra como canal quando há sinal claro de ao vivo (número de canal, caminho de live, formato Xtream).
        return bool((attrs or {}).get('tvg-chno')) or bool(LIVE_PATH.search(path)) or bool(XTREAM_LIVE.match(url))
    return True


def _scan():
    files = sorted(p for p in PLAYLISTS.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED) if PLAYLISTS.is_dir() else []
    try:
        import link_pruner
        dead = link_pruner.dead_urls()
    except Exception:
        dead = frozenset()
    sig = tuple((p.name, p.stat().st_mtime, p.stat().st_size) for p in files) + (len(dead),)
    if sig == _index['sig']:
        return
    items, seen = [], set()
    for path in files:
        title, attrs, opts = None, {}, {}
        with path.open('r', encoding='utf-8-sig', errors='replace') as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith('#EXTINF:'):
                    header, sep, display = line.partition(',')
                    attrs = dict(EXTINF_ATTR.findall(header))
                    title = (display if sep else '').strip() or attrs.get('tvg-name', '').strip()
                    opts = {}
                    for k in ('user-agent', 'http-user-agent'):
                        if attrs.get(k):
                            opts['ua'] = attrs[k]
                    for k in ('referer', 'referrer', 'http-referrer'):
                        if attrs.get(k):
                            opts['referer'] = attrs[k]
                elif line.startswith('#EXTVLCOPT:'):
                    # cabeçalhos exigidos pelo canal (formato do VLC, usado pelas listas iptv-org)
                    k, _, v = line[len('#EXTVLCOPT:'):].partition('=')
                    k = k.strip().lower()
                    if k == 'http-user-agent':
                        opts['ua'] = v
                    elif k in ('http-referrer', 'http-referer'):
                        opts['referer'] = v
                elif line.startswith('#EXTHTTP:'):
                    try:
                        hdrs = {str(k).lower(): str(v) for k, v in json.loads(line[len('#EXTHTTP:'):]).items()}
                        if hdrs.get('user-agent'):
                            opts['ua'] = hdrs['user-agent']
                        if hdrs.get('referer') or hdrs.get('referrer'):
                            opts['referer'] = hdrs.get('referer') or hdrs.get('referrer')
                    except (ValueError, AttributeError):
                        pass
                elif not line or line.startswith('#'):
                    continue
                elif title:
                    url, kodi = _split_kodi(line)
                    name, a, o = title, attrs, {**opts, **kodi}
                    title, attrs, opts = None, {}, {}
                    parts = urlsplit(url)
                    if parts.scheme not in ('http', 'https') or not parts.hostname:
                        continue
                    group = a.get('group-title', '').strip()
                    if not _is_live(name, group, url, a) or url in dead:
                        continue
                    cid = hashlib.sha1(url.encode()).hexdigest()[:16]
                    if cid in seen:
                        continue
                    seen.add(cid)
                    items.append({'id': cid, 'name': name[:200], 'group': group[:200], 'logo': a.get('tvg-logo', '')[:500],
                                  'tvg_id': a.get('tvg-id', '')[:200], 'url': url, 'source': path.name,
                                  'ua': _hdr(o.get('ua')), 'referer': _hdr(o.get('referer')),
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
        settings = {'epg_url': _setting(c, 'epg_url'), 'only_ok': _setting(c, 'only_ok', '1') == '1',
                    'link_base': _setting(c, 'link_base'), 'stream_mode': _setting(c, 'stream_mode', 'auto'),
                    'auto_organize': _setting(c, 'auto_organize', '')}
        alts = {r['channel_id']: (r['n'], r['ok'] or 0) for r in c.execute(
            "SELECT channel_id, COUNT(*) n, SUM(status='ok') ok FROM alternates GROUP BY channel_id")}
    import epg
    names = epg.labels()
    for r in rows:
        r.pop('url', None)            # o endereço da fonte não vai para o navegador
        r['epg'] = names.get(r['id'])
        r['alt_count'], r['alt_ok'] = alts.get(r['id'], (0, 0))
        r['headers'] = bool(r.pop('ua', '') or r.pop('referer', ''))
        r.pop('referer', None)
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
            c.execute('''INSERT INTO selected(id,name,group_title,logo,tvg_id,url,source,position,added_at,ua,referer)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                      (x['id'], x['name'], x['group'], x['logo'], x['tvg_id'], x['url'], x['source'], pos, now, x['ua'], x['referer']))
            added += 1
    if added:
        import epg
        epg.auto_match()
        mode = auto_organize_mode()
        if mode:
            organize(mode)          # organização automática ligada: o canal novo já vai para a sua categoria
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
        c.execute('DELETE FROM alternates WHERE channel_id NOT IN (SELECT id FROM selected)')
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


# ------------------------------------------------------------------ organização automática por categoria
# Ordem das categorias, início do bloco de números (como na TV a cabo), palavras do GRUPO da lista e do NOME.
CATEGORIES = [
    ('Abertos', 1,
     r'abert|nacionais|regionais|locais|\bgeneral\b|variedades|entertainment',
     r'\b(globo|sbt|record|band|bandeirantes|rede ?tv|cultura|tv brasil|gazeta|cnt|rede ?vida|tv aparecida|'
     r'tv camara|tv senado|tv justi|canal sete|chroma|futura)\b(?!.*news)'),
    ('Notícias', 100, r'not[ií]cia|\bnews\b|jornal|jornalismo',
     r'news|cnn|bandnews|globonews|jovem pan|record news|sbt news|cnbc|bloomberg|euronews|dw\b|bbc world|al jazeera'),
    ('Esportes', 200, r'esporte|sport|futebol|football|soccer|\bufc\b|luta|combate|premiere',
     r'espn|sportv|premiere|combate|band ?sports|fox sports|tnt sports|dazn|eleven|goat|cazé|caze|nba|nfl|xsports|sport'),
    ('Filmes e Séries', 300, r'filme|s[eé]rie|movie|cinema|\btelecine\b|\bhbo\b',
     r'hbo|telecine|cinemax|megapix|\btnt\b|\baxn\b|warner|sony|universal|paramount|space|\bfx\b|studio|'
     r'star channel|amc|film|cine|runtime|a&e|\bae\b|lifetime|tcm|prime ?box'),
    ('Infantil', 400, r'kid|infantil|crian[cç]a|anima[cç]|cartoon|desenho|anime',
     r'cartoon|disney|nick|gloob|discovery kids|boomerang|baby|tooncast|zoomoo|kids|ra-tim-bum|gloobinho|otaku|anime'),
    ('Documentários', 500, r'document|educa|ci[eê]ncia|natureza|hist[oó]ri|cultura',
     r'discovery|history|nat ?geo|national geographic|animal planet|curiosity|smithsonian|tlc|h2\b|science'),
    ('Música', 600, r'm[uú]sic|music|\bclipes?\b|r[aá]dio',
     r'mtv|music|vh1|bis\b|multishow|clubbing|trace|kiss|hits'),
    ('Religiosos', 700, r'relig|gospel|cat[oó]lic|evang|crist[aã]o',
     r'can[cç][aã]o nova|aparecida|rede vida|gospel|novo tempo|rit\b|boa vontade|evang|igreja|catol'),
]
OTHER = ('Outros', 800)
_CAT_RX = [(n, b, re.compile(g, re.I), re.compile(nm, re.I)) for n, b, g, nm in CATEGORIES]


def classify(name, group=''):
    """Categoria padrão do canal: primeiro pelo grupo da lista, depois pelo nome."""
    g, n = group or '', name or ''
    for cat, _, _, _ in _CAT_RX:                     # já organizado antes: mantém
        if g.strip().lower() == cat.lower():
            return cat
    for cat, _, grx, nrx in _CAT_RX:
        if grx.search(g) and not (cat == 'Abertos' and _CAT_RX[1][3].search(n)):
            return cat
    # pelo nome: categorias específicas antes de "Abertos" ("Band Sports" é esporte, não a Band)
    for cat, _, _, nrx in _CAT_RX[1:] + _CAT_RX[:1]:
        if nrx.search(n):
            return cat
    return OTHER[0]


def _block(cat):
    return next((b for n, b, _, _ in _CAT_RX if n == cat), OTHER[1])


def organize(numbering='seq'):
    """Classifica os canais da lista, agrupa por categoria (mantendo a ordem dentro de cada uma) e renumera.
    numbering: 'seq' (1, 2, 3…), 'blocks' (Abertos 1+, Notícias 100+, Esportes 200+…) ou 'keep'."""
    if numbering not in ('seq', 'blocks', 'keep'):
        raise ValueError('Numeração inválida')
    order = [n for n, _, _, _ in _CAT_RX] + [OTHER[0]]
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT id, name, group_title FROM selected ORDER BY position, added_at')]
        for r in rows:
            r['cat'] = classify(r['name'], r['group_title'])
        rows.sort(key=lambda r: order.index(r['cat']))          # estável: mantém a ordem dentro da categoria
        counts, seq, inblock = {}, 0, {}
        for pos, r in enumerate(rows, 1):
            counts[r['cat']] = counts.get(r['cat'], 0) + 1
            if numbering == 'seq':
                seq += 1
                number = seq
            elif numbering == 'blocks':
                inblock[r['cat']] = inblock.get(r['cat'], -1) + 1
                number = _block(r['cat']) + inblock[r['cat']]
            else:
                number = None
            if number is None:
                c.execute('UPDATE selected SET group_title=?, position=? WHERE id=?', (r['cat'], pos, r['id']))
            else:
                c.execute('UPDATE selected SET group_title=?, position=?, number=? WHERE id=?', (r['cat'], pos, number, r['id']))
        _set_setting(c, 'organize', numbering)
    import epg
    epg.state['dirty'] = True
    return {'ok': True, 'categories': [{'name': k, 'count': counts[k]} for k in order if k in counts]}


def auto_organize_mode():
    with connect() as c:
        return _setting(c, 'auto_organize', '')


def set_auto_organize(mode):
    if mode not in ('', 'seq', 'blocks', 'keep'):
        raise ValueError('Modo inválido')
    with connect() as c:
        _set_setting(c, 'auto_organize', mode)
    return {'ok': True, 'auto_organize': mode}


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


def save_settings(epg_url=None, only_ok=None, new_token=False, link_base=None, stream_mode=None):
    with connect() as c:
        if stream_mode is not None:
            if stream_mode not in ('auto', 'proxy', 'direct'):
                raise ValueError('Modo inválido')
            _set_setting(c, 'stream_mode', stream_mode)
        if link_base is not None:
            # Endereço usado nos links mostrados no painel (ex.: http://192.168.1.16:8794 na rede local)
            link_base = str(link_base).strip().rstrip('/')
            if link_base and not re.fullmatch(r'https?://[A-Za-z0-9.\-]+(:\d{1,5})?', link_base):
                raise ValueError('Use só protocolo, endereço e porta, ex.: http://192.168.1.16:8794')
            _set_setting(c, 'link_base', link_base)
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


# ------------------------------------------------------------------ acesso às fontes (com cabeçalhos)
def _check_host(url):
    import epg
    epg._check(url)          # bloqueia rede interna, exceto INDEXER_TRUSTED_HOSTS


def _open(url, ua='', referer='', timeout=12):
    _check_host(url)
    headers = {'User-Agent': ua or DEFAULT_UA, 'Accept': '*/*'}
    if referer:
        headers['Referer'] = referer
        p = urlsplit(referer)
        if p.scheme and p.hostname:
            headers['Origin'] = f'{p.scheme}://{p.netloc}'
    return media.OPENER.open(Request(url, headers=headers), timeout=timeout)


def _looks_html(data):
    return data[:200].lstrip().lower().startswith((b'<html', b'<!doctype', b'<?xml'))


def probe(url, ua='', referer='', depth=0):
    """Confere se a fonte entrega vídeo: (ok, detalhe, 'hls'|'ts')."""
    try:
        with _open(url, ua, referer, timeout=10) as r:
            final = r.geturl()
            ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            data = r.read(256 * 1024)
        is_hls = data.lstrip(b'\xef\xbb\xbf').startswith(b'#EXTM3U') or 'mpegurl' in ctype
        if is_hls:
            if depth >= 2:
                return True, 'Lista HLS respondeu', 'hls'
            lines = [x.strip() for x in data.decode('utf-8', 'replace').splitlines()]
            nxt = next((x for x in lines if x and not x.startswith('#')), None)
            if not nxt:
                return False, 'Lista HLS vazia', 'hls'
            ok, detail, _ = probe(urljoin(final, nxt), ua, referer, depth + 1)
            return ok, ('HLS · ' + detail) if ok else detail, 'hls'
        if not data or _looks_html(data):
            return False, 'Resposta não é vídeo', 'ts'
        return True, f'Vídeo respondeu ({ctype or "binário"})', 'ts'
    except HTTPError as e:
        return False, f'HTTP {e.code}', ''
    except Exception as e:   # tempo esgotado, DNS, conexão recusada, endereço bloqueado…
        return False, str(e)[:150] or type(e).__name__, ''


# ------------------------------------------------------------------ contas Xtream: HLS no lugar de .ts
# Nas contas Xtream o .ts ao vivo costuma ser cortado pelo servidor a cada poucos segundos (o player
# precisa reabrir); o HLS do mesmo canal (/live/usuario/senha/ID.m3u8) segue estável. O teste do canal
# confere se a conta entrega o HLS e, se sim, ele passa a ser a primeira opção.
XTREAM_LIVE = re.compile(r'^(https?://[^/?#]+)/(?:live/)?([^/?#]+)/([^/?#]+)/(\d+)(?:\.ts)?$', re.I)


def xtream_hls(url):
    m = XTREAM_LIVE.match((url or '').strip())
    if not m or m.group(2).lower() in ('movie', 'series', 'play', 'hls'):
        return None
    return f'{m.group(1)}/live/{m.group(2)}/{m.group(3)}/{m.group(4)}.m3u8'


def sources_for(cid, with_hls=True):
    """Fonte principal + reservas, na ordem de uso (o HLS confirmado da conta Xtream vem primeiro)."""
    with connect() as c:
        main = c.execute('SELECT id, url, ua, referer, status, hls FROM selected WHERE id=?', (cid,)).fetchone()
        if not main:
            return []
        alts = c.execute('SELECT id, url, ua, referer, status FROM alternates WHERE channel_id=? ORDER BY position', (cid,)).fetchall()
    main = dict(main)
    out = [main] + [dict(a) for a in alts]
    if with_hls and (main.get('hls') or '').startswith('http'):
        out.insert(0, {**main, 'url': main['hls'], 'id': main['id'] + ':hls'})
    return out


# ------------------------------------------------------------------ fontes reserva
def find_alternates(ids=None, limit=4):
    """Procura o mesmo canal em outras listas (mesmo tvg-id ou mesmo nome) e guarda como reserva."""
    items, _ = index()
    cfg = content_filter.load()
    by_key = {}
    for x in items:
        if _hidden(x['group'], cfg):
            continue
        for k in match_keys(x['tvg_id'], x['name']):
            by_key.setdefault(k, []).append(x)
    added = 0
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT id, name, tvg_id, url FROM selected')]
        if ids:
            wanted = set(map(str, ids))
            rows = [r for r in rows if r['id'] in wanted]
        for r in rows:
            have = {a['id'] for a in c.execute('SELECT id FROM alternates WHERE channel_id=?', (r['id'],))}
            pos = len(have)
            for k in match_keys(r['tvg_id'], r['name']):
                for x in by_key.get(k, []):
                    if pos >= limit:
                        break
                    if x['id'] == r['id'] or x['url'] == r['url'] or x['id'] in have:
                        continue
                    pos += 1
                    have.add(x['id'])
                    c.execute('INSERT OR IGNORE INTO alternates(channel_id,id,name,url,ua,referer,source,position) '
                              'VALUES(?,?,?,?,?,?,?,?)',
                              (r['id'], x['id'], x['name'], x['url'], x['ua'], x['referer'], x['source'], pos))
                    added += 1
    return {'added': added}


def alternates(cid):
    with connect() as c:
        main = c.execute('SELECT name, source, status, detail, ua, referer FROM selected WHERE id=?', (str(cid),)).fetchone()
        if not main:
            raise ValueError('Canal não encontrado')
        alts = [dict(r) for r in c.execute('SELECT id, name, source, status, detail, ua, referer, position '
                                           'FROM alternates WHERE channel_id=? ORDER BY position', (str(cid),))]

    def pub(r):
        return {'name': r['name'], 'source': r['source'], 'status': r['status'], 'detail': r['detail'],
                'headers': bool(r['ua'] or r['referer'])}
    return {'main': pub(main), 'items': [pub(a) | {'id': a['id']} for a in alts]}


def remove_alternate(cid, alt_id):
    with connect() as c:
        c.execute('DELETE FROM alternates WHERE channel_id=? AND id=?', (str(cid), str(alt_id)))
    return alternates(cid)


def promote_alternate(cid, alt_id):
    """Troca a fonte principal pela reserva escolhida (a principal vira reserva)."""
    with connect() as c:
        main = c.execute('SELECT url, ua, referer, source, name, status, detail FROM selected WHERE id=?', (str(cid),)).fetchone()
        alt = c.execute('SELECT * FROM alternates WHERE channel_id=? AND id=?', (str(cid), str(alt_id))).fetchone()
        if not main or not alt:
            raise ValueError('Fonte não encontrada')
        c.execute('UPDATE selected SET url=?, ua=?, referer=?, source=?, status=?, detail=? WHERE id=?',
                  (alt['url'], alt['ua'], alt['referer'], alt['source'], alt['status'], alt['detail'], str(cid)))
        c.execute('UPDATE alternates SET id=?, url=?, ua=?, referer=?, source=?, name=?, status=?, detail=? '
                  'WHERE channel_id=? AND id=?',
                  (hashlib.sha1(main['url'].encode()).hexdigest()[:16], main['url'], main['ua'], main['referer'],
                   main['source'], main['name'], main['status'], main['detail'], str(cid), str(alt_id)))
    _good.pop(str(cid), None)
    return alternates(cid)


# ------------------------------------------------------------------ teste dos canais
TRANSIENT = re.compile(r'HTTP (429|5\d\d)|timed out|tempo|Connection (reset|aborted|refused)|Remote end closed|temporar', re.I)


def _probe_confirmed(url, ua, referer):
    """Testa; se falhar, confirma uma segunda vez antes de condenar a fonte (como fazem os apps de IPTV)."""
    ok, detail, kind = probe(url, ua, referer)
    if ok:
        return ok, detail, kind, False
    time.sleep(1.5)
    ok2, detail2, kind2 = probe(url, ua, referer)
    if ok2:
        return ok2, detail2 + ' (na 2ª tentativa)', kind2, False
    # 429/5xx/tempo esgotado: o servidor oscilou, não quer dizer que a fonte morreu
    return False, detail2, kind2, bool(TRANSIENT.search(detail2 or ''))


def _check(row):
    """Testa a principal e as reservas; o canal funciona se alguma fonte responder."""
    srcs = sources_for(row['id'], with_hls=False)
    results = []
    transient_main = False
    hls_state = None
    if srcs:
        hls = xtream_hls(srcs[0]['url'])
        if hls:
            ok_h, det_h, kind_h = probe(hls, srcs[0]['ua'], srcs[0]['referer'])
            hls_state = hls if ok_h and kind_h == 'hls' else 'no'
    for i, src in enumerate(srcs):
        if i == 0 and hls_state and hls_state != 'no':
            ok, detail, transient = True, 'HLS da conta Xtream respondeu (usado no lugar do .ts)', False
        else:
            ok, detail, _, transient = _probe_confirmed(src['url'], src['ua'], src['referer'])
        if i == 0:
            transient_main = transient
        results.append((ok, detail))
        if i:
            with connect() as c:
                c.execute('UPDATE alternates SET status=?, detail=?, checked_at=? WHERE channel_id=? AND id=?',
                          ('ok' if ok else 'fail', detail[:200], time.time(), row['id'], src['id']))
    good = [i for i, (ok, _) in enumerate(results) if ok]
    ok = bool(good)
    # oscilação do servidor (429/5xx/tempo) não derruba um canal que já estava funcionando
    kept = not ok and transient_main and row.get('status') == 'ok'
    if kept:
        ok = True
    if not results:
        detail = 'Sem fonte'
    elif results[0][0]:
        detail = results[0][1]
    elif good:
        detail = f'Principal falhou ({results[0][1]}); reserva {good[0]} funcionando'
    elif kept:
        detail = f'Oscilou no teste ({results[0][1]}); mantido como OK'
    else:
        detail = results[0][1]
    _good.pop(row['id'], None)        # a ordem das fontes pode ter mudado (HLS da conta Xtream)
    with connect() as c:
        c.execute('UPDATE selected SET status=?, detail=?, checked_at=?, hls=? WHERE id=?',
                  ('ok' if ok else 'fail', detail[:200], time.time(), hls_state or '', row['id']))
    with _lock:
        _test['done'] += 1
        _test['ok' if ok else 'fail'] += 1


def start_test(ids=None, only_untested=False):
    with _lock:
        if _test['running']:
            return dict(_test)
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT id, status FROM selected ORDER BY position')]
    if ids:
        wanted = set(map(str, ids))
        rows = [r for r in rows if r['id'] in wanted]
    if only_untested:
        rows = [r for r in rows if r['status'] == 'untested']
    with _lock:
        _test.update(running=True, total=len(rows), done=0, ok=0, fail=0, started=time.time())

    def run():
        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(_check, rows))
        finally:
            with _lock:
                _test['running'] = False
    threading.Thread(target=run, daemon=True, name='channel-test').start()
    return dict(_test)


def test_status():
    return dict(_test)


# ------------------------------------------------------------------ transmissão pelo indexador
_good = {}            # canal -> (índice da fonte que funcionou, quando)
URI_ATTR = re.compile(r'URI="([^"]+)"')


def _rewrite(text, base, cid, sidx, t):
    """Reescreve a lista HLS para que sub-listas e pedaços passem pelo indexador (com os cabeçalhos)."""
    prefix = f'seg?t={quote(t, safe="")}&c={cid}&s={sidx}&u='
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith('#'):
            line = prefix + quote(urljoin(base, s), safe='')
        elif 'URI="' in s:
            line = URI_ATTR.sub(lambda m: 'URI="' + prefix + quote(urljoin(base, m.group(1)), safe='') + '"', line)
        out.append(line)
    return ('\n'.join(out) + '\n').encode()


def _send_bytes(handler, data, ctype):
    handler.send_response(200)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.end_headers()
    handler.wfile.write(data)


STREAM_READ_TIMEOUT = 90   # fluxo ao vivo pode ficar parado entre blocos (HLS de 10 s via Dispatcharr)


def _stream_timeout(resp, seconds=STREAM_READ_TIMEOUT):
    """O timeout de _open() (10-20 s) vale para conectar; num fluxo contínuo ele derrubava o canal
    sempre que a fonte ficava alguns segundos sem mandar bytes. Aqui afrouxa só a leitura."""
    try:
        resp.fp.raw._sock.settimeout(seconds)
    except AttributeError:
        pass


def _pipe(handler, resp, first, ctype):
    _stream_timeout(resp)
    handler.send_response(200)
    handler.send_header('Content-Type', ctype or 'video/mp2t')
    length = resp.headers.get('Content-Length')
    if length and length.isdigit():
        handler.send_header('Content-Length', str(int(length)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.end_headers()
    try:
        handler.wfile.write(first)
        handler.wfile.flush()
        while True:
            chunk = resp.read1(64 * 1024)   # repassa assim que chega (read() esperava juntar 64 KB)
            if not chunk:
                break
            handler.wfile.write(chunk)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass   # o player fechou
    except (TimeoutError, OSError) as exc:
        print(f'CANAL: fonte sem dados por {STREAM_READ_TIMEOUT}s ou conexão perdida: {type(exc).__name__}', flush=True)


def stream(handler, cid, kind, t):
    """/strm-api/ch/<canal>.m3u8|.ts — tenta as fontes em ordem (a última que funcionou primeiro)."""
    if not valid_token(t):
        return handler.send_json(403, {'error': 'Chave inválida'})
    srcs = sources_for(cid)
    if not srcs:
        return handler.send_json(404, {'error': 'Canal não encontrado'})
    order = list(range(len(srcs)))
    last = _good.get(cid)
    if last and last[0] < len(srcs) and time.time() - last[1] < 600:
        order.remove(last[0])
        order.insert(0, last[0])
    errors = []
    for i in order:
        src = srcs[i]
        try:
            resp = _open(src['url'], src['ua'], src['referer'], timeout=10)
        except Exception as exc:
            errors.append(str(exc)[:80])
            continue
        try:
            final = resp.geturl()
            ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            first = resp.read(7)
            is_hls = first.lstrip(b'\xef\xbb\xbf').startswith(b'#EXTM3U') or 'mpegurl' in ctype
            if is_hls and kind == 'ts':
                resp.close()
                # Fonte HLS pedida como .ts: emenda os pedaços num fluxo contínuo (melhor no Jellyfin)
                rest = order[order.index(i):]
                if _hls_to_ts(handler, cid, srcs, rest):
                    return
                errors.append('HLS não convertível (fMP4/criptografado)')
                continue
            if is_hls != (kind == 'm3u8'):
                errors.append('formato diferente')   # reserva de outro tipo não serve neste endereço
                continue
            _good[cid] = (i, time.time())
            if is_hls:
                body = (first + resp.read(4 * 1024 * 1024)).decode('utf-8', 'replace')
                return _send_bytes(handler, _rewrite(body, final, cid, i, t), 'application/vnd.apple.mpegurl')
            return _pipe(handler, resp, first, ctype)
        finally:
            resp.close()
    _good.pop(cid, None)
    return handler.send_json(502, {'error': 'Nenhuma fonte respondeu', 'detalhes': errors[:4]})


def segment(handler, q, t):
    """Sub-listas e pedaços HLS repassados com os cabeçalhos da fonte."""
    if not valid_token(t):
        return handler.send_json(403, {'error': 'Chave inválida'})
    cid, url = q.get('c', ''), q.get('u', '')
    try:
        sidx = int(q.get('s', '0'))
    except ValueError:
        return handler.send_json(400, {'error': 'Fonte inválida'})
    srcs = sources_for(cid)
    if not srcs or not 0 <= sidx < len(srcs) or not url.startswith(('http://', 'https://')):
        return handler.send_json(404, {'error': 'Fonte não encontrada'})
    src = srcs[sidx]
    try:
        resp = _open(url, src['ua'], src['referer'], timeout=20)
    except HTTPError as e:
        return handler.send_json(e.code if 400 <= e.code < 600 else 502, {'error': f'HTTP {e.code}'})
    except Exception as exc:
        return handler.send_json(502, {'error': str(exc)[:120]})
    try:
        final = resp.geturl()
        ctype = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        first = resp.read(7)
        if first.lstrip(b'\xef\xbb\xbf').startswith(b'#EXTM3U') or 'mpegurl' in ctype:
            body = (first + resp.read(4 * 1024 * 1024)).decode('utf-8', 'replace')
            return _send_bytes(handler, _rewrite(body, final, cid, sidx, t), 'application/vnd.apple.mpegurl')
        return _pipe(handler, resp, first, ctype)
    finally:
        resp.close()


# ------------------------------------------------------------------ HLS -> TS contínuo (para o Jellyfin)
# O ffmpeg do Jellyfin lida mal com HLS ao vivo: se a lista atrasa, ele acha que o canal acabou
# (e, com "repetir transmissões", volta para o começo). Aqui o indexador acompanha a lista HLS e
# entrega os pedaços num único fluxo MPEG-TS sem fim, como o Dispatcharr faz.
TS_SYNC = 0x47
BW_ATTR = re.compile(r'BANDWIDTH=(\d+)')


class _NotTs(Exception):
    """Fonte HLS que não dá para emendar (pedaços fMP4 ou criptografados)."""


def _fetch_text(url, src, timeout=10):
    with _open(url, src['ua'], src['referer'], timeout=timeout) as r:
        return r.geturl(), r.read(4 * 1024 * 1024).decode('utf-8', 'replace')


def _parse_media(text, base):
    """Lista de mídia: (segmentos [(seq, url, dur)], duração alvo, terminou?)."""
    seq, target, ended, segs, dur = 0, 6.0, False, [], 0.0
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            try:
                seq = int(s.split(':', 1)[1])
            except ValueError:
                pass
        elif s.startswith('#EXT-X-TARGETDURATION:'):
            try:
                target = float(s.split(':', 1)[1])
            except ValueError:
                pass
        elif s.startswith('#EXT-X-ENDLIST'):
            ended = True
        elif s.startswith('#EXT-X-MAP'):
            raise _NotTs('pedaços fMP4')
        elif s.startswith('#EXT-X-KEY') and 'METHOD=NONE' not in s.upper():
            raise _NotTs('pedaços criptografados')
        elif s.startswith('#EXTINF:'):
            try:
                dur = float(s[8:].split(',', 1)[0])
            except ValueError:
                dur = target
        elif not s.startswith('#'):
            segs.append((seq, urljoin(base, s), dur or target))
            seq += 1
            dur = 0.0
    return segs, max(1.0, min(target, 15.0)), ended


def _media_playlist(url, src):
    """Abre a lista; se for a principal (com qualidades), escolhe a de maior qualidade."""
    final, text = _fetch_text(url, src)
    if '#EXT-X-STREAM-INF' not in text:
        return final, text
    best, best_bw, bw = None, -1, 0
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('#EXT-X-STREAM-INF'):
            m = BW_ATTR.search(s)
            bw = int(m.group(1)) if m else 0
        elif s and not s.startswith('#'):
            if bw > best_bw:
                best, best_bw = urljoin(final, s), bw
            bw = 0
    if not best:
        raise _NotTs('lista sem qualidades')
    return _fetch_text(best, src)


def _hls_to_ts(handler, cid, srcs, order):
    """Entrega a fonte HLS como um fluxo TS contínuo; troca para a reserva se a fonte cair."""
    started = False
    deadline_fail = 45            # segundos sem conseguir nada -> troca de fonte
    for attempt in range(len(order) * 2):          # dá uma segunda volta pelas fontes
        i = order[attempt % len(order)]
        src = srcs[i]
        try:
            url, text = _media_playlist(src['url'], src)
            segs, target, ended = _parse_media(text, url)
        except _NotTs:
            if not started:
                return False      # quem chamou responde com erro de formato
            continue
        except Exception:
            continue
        if not segs:
            continue
        _good[cid] = (i, time.time())
        # começa perto do ao vivo (3 pedaços antes do fim), como os players fazem
        last = segs[max(0, len(segs) - 3)][0] - 1
        seen_urls = set()
        last_ok = time.time()
        while True:
            new = [s for s in segs if s[0] > last and s[1] not in seen_urls]
            if segs and segs[-1][0] < last - 50:    # a fonte reiniciou a numeração
                new = segs[-3:]
            for seq, seg_url, _ in new:
                last = max(last, seq)
                seen_urls.add(seg_url)
                if len(seen_urls) > 200:
                    seen_urls = set(list(seen_urls)[-100:])
                data = None
                for _try in range(2):
                    try:
                        with _open(seg_url, src['ua'], src['referer'], timeout=20) as r:
                            data = r.read(64 * 1024 * 1024)
                        break
                    except Exception:
                        time.sleep(0.5)
                if not data:
                    continue                     # pula o pedaço perdido em vez de travar
                if data[0] != TS_SYNC:
                    if not started:
                        return False             # não é MPEG-TS (ex.: AAC puro / fMP4)
                    continue
                if not started:
                    handler.send_response(200)
                    handler.send_header('Content-Type', 'video/mp2t')
                    handler.send_header('Cache-Control', 'no-store')
                    handler.send_header('Access-Control-Allow-Origin', '*')
                    handler.send_header('Connection', 'close')
                    handler.end_headers()
                    started = True
                try:
                    handler.wfile.write(data)
                    handler.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return True                  # o player fechou
                last_ok = time.time()
            if ended:
                return True                      # VOD disfarçado de canal: termina, não repete
            if time.time() - last_ok > deadline_fail:
                break                            # fonte parou: tenta a próxima
            time.sleep(target / 2 if not new else min(1.0, target / 3))
            try:
                url, text = _media_playlist(src['url'], src)
                segs, target, ended = _parse_media(text, url)
            except _NotTs:
                break
            except Exception:
                segs = []                        # erro temporário: tenta de novo na próxima volta
    if started:
        return True
    return False


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


def playlist(t, base='', fmt=''):
    """Lista M3U. [base] = endereço público do indexador (para apontar o guia de programação).

    fmt='ts': todos os canais pelo indexador como MPEG-TS contínuo (ideal para o Jellyfin)."""
    if not valid_token(t):
        return None
    import epg
    with connect() as c:
        only_ok = _setting(c, 'only_ok', '1') == '1'
        guide = _setting(c, 'epg_url')
        mode = _setting(c, 'stream_mode', 'auto')
        rows = [dict(r) for r in c.execute('SELECT * FROM selected ORDER BY position, added_at')]
        with_alts = {r[0] for r in c.execute('SELECT DISTINCT channel_id FROM alternates')}
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
        # Pelo indexador: envia os cabeçalhos exigidos e troca para a reserva se a fonte cair
        needs = bool(r['ua'] or r['referer']) or r['id'] in with_alts
        if base and fmt == 'ts':
            lines.append(f'{base}/strm-api/ch/{r["id"]}.ts?t={t}')
        hls = r.get('hls') if (r.get('hls') or '').startswith('http') else ''
        if base and fmt == 'ts':
            pass
        elif base and (mode == 'proxy' or (mode == 'auto' and needs)):
            kind = 'm3u8' if hls or urlsplit(r['url']).path.lower().endswith('.m3u8') else 'ts'
            lines.append(f'{base}/strm-api/ch/{r["id"]}.{kind}?t={t}')
        else:
            if r['ua']:
                lines.append(f'#EXTVLCOPT:http-user-agent={r["ua"]}')
            if r['referer']:
                lines.append(f'#EXTVLCOPT:http-referrer={r["referer"]}')
            lines.append(hls or r['url'])
    return '\n'.join(lines) + '\n'
