"""Ponte Jellyfin → STRM Indexer: busca e solicitação a partir da pesquisa do Jellyfin.

Rotas públicas (sob /jf/), autenticadas pelo token do PRÓPRIO usuário do Jellyfin:
  GET  /jf/search?q=...        títulos do catálogo que ainda não estão no Jellyfin
  POST /jf/request             {kind, title, year?, season?}  → cria solicitação
  GET  /jf/status?id=...       acompanha a solicitação
  GET  /jf/art/<hash>.jpg      capas locais (nomes por hash, sem dados sensíveis)
Rotas do painel (sessão de administrador, tratadas no api.py):
  GET  /requests, POST /requests/retry

Aprovação automática: um pedido só vira STRM se a fonte passar na validação
(media.check_entry). Nenhuma URL de mídia é devolvida ao navegador.

Configuração (.env):
  INDEXER_JELLYFIN_URL / INDEXER_JELLYFIN_API_KEY  (já existentes; usados para
      validar tokens e para mandar o Jellyfin atualizar a biblioteca)
  INDEXER_JF_ORIGINS        origens liberadas para CORS, separadas por vírgula
                            (vazio = somente mesma origem, via proxy reverso)
  INDEXER_JF_DAILY_LIMIT    pedidos por usuário a cada 24 h (padrão 20)
  INDEXER_JF_REFRESH_DELAY  segundos entre atualizações da biblioteca (padrão 60)
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from urllib.parse import parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler

import catalog_enrich as enrich
import jellyfin_meta as jellyfin
import media_control as media
import series_bulk as bulk
import content_filter
import live_relay
from catalog_identity import key_for
from indexer import DB as CATALOG, norm

STATE = media.DATA / 'jf_requests.sqlite'


def _env(key, default=''):
    """Variável de ambiente; se o compose não repassar, lê do .env da pasta do indexador."""
    if os.environ.get(key):
        return os.environ[key]
    from pathlib import Path
    try:
        for line in (Path(__file__).resolve().parent / '.env').read_text(encoding='utf-8').splitlines():
            k, sep, v = line.strip().partition('=')
            if sep and k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return default


ORIGINS = {o.strip().rstrip('/') for o in _env('INDEXER_JF_ORIGINS').split(',') if o.strip()}
DAILY_LIMIT = max(1, int(_env('INDEXER_JF_DAILY_LIMIT', '20') or 20))
REFRESH_DELAY = max(30, int(_env('INDEXER_JF_REFRESH_DELAY', '60') or 60))
MAX_CANDIDATES = 6
WAKE = threading.Event()
_TOKENS = {}
_TOKEN_LOCK = threading.Lock()
_refresh = {'pending': False, 'last': 0.0}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = build_opener(NoRedirect())


# ----------------------------------------------------------------- estado
def connect():
    c = sqlite3.connect(STATE, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA busy_timeout=15000')
    return c


def setup():
    media.DATA.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS requests(
            id TEXT PRIMARY KEY, user TEXT NOT NULL, user_id TEXT NOT NULL,
            kind TEXT NOT NULL, title TEXT NOT NULL, normalized TEXT NOT NULL,
            year INTEGER, season INTEGER, state TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '', job_id TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_jf_req_state ON requests(state, created_at);
        CREATE INDEX IF NOT EXISTS idx_jf_req_user ON requests(user_id, created_at);
        ''')
        # Pedidos interrompidos por reinício voltam para a fila.
        c.execute("UPDATE requests SET state='queued' WHERE state='validating'")


# ----------------------------------------------------------------- autenticação
def _jf_request(path, token, method='GET', timeout=10):
    req = Request(jellyfin.BASE + path, method=method, data=b'' if method == 'POST' else None, headers={
        'Authorization': f'MediaBrowser Token="{token}"',
        'Accept': 'application/json', 'User-Agent': 'STRMIndexer-Bridge/1.0'})
    with OPENER.open(req, timeout=timeout) as resp:
        return resp.status, resp.read(200_000)


def user_from_token(token):
    """Confere o token no Jellyfin (GET /Users/Me). Cache de 10 minutos."""
    if not token or len(token) > 200 or not re.fullmatch(r'[A-Za-z0-9._-]+', token):
        return None
    if not jellyfin.BASE:
        raise RuntimeError('INDEXER_JELLYFIN_URL não configurado')
    digest = hashlib.sha256(token.encode()).hexdigest()
    now = time.time()
    with _TOKEN_LOCK:
        hit = _TOKENS.get(digest)
        if hit and hit[2] > now:
            return hit[0]
    try:
        status, body = _jf_request('/Users/Me', token)
        data = json.loads(body) if status == 200 else None
    except Exception:
        data = None
    user = None
    if isinstance(data, dict) and data.get('Id') and not (data.get('Policy') or {}).get('IsDisabled'):
        user = {'id': str(data['Id']), 'name': str(data.get('Name') or 'usuário')[:60],
                'admin': bool((data.get('Policy') or {}).get('IsAdministrator'))}
    with _TOKEN_LOCK:
        if len(_TOKENS) > 500:
            _TOKENS.clear()
        _TOKENS[digest] = (user, 0, now + (600 if user else 30))
    return user


def _token_from(handler):
    h = handler.headers
    token = h.get('X-Emby-Token') or h.get('X-MediaBrowser-Token') or ''
    if not token:
        m = re.search(r'Token="([^"]+)"', h.get('Authorization', '') or h.get('X-Emby-Authorization', ''))
        token = m.group(1) if m else ''
    return token.strip()


# ----------------------------------------------------------------- HTTP
def _cors(handler):
    origin = (handler.headers.get('Origin') or '').rstrip('/')
    if origin and origin in ORIGINS:
        handler.send_header('Access-Control-Allow-Origin', origin)
        handler.send_header('Vary', 'Origin')
        handler.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Emby-Token, Authorization')
        handler.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        handler.send_header('Access-Control-Max-Age', '600')


def _send(handler, code, obj):
    data = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(code)
    handler.send_header('Content-Type', 'application/json; charset=utf-8')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'no-store')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    _cors(handler)
    handler.end_headers()
    handler.wfile.write(data)


def options(handler):
    handler.send_response(204)
    _cors(handler)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def handle(handler, method, parsed):
    # Aceita /jf/... e /strm-api/... (para proxies que não reescrevem o caminho, ex.: Cloudflare Tunnel)
    path = re.sub(r'^/(?:jf|strm-api)', '', parsed.path) or '/'
    if method == 'GET' and path.startswith('/art/'):
        return _art(handler, path[len('/art/'):])
    if method == 'GET' and path == '/health':
        return _send(handler, 200, {'ok': True, 'jellyfin': bool(jellyfin.BASE)})
    if method == 'GET' and path == '/relay':
        # Canal de lista M3U para o navegador: token na URL porque o <video> não envia cabeçalhos
        q = parse_qs(parsed.query)
        token, target = q.get('t', [''])[0], q.get('u', [''])[0]
        try:
            ok = user_from_token(token)
        except RuntimeError as exc:
            return _send(handler, 503, {'error': str(exc)})
        if not ok:
            return _send(handler, 401, {'error': 'Sessão do Jellyfin inválida'})
        try:
            return live_relay.relay(handler, target, token)
        except ValueError as exc:
            return _send(handler, 400, {'error': str(exc)})
        except Exception as exc:
            print('RELAY:', type(exc).__name__, flush=True)
            try:
                return _send(handler, 502, {'error': 'O canal não respondeu'})
            except Exception:
                return None
    try:
        user = user_from_token(_token_from(handler))
    except RuntimeError as exc:
        return _send(handler, 503, {'error': str(exc)})
    if not user:
        return _send(handler, 401, {'error': 'Sessão do Jellyfin inválida'})
    try:
        if method == 'GET' and path == '/search':
            q = parse_qs(parsed.query).get('q', [''])[0]
            return _send(handler, 200, search(q))
        if method == 'GET' and path == '/status':
            rid = parse_qs(parsed.query).get('id', [''])[0]
            return _send(handler, 200, status(rid, user))
        if method == 'GET' and path == '/livetv':
            q = parse_qs(parsed.query)
            try:
                return _send(handler, 200, live_relay.livetv(q.get('m3u', [''])[0], q.get('epg', [''])[0], q.get('force', [''])[0] == '1'))
            except ValueError as exc:
                return _send(handler, 400, {'error': str(exc)})
            except Exception as exc:
                print('LIVETV:', type(exc).__name__, flush=True)
                return _send(handler, 502, {'error': 'Não foi possível baixar a lista'})
        if method == 'GET' and path == '/mine':
            return _send(handler, 200, mine(user))
        if method == 'POST' and path == '/request':
            size = int(handler.headers.get('Content-Length', '0'))
            if not 2 <= size <= 2048:
                raise ValueError('Requisição inválida')
            body = json.loads(handler.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError('Requisição inválida')
            code, result = create(user, body)
            return _send(handler, code, result)
        return _send(handler, 404, {'error': 'Rota não encontrada'})
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return _send(handler, 400, {'error': str(exc)})
    except sqlite3.Error:
        return _send(handler, 503, {'error': 'Catálogo indisponível'})


def _art(handler, name):
    image = enrich.artwork(name)
    if not image:
        return _send(handler, 404, {'error': 'Imagem indisponível'})
    data = image.read_bytes()
    handler.send_response(200)
    handler.send_header('Content-Type', {'.jpg': 'image/jpeg', '.png': 'image/png', '.webp': 'image/webp'}[image.suffix])
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'public, max-age=86400, immutable')
    handler.send_header('X-Content-Type-Options', 'nosniff')
    handler.send_header('Cross-Origin-Resource-Policy', 'cross-origin')
    handler.end_headers()
    handler.wfile.write(data)


# ----------------------------------------------------------------- busca
def _like(q):
    return '%' + norm(q).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'


def search(q):
    q = (q or '').strip()
    if len(q) < 2 or len(q) > 120:
        return {'items': []}
    n, like = norm(q), _like(q)
    block_sql, block_params = content_filter.where()   # categorias bloqueadas (ex.: adultos)
    with sqlite3.connect(f'file:{CATALOG}?mode=ro', uri=True, timeout=15) as db:
        db.row_factory = sqlite3.Row
        movies = [dict(r) for r in db.execute(
            '''SELECT MIN(title) title, year, COUNT(*) sources, MIN(normalized) normalized FROM entries
               WHERE kind='movie' AND normalized LIKE ? ESCAPE '\\' AND ''' + block_sql + '''
               GROUP BY normalized, COALESCE(year,-1)
               ORDER BY (MIN(normalized)=?) DESC, (MIN(normalized) LIKE ?) DESC, LENGTH(MIN(normalized)) LIMIT 40''',
            (like, *block_params, n, n.replace('%', '') + '%'))]
        series = [dict(r) for r in db.execute(
            '''SELECT MIN(title) title, MIN(normalized) normalized,
                      COUNT(DISTINCT season || 'x' || episode) episodes,
                      GROUP_CONCAT(DISTINCT season) seasons FROM entries
               WHERE kind='episode' AND normalized LIKE ? ESCAPE '\\' AND season IS NOT NULL AND ''' + block_sql + '''
               GROUP BY normalized
               ORDER BY (normalized=?) DESC, LENGTH(normalized) LIMIT 20''', (like, *block_params, n))]
    states = media.content_states([key_for('movie', m['title'], m['year']) for m in movies])
    with media.connect() as c:
        managed_eps = {}
        for t, cnt in c.execute("SELECT title, COUNT(*) FROM managed WHERE kind='episode' GROUP BY title"):
            managed_eps[norm(t)] = managed_eps.get(norm(t), 0) + cnt
    pending = _pending_keys()
    out = []
    mmeta = enrich.for_cards('movie', [m['title'] for m in movies])
    for m in movies:
        if states.get(key_for('movie', m['title'], m['year'])):
            continue  # já está no Jellyfin
        meta = mmeta.get(m['title']) or {}
        out.append({'kind': 'movie', 'title': m['title'], 'year': m['year'] or meta.get('year'),
                    'catalog_year': m['year'], 'sources': m['sources'],
                    'poster': _art_url(meta), 'overview': (meta.get('overview') or '')[:300],
                    'requested': ('movie', m['normalized'], m['year'], None) in pending})
        if sum(1 for x in out if x['kind'] == 'movie') >= 12:
            break
    smeta = enrich.for_cards('episode', [s['title'] for s in series])
    count = 0
    for s in series:
        have = managed_eps.get(s['normalized'], 0)
        if have >= s['episodes']:
            continue
        meta = smeta.get(s['title']) or {}
        seasons = sorted({int(x) for x in (s['seasons'] or '').split(',') if x.strip().lstrip('-').isdigit()})
        out.append({'kind': 'series', 'title': s['title'], 'year': meta.get('year'), 'episodes': s['episodes'],
                    'in_library': have, 'seasons': seasons, 'poster': _art_url(meta),
                    'overview': (meta.get('overview') or '')[:300],
                    'requested': ('series', s['normalized'], None, None) in pending})
        count += 1
        if count >= 8:
            break
    return {'items': out}


def _art_url(meta):
    p = meta.get('poster') if meta else None
    return 'art/' + p.rsplit('/', 1)[-1] if p else None


def _pending_keys():
    with connect() as c:
        rows = c.execute("SELECT kind, normalized, year, season FROM requests WHERE state IN ('queued','validating','importing')").fetchall()
    keys = set()
    for r in rows:
        keys.add((r['kind'], r['normalized'], r['year'], r['season']))
        if r['kind'] == 'series':
            keys.add(('series', r['normalized'], None, None))
    return keys


# ----------------------------------------------------------------- solicitações
def create(user, body):
    kind = body.get('kind')
    title = body.get('title')
    if kind not in ('movie', 'series'):
        raise ValueError('Tipo inválido')
    if not isinstance(title, str) or not 2 <= len(title.strip()) <= 180:
        raise ValueError('Título inválido')
    title = title.strip()
    year = body.get('year')
    year = int(year) if year not in (None, '') else None
    if year is not None and not 1900 <= year <= 2100:
        raise ValueError('Ano inválido')
    season = body.get('season')
    season = int(season) if season not in (None, '') else None
    if season is not None and not 0 <= season <= 999:
        raise ValueError('Temporada inválida')
    if kind == 'movie':
        season = None
    normalized = norm(title)
    with sqlite3.connect(f'file:{CATALOG}?mode=ro', uri=True, timeout=10) as db:
        block_sql, block_params = content_filter.where()
        exists = db.execute('SELECT 1 FROM entries WHERE kind=? AND normalized=? AND ' + block_sql + ' LIMIT 1',
                            ('movie' if kind == 'movie' else 'episode', normalized, *block_params)).fetchone()
    if not exists:
        raise ValueError('Título não encontrado no catálogo')
    now = time.time()
    with connect() as c:
        same = c.execute('''SELECT id, state FROM requests WHERE kind=? AND normalized=? AND year IS ? AND season IS ?
                            AND state IN ('queued','validating','importing') LIMIT 1''',
                         (kind, normalized, year, season)).fetchone()
        if same:
            return 200, {'id': same['id'], 'state': same['state'], 'duplicate': True}
        if not user['admin']:
            used = c.execute('SELECT COUNT(*) FROM requests WHERE user_id=? AND created_at>?',
                             (user['id'], now - 86400)).fetchone()[0]
            if used >= DAILY_LIMIT:
                return 429, {'error': f'Limite de {DAILY_LIMIT} solicitações por dia atingido'}
        rid = uuid.uuid4().hex
        c.execute('''INSERT INTO requests(id,user,user_id,kind,title,normalized,year,season,state,message,created_at,updated_at)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (rid, user['name'], user['id'], kind, title, normalized, year, season, 'queued',
                   'Na fila para validação', now, now))
    WAKE.set()
    print(f'JF_REQUEST: {user["name"]!r} pediu {kind} {title[:60]!r}', flush=True)
    return 200, {'id': rid, 'state': 'queued'}


def _public(row):
    return {k: row[k] for k in ('id', 'kind', 'title', 'year', 'season', 'state', 'message', 'created_at', 'updated_at')}


def status(rid, user):
    if not isinstance(rid, str) or not re.fullmatch(r'[a-f0-9]{32}', rid):
        raise ValueError('ID inválido')
    with connect() as c:
        row = c.execute('SELECT * FROM requests WHERE id=?', (rid,)).fetchone()
    if not row or (row['user_id'] != user['id'] and not user['admin']):
        raise ValueError('Solicitação não encontrada')
    return _public(row)


def mine(user):
    with connect() as c:
        rows = c.execute('SELECT * FROM requests WHERE user_id=? ORDER BY created_at DESC LIMIT 30', (user['id'],)).fetchall()
    return {'items': [_public(r) for r in rows]}


def admin_list():
    with connect() as c:
        rows = [dict(r) for r in c.execute('SELECT * FROM requests ORDER BY created_at DESC LIMIT 300')]
        counts = dict(c.execute('SELECT state, COUNT(*) FROM requests GROUP BY state').fetchall())
    return {'items': rows, 'counts': counts}


def admin_retry(rid):
    if not isinstance(rid, str) or not re.fullmatch(r'[a-f0-9]{32}', rid):
        raise ValueError('ID inválido')
    with connect() as c:
        n = c.execute("UPDATE requests SET state='queued', message='Reenviado pelo administrador', updated_at=? "
                      "WHERE id=? AND state IN ('unavailable','failed')", (time.time(), rid)).rowcount
    if not n:
        raise ValueError('Só pedidos sem fonte ou com falha podem ser reprocessados')
    WAKE.set()
    return {'ok': True}


def _set(rid, **fields):
    fields['updated_at'] = time.time()
    cols = ', '.join(k + '=?' for k in fields)
    with connect() as c:
        c.execute('UPDATE requests SET ' + cols + ' WHERE id=?', (*fields.values(), rid))


def _process_movie(row):
    _set(row['id'], state='validating', message='Validando fontes…')
    block_sql, block_params = content_filter.where()
    with sqlite3.connect(f'file:{CATALOG}?mode=ro', uri=True, timeout=15) as db:
        db.row_factory = sqlite3.Row
        ids = [r['id'] for r in db.execute(
            '''SELECT id FROM entries WHERE kind='movie' AND normalized=? AND (? IS NULL OR year IS NULL OR year=?)
               AND ''' + block_sql + ''' ORDER BY (year IS ?) DESC, source_order, id LIMIT ?''',
            (row['normalized'], row['year'], row['year'], *block_params, row['year'], MAX_CANDIDATES))]
    if not ids:
        return _set(row['id'], state='unavailable', message='Título não está mais nas listas')
    reasons = []
    for entry_id in ids:
        try:
            check = media.check_entry(entry_id)
            if not check['ok']:
                reasons.append(check['reason'][:60])
                continue
            with bulk.PROCESS_LOCK:
                result = media.add_entry(entry_id)
            _set(row['id'], state='available', path=result.get('path', ''),
                 message='Já estava no Jellyfin' if result.get('already_added') else 'Adicionado; aparece após a atualização da biblioteca')
            _refresh['pending'] = True
            return
        except (ValueError, OSError, sqlite3.Error) as exc:
            reasons.append(str(exc)[:60])
    _set(row['id'], state='unavailable', message='Nenhuma fonte respondeu: ' + '; '.join(reasons[:2]))


def _process_series(row):
    if row['job_id']:
        job = bulk.get_job(row['job_id'])['job']
        if job['state'] in ('queued', 'running'):
            return _set(row['id'], state='importing',
                        message=f"Importando {job['completed']}/{job['total']} episódios")
        added, already, pending = job['added'], job['already'], job['pending']
        if added or already:
            msg = f'{added} episódio(s) adicionado(s)' + (f', {pending} sem fonte no momento' if pending else '')
            _set(row['id'], state='available', message=msg)
            _refresh['pending'] = True
        else:
            _set(row['id'], state='unavailable', message='Nenhum episódio com fonte válida agora; nova tentativa diária automática')
        return
    try:
        result = bulk.start(row['title'], '', row['season'])
    except ValueError as exc:
        return _set(row['id'], state='unavailable', message=str(exc)[:150])
    _set(row['id'], state='importing', job_id=result['job_id'],
         message=f"Validando {result['episodes']} episódio(s)")


def _refresh_library():
    try:
        status_code, _ = _jf_request('/Library/Refresh', jellyfin.TOKEN, method='POST', timeout=15)
        print(f'JF_REQUEST: atualização da biblioteca solicitada (HTTP {status_code})', flush=True)
    except Exception as exc:
        print(f'JF_REQUEST: falha ao atualizar biblioteca: {type(exc).__name__}', flush=True)


def loop(stop):
    print('JF_REQUEST: trabalhador iniciado', flush=True)
    while not stop.is_set():
        row = None
        try:
            with connect() as c:
                row = c.execute("SELECT * FROM requests WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
                if not row:
                    row = c.execute("SELECT * FROM requests WHERE state='importing' AND updated_at < ? ORDER BY updated_at LIMIT 1",
                                    (time.time() - 4,)).fetchone()
            if row:
                (_process_movie if row['kind'] == 'movie' else _process_series)(row)
        except Exception as exc:
            print(f'JF_REQUEST: falha {type(exc).__name__}', flush=True)
            if row:
                try:
                    _set(row['id'], state='failed', message='Erro interno: ' + type(exc).__name__)
                except sqlite3.Error:
                    pass
        if _refresh['pending'] and time.time() - _refresh['last'] >= REFRESH_DELAY and jellyfin.configured():
            _refresh.update(pending=False, last=time.time())
            _refresh_library()
        if not row:
            WAKE.wait(5)
            WAKE.clear()
